"""
Интеграция с iiko Cloud API.

Пока IIKO_API_LOGIN пустой — всё молча выключено, заказы просто живут в нашей
базе. Как только клиент даст ключ, достаточно вписать его в .env и перезапустить.
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

logger = logging.getLogger("backend.iiko")

BASE = "https://api-ru.iiko.services/api/1"
API_LOGIN = os.getenv("IIKO_API_LOGIN", "")
ORG_ID = os.getenv("IIKO_ORGANIZATION_ID", "")
TERMINAL_GROUP = os.getenv("IIKO_TERMINAL_GROUP_ID", "")

_token: str | None = None
_token_at: float = 0.0

enabled = bool(API_LOGIN and ORG_ID)


async def _get_token(client: httpx.AsyncClient) -> str:
    """Токен живёт час, берём с запасом на 50 минут."""
    global _token, _token_at
    if _token and time.time() - _token_at < 50 * 60:
        return _token
    r = await client.post(f"{BASE}/access_token", json={"apiLogin": API_LOGIN})
    r.raise_for_status()
    _token = r.json()["token"]
    _token_at = time.time()
    return _token


async def fetch_menu() -> list[dict]:
    """Выгрузка номенклатуры. Пригодится, чтобы не забивать menu.yaml руками."""
    if not enabled:
        return []
    async with httpx.AsyncClient(timeout=60) as c:
        token = await _get_token(c)
        r = await c.post(
            f"{BASE}/nomenclature",
            headers={"Authorization": f"Bearer {token}"},
            json={"organizationId": ORG_ID},
        )
        r.raise_for_status()
        return r.json().get("products", [])


async def fetch_groups() -> list[dict]:
    """Группы меню — по ним определяем категорию блюда."""
    if not enabled:
        return []
    async with httpx.AsyncClient(timeout=60) as c:
        token = await _get_token(c)
        r = await c.post(
            f"{BASE}/nomenclature",
            headers={"Authorization": f"Bearer {token}"},
            json={"organizationId": ORG_ID},
        )
        r.raise_for_status()
        return r.json().get("groups", [])


async def push_order(order_id: str) -> None:
    """Отправка подтверждённого заказа в iiko.

    Ошибка здесь не должна ронять заказ: он уже принят и лежит в нашей базе.
    Логируем и идём дальше — оператор увидит заказ в админке.
    """
    if not enabled:
        return
    from . import db

    async with db.pool.acquire() as c:
        o = await c.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    if not o:
        return

    receipt = json.loads(o["receipt"]) if isinstance(o["receipt"], str) else o["receipt"]
    payload = {
        "organizationId": ORG_ID,
        "terminalGroupId": TERMINAL_GROUP or None,
        "order": {
            "phone": o["phone"] or "+70000000000",
            "customer": {"name": o["customer_name"] or "Гость"},
            "deliveryPoint": {"address": {"street": {"name": o["address"]}}},
            "comment": o["comment"],
            "items": [
                {
                    "productId": line.get("external_id"),
                    "type": "Product",
                    "amount": line["quantity"],
                }
                for line in receipt
                if line.get("external_id")
            ],
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _get_token(client)
            r = await client.post(
                f"{BASE}/deliveries/create",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            r.raise_for_status()
            iiko_id = r.json().get("orderInfo", {}).get("id")
        async with db.pool.acquire() as c:
            await c.execute("UPDATE orders SET iiko_order_id=$2 WHERE id=$1", order_id, iiko_id)
        logger.info("заказ %s ушёл в iiko: %s", o["number"], iiko_id)
    except Exception:
        logger.exception("не удалось отправить заказ %s в iiko", o["number"])
