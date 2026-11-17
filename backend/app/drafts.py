"""Черновик заказа в Redis. Живёт только во время разговора."""

from __future__ import annotations

import json
import os

import redis.asyncio as aioredis

TTL = 60 * 60 * 4

redis: aioredis.Redis | None = None


async def connect() -> None:
    global redis
    redis = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await redis.ping()


async def disconnect() -> None:
    if redis:
        await redis.aclose()


def empty(call_id: str) -> dict:
    return {
        "call_id": call_id,
        "items": [],
        "customer_name": None,
        "address": None,
        "persons": None,
        "cutlery": None,
        "payment": None,
        "change_from": None,
        "comment": "",
        # что уже предложили — чтобы бот не спрашивал про соус три раза
        "offered": [],
    }


def _key(call_id: str) -> str:
    return f"draft:{call_id}"


async def get(call_id: str) -> dict:
    raw = await redis.get(_key(call_id))
    return json.loads(raw) if raw else empty(call_id)


async def save(draft: dict) -> None:
    await redis.setex(_key(draft["call_id"]), TTL, json.dumps(draft, ensure_ascii=False))


async def drop(call_id: str) -> None:
    await redis.delete(_key(call_id))


def total_cents(draft: dict) -> int:
    return sum(
        (it["price_cents"] + sum(m["price_cents"] for m in it.get("modifiers", [])))
        * it["quantity"]
        for it in draft["items"]
    )
