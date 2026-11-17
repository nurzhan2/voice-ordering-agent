"""Fixtures: the API runs against in-memory fakes of PostgreSQL and Redis.

The fakes answer only the queries the code under test actually sends, and fail
loudly on anything else, so a test can't pass by accident through a query
nobody expected.
"""

from __future__ import annotations

import copy
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "agent")]
os.environ["BACKEND_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, drafts, iiko, main  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}


def dish(id_: str, name: str, price: int, **extra) -> dict:
    row = {
        "id": id_,
        "name": name,
        "description": "",
        "price_cents": price * 100,
        "sizes": [],
        "modifiers": [],
        "is_hit": False,
        "is_available": True,
        "category_key": "hot",
        "external_id": None,
    }
    row.update(extra)
    return row


class FakeConnection:
    def __init__(self, menu: dict[str, dict]) -> None:
        self.menu = menu
        self.orders: list[tuple] = []

    async def fetchrow(self, sql: str, *args):
        if "FROM menu_items WHERE id=$1" in sql:
            row = self.menu.get(args[0])
            if row is None or ("is_available" in sql and not row["is_available"]):
                return None
            return row
        if "FROM calls WHERE id=$1" in sql:
            return {"channel": "phone", "caller": "+70000000000"}
        if "INSERT INTO orders" in sql:
            self.orders.append(args)
            return {"id": "order-1", "number": 100 + len(self.orders)}
        raise AssertionError(f"unexpected query: {sql[:60]}")

    async def fetch(self, sql: str, *args):
        raise AssertionError(f"unexpected query: {sql[:60]}")


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def menu() -> dict[str, dict]:
    return {
        "kebab": dish("kebab", "Шашлык из баранины", 890),
        "fries": dish("fries", "Картофель фри", 225),
        "pizza": dish(
            "pizza",
            "Пицца Пепперони",
            0,
            sizes=[
                {"name": "25 см", "price_cents": 55000},
                {"name": "33 см", "price_cents": 75000},
            ],
        ),
    }


@pytest.fixture
def settings() -> dict:
    # ignore_hours keeps tests independent of the wall clock;
    # the hours logic itself is covered in test_hours.py.
    return {
        "ignore_hours": True,
        "min_order": 0,
        "delivery_zone": ["Махачкала", "Каспийск"],
        "zone_note": "Возим по Махачкале и Каспийску.",
        "delivery_time": "в течение часа",
    }


@pytest.fixture
def store(monkeypatch) -> dict[str, dict]:
    """In-memory replacement for the Redis draft store."""
    data: dict[str, dict] = {}

    async def get(call_id: str) -> dict:
        return copy.deepcopy(data.get(call_id) or drafts.empty(call_id))

    async def save(draft: dict) -> None:
        data[draft["call_id"]] = copy.deepcopy(draft)

    async def drop(call_id: str) -> None:
        data.pop(call_id, None)

    monkeypatch.setattr(drafts, "get", get)
    monkeypatch.setattr(drafts, "save", save)
    monkeypatch.setattr(drafts, "drop", drop)
    return data


@pytest.fixture
def conn(monkeypatch, menu, settings, store) -> FakeConnection:
    connection = FakeConnection(menu)
    monkeypatch.setattr(db, "pool", FakePool(connection))

    async def get_settings() -> dict:
        return settings

    async def push_order(order_id: str) -> None:
        return None

    monkeypatch.setattr(db, "get_settings", get_settings)
    monkeypatch.setattr(iiko, "push_order", push_order)
    return connection


@pytest.fixture
def api(conn) -> TestClient:
    # No `with` block: lifespan would try to reach real PostgreSQL and Redis.
    return TestClient(main.app, headers=AUTH)
