"""
Демо-заказы для показа панели.

    docker compose exec backend python -m app.seed_demo        # добавить
    docker compose exec backend python -m app.seed_demo clear  # убрать

ВАЖНО: это выдуманные заказы для демонстрации интерфейса. Перед боевым
запуском обязательно очистить, иначе они смешаются с реальными.
Все демо-заказы имеют номера от 9001 — по ним же и удаляются.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from . import db

DEMO_FROM = 9001

ORDERS = [
    {
        "min_ago": 2,
        "name": "Марат",
        "phone": "+7 928 844-55-12",
        "address": "Махачкала, ул. Ярагского 68, кв. 12",
        "persons": 3,
        "payment": "cash",
        "change_from": 300000,
        "comment": "Позвонить за 5 минут до приезда",
        "channel": "phone",
        "status": "confirmed",
        "items": [
            ("Шашлык из баранины", None, 2, 89000),
            ("Картофель по-деревенски", None, 2, 22000),
            ("Чесночный соус", None, 2, 6000),
            ("Компот домашний", None, 2, 15000),
        ],
    },
    {
        "min_ago": 18,
        "name": "Зарема",
        "phone": "+7 964 001-22-88",
        "address": "пр. Шамиля 44, подъезд 2, домофон 12",
        "persons": 2,
        "payment": "transfer",
        "change_from": None,
        "comment": "",
        "channel": "whatsapp",
        "status": "sent_to_kitchen",
        "items": [
            ("Пепперони", "33 см", 1, 75000),
            ("Хинкал дагестанский", None, 1, 55000),
            ("Лимонад", None, 2, 19000),
        ],
    },
    {
        "min_ago": 35,
        "name": "Ахмед",
        "phone": "+7 928 055-11-77",
        "address": "ул. Гагарина 5, офис 3",
        "persons": 6,
        "payment": "cash",
        "change_from": 500000,
        "comment": "Заказ на офис, приборы обязательно",
        "channel": "phone",
        "status": "delivering",
        "items": [
            ("Сет Большая компания", None, 1, 290000),
            ("Люля-кебаб", None, 2, 52000),
            ("Барбекю", None, 1, 6000),
            ("Кока-кола 1 л", None, 1, 18000),
        ],
    },
    {
        "min_ago": 80,
        "name": "Патимат",
        "phone": "+7 928 990-03-21",
        "address": "ул. Коркмасова 21",
        "persons": 2,
        "payment": "card",
        "change_from": None,
        "comment": "",
        "channel": "whatsapp",
        "status": "completed",
        "items": [
            ("Филадельфия", None, 1, 59000),
            ("Чизкейк", None, 1, 32000),
            ("Вода без газа 0.5", None, 1, 8000),
        ],
    },
]


async def seed() -> None:
    await db.connect()
    now = datetime.now(timezone.utc)

    async with db.pool.acquire() as c, c.transaction():
        await c.execute("DELETE FROM orders WHERE number >= $1", DEMO_FROM)
        await c.execute(
            "SELECT setval(pg_get_serial_sequence('orders','number'), $1, false)",
            DEMO_FROM,
        )

        for o in ORDERS:
            receipt = [
                {
                    "name": name,
                    "size": size,
                    "quantity": qty,
                    "price_cents": price,
                    "modifiers": [],
                    "total_cents": price * qty,
                }
                for name, size, qty, price in o["items"]
            ]
            total = sum(line["total_cents"] for line in receipt)
            await c.execute(
                """
                INSERT INTO orders (channel, customer_name, phone, address, persons,
                                    cutlery, payment, change_from, comment,
                                    total_cents, status, receipt, created_at)
                VALUES ($1,$2,$3,$4,$5,true,$6,$7,$8,$9,$10,$11,$12)
                """,
                o["channel"], o["name"], o["phone"], o["address"], o["persons"],
                o["payment"], o["change_from"], o["comment"], total, o["status"],
                json.dumps(receipt, ensure_ascii=False),
                now - timedelta(minutes=o["min_ago"]),
            )

    print(f"Добавлено демо-заказов: {len(ORDERS)} (номера от {DEMO_FROM})")
    print("Убрать:  docker compose exec backend python -m app.seed_demo clear")
    await db.disconnect()


async def clear() -> None:
    await db.connect()
    async with db.pool.acquire() as c:
        n = await c.execute("DELETE FROM orders WHERE number >= $1", DEMO_FROM)
    print("Демо-заказы удалены:", n)
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(clear() if len(sys.argv) > 1 and sys.argv[1] == "clear" else seed())
