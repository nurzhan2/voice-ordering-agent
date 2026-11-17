"""
WhatsApp-бот через Wazzup (API v3).

Тот же скрипт продаж и те же инструменты, что у голосового агента —
отличается только транспорт.

Первый запуск после того, как поднялся HTTPS:
    docker compose exec wa-bot python wa_bot.py setup
Команда сама найдёт channelId и подпишет вебхук.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

import prompts
from tools import SCHEMAS, Backend, dispatch

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wa-bot")

WAZZUP = "https://api.wazzup24.com/v3"
KEY = os.environ["WAZZUP_API_KEY"]
CHANNEL = os.getenv("WAZZUP_CHANNEL_ID", "")
DOMAIN = os.getenv("DOMAIN", "")
SESSION_TTL = 60 * 60 * 6

app = FastAPI()
oai = AsyncOpenAI()
be = Backend()
redis = aioredis.from_url(os.environ["REDIS_URL"].replace("/0", "/1"), decode_responses=True)


# ---------------------------------------------------------------------------
# Сессии переписки
# ---------------------------------------------------------------------------


async def load(chat_id: str) -> dict:
    raw = await redis.get(f"wa:{chat_id}")
    if raw:
        return json.loads(raw)
    call_id = await be.start_call("whatsapp", chat_id)
    return {"call_id": call_id, "history": []}


async def save(chat_id: str, s: dict) -> None:
    s["history"] = s["history"][-24:]
    await redis.setex(f"wa:{chat_id}", SESSION_TTL, json.dumps(s, ensure_ascii=False))


async def reset(chat_id: str) -> None:
    await redis.delete(f"wa:{chat_id}")


# ---------------------------------------------------------------------------
# Генерация ответа
# ---------------------------------------------------------------------------


async def reply_for(chat_id: str, text: str) -> str:
    s = await load(chat_id)
    call_id = s["call_id"]
    conf = await be.settings()
    messages = [
        {"role": "system", "content": prompts.build(conf, "whatsapp")},
        *s["history"],
    ]
    messages.append({"role": "user", "content": text})

    confirmed = False
    for _ in range(8):
        resp = await oai.chat.completions.create(
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            messages=messages,
            tools=SCHEMAS,
            temperature=0.4,
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            s["history"] = messages[1:]
            await save(chat_id, s)
            if confirmed:
                # заказ оформлен — следующее сообщение начнёт новый диалог
                await reset(chat_id)
            return msg.content or "Простите, не понял. Повторите, пожалуйста?"

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = await dispatch(be, call_id, tc.function.name, args)
            if tc.function.name == "confirm_order" and "номер заказа" in result:
                confirmed = True
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    s["history"] = messages[1:]
    await save(chat_id, s)
    return "Извините, не получилось обработать. Напишите, пожалуйста, ещё раз."


# ---------------------------------------------------------------------------
# Wazzup
# ---------------------------------------------------------------------------


async def channel_id() -> str:
    global CHANNEL
    if CHANNEL:
        return CHANNEL
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{WAZZUP}/channels", headers={"Authorization": f"Bearer {KEY}"})
        r.raise_for_status()
        for ch in r.json():
            if ch.get("transport") in ("whatsapp", "wapi"):
                CHANNEL = ch["channelId"]
                return CHANNEL
    raise RuntimeError("не найден WhatsApp-канал в Wazzup")


async def send(chat_id: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{WAZZUP}/message",
            headers={"Authorization": f"Bearer {KEY}"},
            json={
                "channelId": await channel_id(),
                "chatType": "whatsapp",
                "chatId": chat_id,
                "text": text,
                "crmMessageId": str(uuid.uuid4()),
            },
        )
        if r.status_code >= 400:
            logger.error("wazzup send %s: %s", r.status_code, r.text)


async def handle(chat_id: str, text: str) -> None:
    try:
        await send(chat_id, await reply_for(chat_id, text))
    except Exception:
        logger.exception("ошибка обработки для %s", chat_id)
        await send(chat_id, "Извините, техническая неполадка. Напишите ещё раз.")


@app.post("/wazzup/webhook")
async def webhook(request: Request) -> JSONResponse:
    """Wazzup ждёт 200 в течение 30 секунд, поэтому отвечаем сразу,
    а обработку уводим в фон."""
    body = await request.json()

    if body.get("test"):
        return JSONResponse({"ok": True})

    # Временно: смотрим, что Wazzup сообщает о доставке
    if body.get("statuses"):
        logger.info("СТАТУС: %s", json.dumps(body["statuses"], ensure_ascii=False)[:500])

    for m in body.get("messages", []):
        if m.get("isEcho"):
            continue  # наши же исходящие, иначе зациклимся
        if m.get("status") not in (None, "inbound"):
            continue
        if m.get("type") not in (None, "text"):
            asyncio.create_task(
                send(
                    m["chatId"],
                    "Пока принимаю заказы только текстом. Напишите, пожалуйста, "
                    "что хотели бы заказать 🙂",
                )
            )
            continue
        text = (m.get("text") or "").strip()
        if text:
            asyncio.create_task(handle(m["chatId"], text))

    return JSONResponse({"ok": True})


@app.get("/wazzup/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Настройка вебхука
# ---------------------------------------------------------------------------


async def setup() -> None:
    if not DOMAIN:
        raise SystemExit("DOMAIN не задан в .env")
    ch = await channel_id()
    print(f"channelId: {ch}")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(
            f"{WAZZUP}/webhooks",
            headers={"Authorization": f"Bearer {KEY}"},
            json={
                "webhooksUri": f"https://{DOMAIN}/wazzup/webhook",
                "subscriptions": {
                    "messagesAndStatuses": True,
                    "contactsAndDealsCreation": False,
                },
            },
        )
        print("webhook:", r.status_code, r.text or "OK")
        if r.status_code < 300:
            print("\nГотово. Впиши в .env:  WAZZUP_CHANNEL_ID=" + ch)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        asyncio.run(setup())
