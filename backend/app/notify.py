"""
Уведомление о новом заказе.

Пока iiko не подключена, заказ виден только в панели. Этого мало: никто
не сидит и не смотрит в экран постоянно. Поэтому после оформления
отправляем состав заказа менеджеру в WhatsApp через Wazzup.

Номер задаётся в menu.yaml -> settings -> manager_whatsapp.
Не задан — просто пропускаем, ничего не ломается.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import httpx

logger = logging.getLogger("backend.notify")

WAZZUP = "https://api.wazzup24.com/v3"
KEY = os.getenv("WAZZUP_API_KEY", "")
CHANNEL = os.getenv("WAZZUP_CHANNEL_ID", "")

PAY = {"cash": "наличными", "transfer": "переводом", "card": "картой курьеру"}


def _rub(cents: int | None) -> int:
    return int(round((cents or 0) / 100))


def build_text(order: dict, receipt: list[dict], settings: dict) -> str:
    lines = [f"🔔 Новый заказ №{order['number']}", ""]

    for it in receipt:
        name = it["name"] + (f" · {it['size']}" if it.get("size") else "")
        lines.append(f"{it['quantity']}× {name} — {_rub(it['total_cents'])} ₽")
        for m in it.get("modifiers", []):
            lines.append(f"    + {m['name']}")

    lines += [
        "",
        f"Итого: {_rub(order['total_cents'])} ₽",
        "",
        f"Гость: {order['customer_name'] or '—'}",
        f"Телефон: {order['phone'] or '—'}",
        f"Адрес: {order['address'] or '—'}",
    ]

    if order.get("persons"):
        lines.append(f"Приборы: {order['persons']} перс.")
    elif order.get("cutlery") is False:
        lines.append("Приборы: не нужны")

    pay = PAY.get(order.get("payment", ""), order.get("payment", ""))
    if order.get("change_from"):
        pay += f", сдача с {_rub(order['change_from'])} ₽"
    lines.append(f"Оплата: {pay}")

    if order.get("comment"):
        lines.append(f"Просьба: {order['comment']}")

    lines += ["", f"Доставка {settings.get('delivery_time', 'в течение часа')}"]
    return "\n".join(lines)


async def send_order(order: dict, receipt: list[dict], settings: dict) -> None:
    """Ошибка отправки не должна ронять заказ — он уже принят и сохранён."""
    to = (settings or {}).get("manager_whatsapp", "").strip()
    if not (to and KEY and CHANNEL):
        logger.info(
            "уведомление не отправлено: не настроен номер менеджера или Wazzup"
        )
        return

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{WAZZUP}/message",
                headers={"Authorization": f"Bearer {KEY}"},
                json={
                    "channelId": CHANNEL,
                    "chatType": "whatsapp",
                    "chatId": to.lstrip("+"),
                    "text": build_text(order, receipt, settings),
                    "crmMessageId": str(uuid.uuid4()),
                },
            )
            if r.status_code >= 400:
                logger.error("Wazzup отклонил уведомление %s: %s", r.status_code, r.text)
            else:
                logger.info("заказ №%s отправлен менеджеру", order["number"])
    except Exception:
        logger.exception("не удалось отправить уведомление о заказе")
