"""
Слой хранения. Работает и на SQLite, и на Postgres.

По умолчанию — SQLite: файл рядом с проектом, ничего ставить не надо.
Достаточно Python. Это режим для отладки и демонстрации.

Если в DATABASE_URL указан postgresql:// — используется он. Это боевой
режим: несколько процессов, надёжные транзакции, полнотекстовый поиск.

Переключение — одна переменная окружения, код одинаковый.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("backend.store")

DSN = os.getenv("DATABASE_URL", "").strip()
IS_PG = DSN.startswith("postgres")

_pg_pool = None
_lite = None

LITE_PATH = os.getenv("SQLITE_PATH", str(pathlib.Path.cwd() / "tarelka.db"))


# ---------------------------------------------------------------------------
# Подключение
# ---------------------------------------------------------------------------


async def connect() -> None:
    global _pg_pool, _lite

    if IS_PG:
        import asyncpg

        _pg_pool = await asyncpg.create_pool(DSN, min_size=2, max_size=10)
        schema = (pathlib.Path(__file__).with_name("schema.sql")).read_text("utf-8")
        async with _pg_pool.acquire() as c:
            await c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await c.execute(schema)
            await c.execute(
                """
                DELETE FROM menu_items a USING menu_items b
                WHERE lower(a.name) = lower(b.name) AND a.created_at > b.created_at
                """
            )
            await c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_item_name "
                "ON menu_items (lower(name))"
            )
        logger.info("Postgres подключён")
        return

    import aiosqlite

    _lite = await aiosqlite.connect(LITE_PATH)
    _lite.row_factory = aiosqlite.Row
    await _lite.execute("PRAGMA journal_mode=WAL")
    await _lite.execute("PRAGMA foreign_keys=ON")
    schema = (pathlib.Path(__file__).with_name("schema_lite.sql")).read_text("utf-8")
    await _lite.executescript(schema)
    await _lite.commit()
    logger.info("SQLite подключён: %s", LITE_PATH)


async def disconnect() -> None:
    if _pg_pool:
        await _pg_pool.close()
    if _lite:
        await _lite.close()


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

_PARAM = re.compile(r"\$(\d+)")


def _to_lite(sql: str) -> str:
    """$1, $2 -> ?, ? плюс замена того, чего нет в SQLite."""
    sql = _PARAM.sub("?", sql)
    sql = sql.replace("now()", "CURRENT_TIMESTAMP")
    sql = sql.replace("::jsonb", "").replace("::text[]", "")
    return sql


def _clean(args: tuple) -> tuple:
    out = []
    for a in args:
        if isinstance(a, (dict, list)):
            out.append(json.dumps(a, ensure_ascii=False))
        elif isinstance(a, bool):
            out.append(1 if a else 0)
        elif isinstance(a, uuid.UUID):
            out.append(str(a))
        else:
            out.append(a)
    return tuple(out)


async def fetch(sql: str, *args) -> list[dict]:
    if IS_PG:
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(sql, *args)
        return [dict(r) for r in rows]
    cur = await _lite.execute(_to_lite(sql), _clean(args))
    rows = await cur.fetchall()
    await cur.close()
    return [dict(r) for r in rows]


async def fetchrow(sql: str, *args) -> dict | None:
    rows = await fetch(sql, *args)
    return rows[0] if rows else None


async def fetchval(sql: str, *args):
    row = await fetchrow(sql, *args)
    return list(row.values())[0] if row else None


async def execute(sql: str, *args) -> int:
    """Возвращает количество затронутых строк."""
    if IS_PG:
        async with _pg_pool.acquire() as c:
            res = await c.execute(sql, *args)
        try:
            return int(res.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            return 0
    cur = await _lite.execute(_to_lite(sql), _clean(args))
    await _lite.commit()
    n = cur.rowcount
    await cur.close()
    return max(n, 0)


async def insert_returning(sql: str, *args) -> dict:
    """INSERT ... RETURNING. В SQLite поддерживается с версии 3.35."""
    if IS_PG:
        async with _pg_pool.acquire() as c:
            row = await c.fetchrow(sql, *args)
        return dict(row)
    cur = await _lite.execute(_to_lite(sql), _clean(args))
    row = await cur.fetchone()
    await _lite.commit()
    await cur.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Мелкие различия диалектов
# ---------------------------------------------------------------------------


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


def loads(value):
    """JSONB в Postgres приходит объектом, в SQLite — строкой."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def as_bool(value) -> bool:
    return bool(value)


def as_dt(value) -> datetime:
    """SQLite отдаёт время строкой."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return now()
    return value or now()
