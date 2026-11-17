"""
API ресторана «Тарелка».

Единственный потребитель — AI-агент (голосовой и WhatsApp). Поэтому ответы
сделаны максимально «съедобными» для языковой модели: цены в рублях числом,
никаких копеек, никаких вложенных структур глубже двух уровней.

Главный принцип: модель не может назвать блюдо или цену, которых нет в базе.
Любое добавление в заказ идёт по id, полученному из поиска.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from contextlib import asynccontextmanager
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import db, drafts, iiko

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend")

TOKEN = os.getenv("BACKEND_TOKEN", "")
DELIVERY_TEXT = os.getenv("DELIVERY_TEXT", "в течение часа")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await drafts.connect()
    await db.sync_menu()
    yield
    await drafts.disconnect()
    await db.disconnect()


app = FastAPI(title="Tarelka API", lifespan=lifespan)


def auth(authorization: str = Header(default="")) -> None:
    if not TOKEN:
        return
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "unauthorized")


def rub(cents: int) -> int:
    return int(round(cents / 100))


# ---------------------------------------------------------------------------
# Меню
# ---------------------------------------------------------------------------


def item_view(row) -> dict:
    sizes = json.loads(row["sizes"]) if isinstance(row["sizes"], str) else row["sizes"]
    mods = json.loads(row["modifiers"]) if isinstance(row["modifiers"], str) else row["modifiers"]
    out: dict = {"id": str(row["id"]), "name": row["name"]}
    if row["description"]:
        out["описание"] = row["description"]
    if sizes:
        out["размеры"] = [{"size": s["name"], "цена": rub(s["price_cents"])} for s in sizes]
    else:
        out["цена"] = rub(row["price_cents"])
    if mods:
        out["добавки"] = [{"name": m["name"], "цена": rub(m["price_cents"])} for m in mods]
    if row["is_hit"]:
        out["хит"] = True
    return out


SEARCH_SQL = """
SELECT i.* FROM menu_items i
WHERE i.is_available
  AND (
        lower(i.name) LIKE '%' || lower($1) || '%'
     OR EXISTS (
          SELECT 1 FROM jsonb_array_elements_text(i.aliases) a
          WHERE lower(a) LIKE '%' || lower($1) || '%'
        )
     OR similarity(lower(i.name), lower($1)) > 0.3
  )
ORDER BY similarity(lower(i.name), lower($1)) DESC
LIMIT $2
"""


@app.get("/api/menu/search", dependencies=[Depends(auth)])
async def search_menu(q: str, limit: int = 5) -> dict:
    async with db.pool.acquire() as c:
        rows = await c.fetch(SEARCH_SQL, q, limit)
    return {"items": [item_view(r) for r in rows]}


@app.get("/api/menu/recommend", dependencies=[Depends(auth)])
async def recommend(kind: str = "main", limit: int = 4, call_id: str = "") -> dict:
    """Что предлагать. kind:
    main    — то, что продаём в первую очередь (сеты, пицца, мангал, горячее)
    extra   — допродажа (роллы, салаты, десерты)
    garnish — гарниры
    sauce   — соусы
    drink   — напитки
    """
    # Жёсткий предел допродажи. Промпт можно обойти, счётчик — нет.
    if call_id and kind in ("garnish", "sauce", "drink", "extra"):
        d = await drafts.get(call_id)
        if len(d.get("offered", [])) >= 2:
            return {
                "блюда в наличии": True,
                "предлагать самой": False,
                "что делать": "лимит твоих предложений исчерпан. Блюда есть, "
                "просто не предлагай их сам. Если гость попросит — "
                "найди через search_menu и добавь.",
            }
    if kind == "main":
        where, args = "c.tier = 1", []
    elif kind == "extra":
        where, args = "c.tier = 2", []
    else:
        where, args = "c.addon_kind = $1", [kind]

    sql = f"""
        SELECT i.* FROM menu_items i
        JOIN categories c ON c.key = i.category_key
        WHERE i.is_available AND {where}
        ORDER BY i.is_hit DESC, c.sort_order, random()
        LIMIT {int(limit)}
    """
    async with db.pool.acquire() as c:
        rows = await c.fetch(sql, *args)
    return {"items": [item_view(r) for r in rows]}


# ---------------------------------------------------------------------------
# Звонки
# ---------------------------------------------------------------------------


class StartCall(BaseModel):
    channel: str = "phone"
    caller: str = ""
    room: str = ""


@app.post("/api/calls", dependencies=[Depends(auth)])
async def start_call(body: StartCall) -> dict:
    async with db.pool.acquire() as c:
        row = await c.fetchrow(
            "INSERT INTO calls (channel, caller, room) VALUES ($1,$2,$3) RETURNING id",
            body.channel,
            body.caller,
            body.room,
        )
    call_id = str(row["id"])
    await drafts.save(drafts.empty(call_id))
    return {"call_id": call_id, "delivery_time": DELIVERY_TEXT}


@app.post("/api/calls/{call_id}/finish", dependencies=[Depends(auth)])
async def finish_call(call_id: str, status: str = "completed") -> dict:
    async with db.pool.acquire() as c:
        await c.execute("UPDATE calls SET status=$2, ended_at=now() WHERE id=$1", call_id, status)
    return {"ok": True}


class Line(BaseModel):
    speaker: str
    text: str


@app.post("/api/calls/{call_id}/transcript", dependencies=[Depends(auth)])
async def add_transcript(call_id: str, body: Line) -> dict:
    async with db.pool.acquire() as c:
        await c.execute(
            "INSERT INTO transcripts (call_id, speaker, text) VALUES ($1,$2,$3)",
            call_id,
            body.speaker,
            body.text,
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Черновик заказа
# ---------------------------------------------------------------------------


def draft_view(d: dict) -> dict:
    items = [
        {
            "line_id": i,
            "название": it["name"]
            + (f" ({it['size']})" if it.get("size") else "")
            + (
                " + " + ", ".join(m["name"] for m in it["modifiers"]) if it.get("modifiers") else ""
            ),
            "количество": it["quantity"],
            "сумма": rub(
                (it["price_cents"] + sum(m["price_cents"] for m in it["modifiers"]))
                * it["quantity"]
            ),
        }
        for i, it in enumerate(d["items"])
    ]
    missing = []
    if d["items"]:
        if not d.get("customer_name"):
            missing.append("имя гостя")
        if not d.get("address"):
            missing.append("адрес доставки")
        if d.get("persons") is None:
            missing.append("на сколько персон приборы")
        if not d.get("payment"):
            missing.append("способ оплаты")
    return {
        "позиции": items,
        "итого": rub(drafts.total_cents(d)),
        "пусто": not items,
        "не хватает": missing,
        "уже предлагали": d.get("offered", []),
        "гость": d.get("customer_name"),
        "адрес": d.get("address"),
        "персон": d.get("persons"),
        "приборы": d.get("cutlery"),
        "оплата": d.get("payment"),
    }


class AddItem(BaseModel):
    item_id: str
    quantity: int = 1
    size: str | None = None
    modifiers: list[str] = Field(default_factory=list)


@app.post("/api/calls/{call_id}/items", dependencies=[Depends(auth)])
async def add_item(call_id: str, body: AddItem) -> dict:
    async with db.pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT * FROM menu_items WHERE id=$1 AND is_available", body.item_id
        )
    if not row:
        raise HTTPException(422, "блюдо не найдено, используй поиск по меню")

    sizes = json.loads(row["sizes"]) if isinstance(row["sizes"], str) else row["sizes"]
    mods_all = (
        json.loads(row["modifiers"]) if isinstance(row["modifiers"], str) else row["modifiers"]
    )

    price = row["price_cents"]
    size_name = None
    if sizes:
        if not body.size:
            return {
                "error": "нужен размер",
                "доступные размеры": [
                    {"size": s["name"], "цена": rub(s["price_cents"])} for s in sizes
                ],
            }
        match = next((s for s in sizes if s["name"].lower() == body.size.lower()), None)
        if not match:
            return {
                "error": "такого размера нет",
                "доступные размеры": [s["name"] for s in sizes],
            }
        price, size_name = match["price_cents"], match["name"]

    chosen_mods = [m for m in mods_all if m["name"].lower() in [x.lower() for x in body.modifiers]]

    d = await drafts.get(call_id)
    # одинаковая позиция с тем же размером и добавками — просто увеличиваем количество
    for it in d["items"]:
        same_mods = sorted(m["name"] for m in it["modifiers"]) == sorted(
            m["name"] for m in chosen_mods
        )
        if it["item_id"] == str(row["id"]) and it.get("size") == size_name and same_mods:
            it["quantity"] += max(1, body.quantity)
            break
    else:
        d["items"].append(
            {
                "item_id": str(row["id"]),
                "name": row["name"],
                "size": size_name,
                "price_cents": price,
                "quantity": max(1, body.quantity),
                "modifiers": chosen_mods,
                "category": row["category_key"],
            }
        )
    await drafts.save(d)
    return draft_view(d)


@app.delete("/api/calls/{call_id}/items/{line_id}", dependencies=[Depends(auth)])
async def remove_item(call_id: str, line_id: int) -> dict:
    d = await drafts.get(call_id)
    if 0 <= line_id < len(d["items"]):
        d["items"].pop(line_id)
        await drafts.save(d)
    return draft_view(d)


class Qty(BaseModel):
    quantity: int


@app.patch("/api/calls/{call_id}/items/{line_id}", dependencies=[Depends(auth)])
async def set_quantity(call_id: str, line_id: int, body: Qty) -> dict:
    d = await drafts.get(call_id)
    if 0 <= line_id < len(d["items"]):
        if body.quantity <= 0:
            d["items"].pop(line_id)
        else:
            d["items"][line_id]["quantity"] = body.quantity
        await drafts.save(d)
    return draft_view(d)


@app.get("/api/calls/{call_id}/draft", dependencies=[Depends(auth)])
async def get_draft(call_id: str) -> dict:
    return draft_view(await drafts.get(call_id))


class Customer(BaseModel):
    name: str | None = None
    address: str | None = None
    persons: int | None = None
    cutlery: bool | None = None
    payment: str | None = None
    change_from: int | None = None
    comment: str | None = None


@app.patch("/api/calls/{call_id}/customer", dependencies=[Depends(auth)])
async def set_customer(call_id: str, body: Customer) -> dict:
    d = await drafts.get(call_id)
    for field in ("name", "address", "persons", "cutlery", "payment", "comment"):
        val = getattr(body, field)
        if val is not None:
            d["customer_name" if field == "name" else field] = val
    if body.change_from is not None:
        d["change_from"] = body.change_from * 100
    await drafts.save(d)
    return draft_view(d)


class Offered(BaseModel):
    kind: str


@app.post("/api/calls/{call_id}/offered", dependencies=[Depends(auth)])
async def mark_offered(call_id: str, body: Offered) -> dict:
    """Отмечаем, что допродажу такого типа уже предлагали."""
    d = await drafts.get(call_id)
    if body.kind not in d["offered"]:
        d["offered"].append(body.kind)
    await drafts.save(d)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Подтверждение
# ---------------------------------------------------------------------------


@app.post("/api/calls/{call_id}/confirm", dependencies=[Depends(auth)])
async def confirm(call_id: str) -> dict:
    d = await drafts.get(call_id)
    if not d["items"]:
        raise HTTPException(422, "заказ пустой")
    if not d.get("address"):
        return {"error": "нет адреса доставки"}

    conf = await db.get_settings()
    open_now, opens, closes = _is_open(conf)
    if not open_now:
        return {"error": f"кафе закрыто, работаем с {opens} до {closes}"}

    # Цены перечитываем из базы. Всё, что лежало в Redis, считаем ненадёжным.
    async with db.pool.acquire() as c:
        total = 0
        receipt = []
        for it in d["items"]:
            row = await c.fetchrow("SELECT * FROM menu_items WHERE id=$1", it["item_id"])
            if not row or not row["is_available"]:
                return {"error": f"позиция «{it['name']}» закончилась, убери из заказа"}

            sizes = json.loads(row["sizes"]) if isinstance(row["sizes"], str) else row["sizes"]
            price = row["price_cents"]
            if it.get("size"):
                match = next((s for s in sizes if s["name"] == it["size"]), None)
                if not match:
                    return {"error": f"размер «{it['size']}» недоступен"}
                price = match["price_cents"]

            mods_sum = sum(m["price_cents"] for m in it["modifiers"])
            line_total = (price + mods_sum) * it["quantity"]
            total += line_total
            receipt.append(
                {
                    "name": row["name"],
                    "size": it.get("size"),
                    "quantity": it["quantity"],
                    "price_cents": price,
                    "modifiers": it["modifiers"],
                    "total_cents": line_total,
                    # нужен, чтобы заказ ушёл в iiko
                    "external_id": row["external_id"],
                }
            )

        min_order = int(conf.get("min_order", 0)) * 100
        if min_order and total < min_order:
            return {
                "error": "сумма меньше минимальной",
                "минимальная сумма": rub(min_order),
                "сейчас": rub(total),
                "не хватает": rub(min_order - total),
            }

        call = await c.fetchrow("SELECT * FROM calls WHERE id=$1", call_id)
        order = await c.fetchrow(
            """
            INSERT INTO orders (call_id, channel, customer_name, phone, address,
                                persons, cutlery, payment, change_from, comment,
                                total_cents, receipt)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            RETURNING id, number
            """,
            call_id,
            call["channel"] if call else "phone",
            d.get("customer_name") or "",
            call["caller"] if call else "",
            d.get("address") or "",
            d.get("persons"),
            d.get("cutlery"),
            d.get("payment") or "cash",
            d.get("change_from"),
            d.get("comment") or "",
            total,
            json.dumps(receipt, ensure_ascii=False),
        )

    await drafts.drop(call_id)

    # iiko — если не настроена, просто пропускаем, заказ уже в нашей базе
    await iiko.push_order(str(order["id"]))

    return {
        "номер заказа": order["number"],
        "итого": rub(total),
        "доставка": DELIVERY_TEXT,
    }


# ---------------------------------------------------------------------------
# Простая админка: список заказов
# ---------------------------------------------------------------------------


@app.get("/api/settings", dependencies=[Depends(auth)])
async def get_settings() -> dict:
    s = await db.get_settings()
    return {
        "name": s.get("name", "кафе"),
        "operator_name": s.get("operator_name", "оператор"),
        "city": s.get("city", ""),
        "delivery_time": s.get("delivery_time", "в течение часа"),
        "min_order": s.get("min_order", 0),
        "zone_note": s.get("zone_note", ""),
        "opens": s.get("opens", "00:00"),
        "closes": s.get("closes", "23:59"),
    }


def _is_open(s: dict) -> tuple[bool, str, str]:
    if s.get("ignore_hours"):
        return True, s.get("opens", ""), s.get("closes", "")
    tz = ZoneInfo(s.get("timezone", "Europe/Moscow"))
    now = datetime.now(tz).time()
    opens = time.fromisoformat(s.get("opens", "00:00"))
    closes = time.fromisoformat(s.get("closes", "23:59"))
    if opens <= closes:  # noqa: SIM108 - overnight branch reads clearer as a block
        ok = opens <= now <= closes
    else:  # заведение работает через полночь
        ok = now >= opens or now <= closes
    return ok, s.get("opens", ""), s.get("closes", "")


class AddressCheck(BaseModel):
    address: str


@app.post("/api/delivery/check", dependencies=[Depends(auth)])
async def check_delivery(body: AddressCheck) -> dict:
    """Проверка адреса и времени работы одним вызовом.

    Специально сделано просто: сверка по названиям населённых пунктов.
    Точная геозона появится, когда заказчик пришлёт список улиц.
    """
    s = await db.get_settings()
    open_now, opens, closes = _is_open(s)
    if not open_now:
        return {
            "можно доставить": False,
            "причина": f"кафе сейчас закрыто, работаем с {opens} до {closes}",
        }

    zone = [z.lower() for z in s.get("delivery_zone", [])]
    addr = body.address.lower()
    if zone and not any(z in addr for z in zone):
        # город может быть не назван — гость часто диктует только улицу
        looks_like_street = any(
            w in addr for w in ("ул", "улиц", "пр", "просп", "пер", "дом", "д.")
        )
        if not looks_like_street:
            return {
                "можно доставить": False,
                "причина": s.get("zone_note", "адрес вне зоны доставки"),
            }

    return {
        "можно доставить": True,
        "время доставки": s.get("delivery_time", "в течение часа"),
    }


@app.get("/admin/orders", dependencies=[Depends(auth)])
async def list_orders(limit: int = 50) -> dict:
    async with db.pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM orders ORDER BY created_at DESC LIMIT $1", limit)

    orders = []
    for r in rows:
        receipt = json.loads(r["receipt"]) if isinstance(r["receipt"], str) else r["receipt"]
        orders.append(
            {
                "number": r["number"],
                "name": r["customer_name"],
                "phone": r["phone"],
                "address": r["address"],
                "persons": r["persons"],
                "cutlery": r["cutlery"],
                "payment": r["payment"],
                "change_from": rub(r["change_from"]) if r["change_from"] else None,
                "comment": r["comment"],
                "total": rub(r["total_cents"]),
                "status": r["status"],
                "channel": r["channel"],
                "created_at": r["created_at"].isoformat(),
                "items": [
                    {
                        "name": line["name"] + (f" · {line['size']}" if line.get("size") else ""),
                        "qty": line["quantity"],
                        "sum": rub(line["total_cents"]),
                        "modifiers": [m["name"] for m in line.get("modifiers", [])],
                    }
                    for line in receipt
                ],
            }
        )
    return {"orders": orders}


class StatusPatch(BaseModel):
    status: str


ALLOWED = {
    "confirmed": {"sent_to_kitchen", "cancelled"},
    "sent_to_kitchen": {"delivering", "cancelled"},
    "delivering": {"completed", "cancelled"},
}


@app.patch("/admin/orders/{number}", dependencies=[Depends(auth)])
async def update_status(number: int, body: StatusPatch) -> dict:
    async with db.pool.acquire() as c:
        row = await c.fetchrow("SELECT status FROM orders WHERE number=$1", number)
        if not row:
            raise HTTPException(404, "заказ не найден")
        if body.status not in ALLOWED.get(row["status"], set()):
            raise HTTPException(422, f"из «{row['status']}» нельзя перейти в «{body.status}»")
        await c.execute("UPDATE orders SET status=$2 WHERE number=$1", number, body.status)
    return {"ok": True}


@app.get("/admin/menu", dependencies=[Depends(auth)])
async def admin_menu() -> dict:
    async with db.pool.acquire() as c:
        rows = await c.fetch(
            """
            SELECT i.id, i.name, i.price_cents, i.is_available, i.sizes,
                   c.name AS category, c.sort_order
            FROM menu_items i JOIN categories c ON c.key = i.category_key
            ORDER BY c.sort_order, i.name
            """
        )
    out: dict[str, list] = {}
    for r in rows:
        sizes = json.loads(r["sizes"]) if isinstance(r["sizes"], str) else r["sizes"]
        price = min((s["price_cents"] for s in sizes), default=r["price_cents"])
        out.setdefault(r["category"], []).append(
            {
                "id": str(r["id"]),
                "name": r["name"],
                "price": rub(price),
                "from": bool(sizes),
                "available": r["is_available"],
            }
        )
    return {"categories": [{"name": k, "items": v} for k, v in out.items()]}


class Availability(BaseModel):
    available: bool


@app.patch("/admin/menu/{item_id}", dependencies=[Depends(auth)])
async def toggle_item(item_id: str, body: Availability) -> dict:
    """Стоп-лист. Выключенное блюдо бот перестаёт находить и предлагать."""
    async with db.pool.acquire() as c:
        res = await c.execute(
            "UPDATE menu_items SET is_available=$2 WHERE id=$1", item_id, body.available
        )
    if res.endswith("0"):
        raise HTTPException(404, "блюдо не найдено")
    return {"ok": True}


@app.get("/admin/calls", dependencies=[Depends(auth)])
async def admin_calls(limit: int = 30) -> dict:
    """Разговоры целиком. По ним видно, где бот ошибся."""
    async with db.pool.acquire() as c:
        calls = await c.fetch(
            """
            SELECT c.id, c.channel, c.caller, c.status, c.started_at,
                   o.number AS order_number, o.total_cents,
                   (SELECT count(*) FROM transcripts t WHERE t.call_id = c.id) AS lines
            FROM calls c
            LEFT JOIN orders o ON o.call_id = c.id
            WHERE EXISTS (SELECT 1 FROM transcripts t WHERE t.call_id = c.id)
            ORDER BY c.started_at DESC LIMIT $1
            """,
            limit,
        )
        out = []
        for c_ in calls:
            lines = await c.fetch(
                "SELECT speaker, text, at FROM transcripts WHERE call_id=$1 ORDER BY at",
                c_["id"],
            )
            out.append(
                {
                    "channel": c_["channel"],
                    "caller": c_["caller"],
                    "started_at": c_["started_at"].isoformat(),
                    "order_number": c_["order_number"],
                    "total": rub(c_["total_cents"]) if c_["total_cents"] else None,
                    "lines": [{"who": line["speaker"], "text": line["text"]} for line in lines],
                }
            )
    return {"calls": out}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> str:
    return (pathlib.Path(__file__).with_name("admin.html")).read_text(encoding="utf-8")


@app.post("/admin/menu/sync", dependencies=[Depends(auth)])
async def resync() -> dict:
    return {"synced": await db.sync_menu()}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
