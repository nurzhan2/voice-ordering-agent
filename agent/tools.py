"""
Инструменты агента. Один набор на два канала — голос и WhatsApp работают
через одни и те же функции, поэтому логика продаж не разъезжается.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("agent.tools")


class Backend:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        tok = token if token is not None else os.getenv("BACKEND_TOKEN", "")
        # Пустой токен нельзя класть в заголовок: httpx отвергает "Bearer ".
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        self.c = httpx.AsyncClient(
            base_url=(base_url or os.environ["BACKEND_URL"]).rstrip("/"),
            headers=headers,
            timeout=4.0,
        )

    async def close(self) -> None:
        await self.c.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self.c.request(method, path, **kw)
        except httpx.HTTPError as e:
            logger.warning("backend unreachable: %s", e)
            return {"error": "сервис временно недоступен"}
        if r.status_code >= 400:
            try:
                return {"error": r.json().get("detail", r.text)}
            except Exception:
                return {"error": r.text}
        return r.json() if r.content else {}

    # -- звонок --
    async def start_call(self, channel: str, caller: str, room: str = "") -> str:
        d = await self._call(
            "POST", "/api/calls",
            json={"channel": channel, "caller": caller, "room": room},
        )
        if "call_id" not in d:
            raise RuntimeError(
                f"бэкенд не завёл звонок: {d.get('error', d)}. "
                "Проверь, что он запущен и BACKEND_URL указывает на него."
            )
        return d["call_id"]

    async def finish_call(self, call_id: str) -> None:
        await self._call("POST", f"/api/calls/{call_id}/finish")

    async def log(self, call_id: str, speaker: str, text: str) -> None:
        await self._call(
            "POST", f"/api/calls/{call_id}/transcript",
            json={"speaker": speaker, "text": text},
        )

    # -- инструменты --
    async def search(self, q: str) -> dict:
        return await self._call("GET", "/api/menu/search", params={"q": q})

    async def recommend(self, kind: str, call_id: str = "") -> dict:
        return await self._call(
            "GET", "/api/menu/recommend",
            params={"kind": kind, "call_id": call_id},
        )

    async def add(self, call_id: str, item_id: str, quantity: int,
                  size: str | None, modifiers: list[str]) -> dict:
        return await self._call(
            "POST", f"/api/calls/{call_id}/items",
            json={"item_id": item_id, "quantity": quantity,
                  "size": size, "modifiers": modifiers or []},
        )

    async def remove(self, call_id: str, line_id: int) -> dict:
        return await self._call("DELETE", f"/api/calls/{call_id}/items/{line_id}")

    async def qty(self, call_id: str, line_id: int, quantity: int) -> dict:
        return await self._call(
            "PATCH", f"/api/calls/{call_id}/items/{line_id}",
            json={"quantity": quantity},
        )

    async def draft(self, call_id: str) -> dict:
        return await self._call("GET", f"/api/calls/{call_id}/draft")

    async def customer(self, call_id: str, **fields) -> dict:
        return await self._call(
            "PATCH", f"/api/calls/{call_id}/customer",
            json={k: v for k, v in fields.items() if v is not None},
        )

    async def offered(self, call_id: str, kind: str) -> dict:
        return await self._call(
            "POST", f"/api/calls/{call_id}/offered", json={"kind": kind}
        )

    async def confirm(self, call_id: str) -> dict:
        return await self._call("POST", f"/api/calls/{call_id}/confirm")

    async def check_delivery(self, address: str) -> dict:
        return await self._call(
            "POST", "/api/delivery/check", json={"address": address}
        )

    async def settings(self) -> dict:
        return await self._call("GET", "/api/settings")


# ---------------------------------------------------------------------------
# Схемы инструментов для OpenAI (используются WhatsApp-ботом).
# Голосовой агент описывает те же функции декораторами LiveKit.
# ---------------------------------------------------------------------------

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_menu",
            "description": "Найти блюдо в меню по названию или его части.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend",
            "description": (
                "Что предложить гостю. main — основное (сеты, пицца, мангал, горячее); "
                "extra — допродажа (роллы, салаты, десерты); "
                "garnish — гарниры; sauce — соусы; drink — напитки."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["main", "extra", "garnish", "sauce", "drink"],
                    }
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_item",
            "description": "Добавить позицию в заказ. item_id только из search_menu или recommend.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "size": {"type": "string"},
                    "modifiers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_item",
            "description": "Убрать позицию из заказа по её line_id из get_order.",
            "parameters": {
                "type": "object",
                "properties": {"line_id": {"type": "integer"}},
                "required": ["line_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_quantity",
            "description": "Изменить количество позиции. 0 — удалить.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "integer"},
                    "quantity": {"type": "integer"},
                },
                "required": ["line_id", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Текущий состав заказа, сумма и чего ещё не хватает.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_customer",
            "description": "Записать данные гостя: имя, адрес, персон, приборы, оплату.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "address": {"type": "string"},
                    "persons": {"type": "integer"},
                    "cutlery": {"type": "boolean"},
                    "payment": {"type": "string", "enum": ["cash", "transfer", "card"]},
                    "change_from": {"type": "integer", "description": "сдача с какой суммы, в рублях"},
                    "comment": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_offered",
            "description": "Отметить, что допродажу такого типа уже предлагали.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["garnish", "sauce", "drink", "extra"],
                    }
                },
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_delivery",
            "description": (
                "Проверить, доставляем ли по этому адресу и открыто ли кафе. "
                "Вызывать сразу, как гость назвал адрес, до подтверждения заказа."
            ),
            "parameters": {
                "type": "object",
                "properties": {"address": {"type": "string"}},
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_order",
            "description": "Оформить заказ. Только после явного согласия гостя.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def dispatch(be: Backend, call_id: str, name: str, args: dict) -> dict:
    """Единая точка выполнения инструмента — общая для обоих каналов."""
    if name == "search_menu":
        return await be.search(args["query"])
    if name == "recommend":
        return await be.recommend(args["kind"], call_id)
    if name == "add_item":
        return await be.add(
            call_id, args["item_id"], int(args.get("quantity", 1)),
            args.get("size"), args.get("modifiers", []),
        )
    if name == "remove_item":
        return await be.remove(call_id, int(args["line_id"]))
    if name == "set_quantity":
        return await be.qty(call_id, int(args["line_id"]), int(args["quantity"]))
    if name == "get_order":
        return await be.draft(call_id)
    if name == "set_customer":
        return await be.customer(call_id, **args)
    if name == "mark_offered":
        return await be.offered(call_id, args["kind"])
    if name == "check_delivery":
        return await be.check_delivery(args["address"])
    if name == "confirm_order":
        return await be.confirm(call_id)
    return {"error": "неизвестный инструмент"}
