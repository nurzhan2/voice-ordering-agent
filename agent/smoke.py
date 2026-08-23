"""
Проверка, что вся цепочка собрана правильно.

    python smoke.py         # быстрая проверка API, без затрат на модель
    python smoke.py --llm   # плюс настоящий разговор с моделью и разбор скрипта

Первый режим прогоняет заказ через те же инструменты, что и бот, и
проверяет главное: модель не может добавить блюдо мимо базы, цены берутся
из Postgres, заказ без адреса не оформляется.

Второй режим прогоняет реальный диалог и проверяет, соблюдён ли скрипт
продаж: нет запрещённых фраз, есть допродажа, спрошен адрес.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

import prompts
from tools import SCHEMAS, Backend, dispatch

load_dotenv()

OK = "\033[38;5;71m✓\033[0m"
NO = "\033[38;5;167m✗\033[0m"
DIM = "\033[2m"
OFF = "\033[0m"

failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global failed
    print(f"  {OK if condition else NO} {name}" + (f" {DIM}{detail}{OFF}" if detail else ""))
    if not condition:
        failed += 1


# ---------------------------------------------------------------------------
# Проверка API
# ---------------------------------------------------------------------------


async def test_api(be: Backend) -> None:
    print("\nНастройки и меню")
    conf = await be.settings()
    check("бэкенд отвечает", "name" in conf, conf.get("name", ""))
    check("минимальная сумма задана", conf.get("min_order", 0) > 0,
          f"{conf.get('min_order')} ₽")

    found = await be.search("пеперони")
    items = found.get("items", [])
    check("поиск по алиасу находит блюдо", bool(items),
          items[0]["name"] if items else "ничего не найдено")
    check("у пиццы есть размеры", bool(items and items[0].get("размеры")))

    for kind in ("main", "garnish", "sauce", "drink", "extra"):
        r = await be.recommend(kind)
        check(f"рекомендации: {kind}", bool(r.get("items")),
              f"{len(r.get('items', []))} шт")

    print("\nСборка заказа")
    call_id = await be.start_call("whatsapp", "+79280000000")

    pizza = items[0]
    size = pizza["размеры"][1]["size"]
    d = await be.add(call_id, pizza["id"], 2, size, [])
    check("позиция добавлена", d.get("позиции") and d["позиции"][0]["количество"] == 2,
          f"{d.get('итого')} ₽")

    bad = await be.add(call_id, "00000000-0000-0000-0000-000000000000", 1, None, [])
    check("выдуманное блюдо отклонено", "error" in bad)

    no_size = await be.add(call_id, pizza["id"], 1, None, [])
    check("без размера просит размер", no_size.get("error") == "нужен размер")

    print("\nДоставка и часы работы")
    good = await be.check_delivery("Махачкала, ул. Ярагского 68")
    check("адрес в зоне принимается", good.get("можно доставить") is True,
          good.get("время доставки", "") or good.get("причина", ""))
    bad_addr = await be.check_delivery("Владивосток, Светланская 1")
    check("адрес вне зоны отклонён", bad_addr.get("можно доставить") is False,
          bad_addr.get("причина", ""))

    print("\nОформление")
    early = await be.confirm(call_id)
    check("без адреса не оформляется", "error" in early, early.get("error", ""))

    await be.customer(call_id, name="Тест", address="Махачкала, ул. Ярагского 68",
                      persons=2, cutlery=True, payment="cash")
    draft = await be.draft(call_id)
    check("данные гостя записаны", draft.get("адрес") is not None)
    check("список недостающего пуст", not draft.get("не хватает"),
          str(draft.get("не хватает")))

    done = await be.confirm(call_id)
    check("заказ оформлен", "номер заказа" in done, str(done))

    if "номер заказа" in done:
        after = await be.draft(call_id)
        check("черновик очищен после оформления", after.get("пусто") is True)


# ---------------------------------------------------------------------------
# Проверка скрипта продаж на живой модели
# ---------------------------------------------------------------------------

BANNED = ["что будете заказывать", "что хотите", "ещё что-нибудь", "еще что-нибудь"]

SCENARIO = [
    "Здравствуйте, это Марат",
    "Хочу что-нибудь мясное",
    "Давайте шашлык из баранины, две порции",
    "Нет, спасибо",
    "Давайте компот два",
    "Всё, больше ничего",
    "Махачкала, улица Ярагского 68, квартира 12",
    "Двое, приборы нужны",
    "Наличными, сдача с трёх тысяч",
    "Да, всё верно, подтверждаю",
]


async def test_script(be: Backend) -> None:
    from openai import AsyncOpenAI

    oai = AsyncOpenAI()
    conf = await be.settings()
    call_id = await be.start_call("whatsapp", "+79281111111")

    messages = [{"role": "system", "content": prompts.build(conf, "whatsapp")}]
    replies: list[str] = []
    used: set[str] = set()

    print("\nРазговор с моделью")
    for line in SCENARIO:
        print(f"  {DIM}гость:{OFF} {line}")
        messages.append({"role": "user", "content": line})

        for _ in range(8):
            r = await oai.chat.completions.create(
                model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
                messages=messages, tools=SCHEMAS, temperature=0.4,
            )
            m = r.choices[0].message
            messages.append(m.model_dump(exclude_none=True))
            if not m.tool_calls:
                replies.append(m.content or "")
                print(f"  {DIM}бот:  {(m.content or '')[:90]}{OFF}")
                break
            for tc in m.tool_calls:
                used.add(tc.function.name)
                res = await dispatch(be, call_id, tc.function.name,
                                     json.loads(tc.function.arguments or "{}"))
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(res, ensure_ascii=False)})

    text = " ".join(replies).lower()

    print("\nСоблюдение скрипта")
    hit = [p for p in BANNED if p in text]
    check("нет запрещённых фраз", not hit, ", ".join(hit))
    check("спросил имя", "обращат" in text or "как вас" in text or "как я могу" in text)
    check("предложил гарнир", "garnish" in _kinds(messages))
    check("предложил соус", "sauce" in _kinds(messages))
    check("предложил напиток", "drink" in _kinds(messages))
    check("проверил адрес", "check_delivery" in used)
    check("оформил заказ", "confirm_order" in used)
    check("не выдумывал блюда", "search_menu" in used or "recommend" in used)


def _kinds(messages: list[dict]) -> set[str]:
    kinds = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") in ("recommend", "mark_offered"):
                try:
                    kinds.add(json.loads(fn.get("arguments") or "{}").get("kind", ""))
                except json.JSONDecodeError:
                    pass
    return kinds


async def main() -> None:
    be = Backend()
    try:
        await test_api(be)
        if "--llm" in sys.argv:
            await test_script(be)
    finally:
        await be.close()

    print()
    if failed:
        print(f"  Провалено проверок: {failed}")
        sys.exit(1)
    print("  Всё в порядке.")


if __name__ == "__main__":
    asyncio.run(main())
