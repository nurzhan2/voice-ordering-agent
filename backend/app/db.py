"""Пул подключений к Postgres, миграция и загрузка меню из menu.yaml."""

from __future__ import annotations

import json
import logging
import os
import pathlib

import asyncpg
import yaml

logger = logging.getLogger("backend.db")

pool: asyncpg.Pool | None = None

SCHEMA = pathlib.Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
MENU_FILE = pathlib.Path(os.getenv("MENU_FILE", "/app/menu.yaml"))


async def connect() -> None:
    global pool
    pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"], min_size=2, max_size=10
    )
    async with pool.acquire() as c:
        await c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await c.execute(SCHEMA)

        # Убираем дубли, если база создавалась до появления уникального
        # индекса, иначе индекс не создастся.
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
    logger.info("database ready")


async def disconnect() -> None:
    if pool:
        await pool.close()


def to_cents(value: float | int) -> int:
    """В YAML цены в рублях, в базе — в копейках."""
    return int(round(float(value) * 100))


async def sync_menu() -> int:
    """Заливает menu.yaml в базу. Идемпотентно: совпадение по названию.

    Меню правится в menu.yaml и применяется через `make menu`.
    Так не нужна админка, и меню лежит в гите вместе с кодом.
    """
    if not MENU_FILE.exists():
        logger.warning("menu.yaml не найден, пропускаю синхронизацию")
        return 0

    data = yaml.safe_load(MENU_FILE.read_text(encoding="utf-8")) or {}
    cats = data.get("categories", [])
    items = data.get("items", [])
    conf = data.get("settings", {})

    async with pool.acquire() as c, c.transaction():
        await c.execute(
            "UPDATE settings SET data = $1::jsonb WHERE id = 1",
            json.dumps(conf, ensure_ascii=False),
        )

        for i, cat in enumerate(cats):
            await c.execute(
                """
                INSERT INTO categories (key, name, tier, addon_kind, sort_order)
                VALUES ($1,$2,$3,$4,$5)
                ON CONFLICT (key) DO UPDATE
                   SET name=$2, tier=$3, addon_kind=$4, sort_order=$5
                """,
                cat["key"],
                cat["name"],
                int(cat.get("tier", 2)),
                cat.get("addon_kind"),
                i,
            )

        # Всё, чего нет в yaml, скрываем — но не удаляем, чтобы не поехали заказы
        names = [it["name"] for it in items]
        await c.execute(
            "UPDATE menu_items SET is_available=false WHERE NOT (name = ANY($1::text[]))",
            names,
        )

        for it in items:
            sizes = [
                {"name": s["name"], "price_cents": to_cents(s["price"])}
                for s in it.get("sizes", [])
            ]
            mods = [
                {"name": m["name"], "price_cents": to_cents(m.get("price", 0))}
                for m in it.get("modifiers", [])
            ]
            await c.execute(
                """
                INSERT INTO menu_items
                    (category_key, name, description, price_cents, is_available,
                     is_hit, aliases, sizes, modifiers, external_id)
                VALUES ($1,$2,$3,$4,true,$5,$6,$7,$8,$9)
                ON CONFLICT (lower(name)) DO UPDATE SET
                    category_key = EXCLUDED.category_key,
                    description  = EXCLUDED.description,
                    price_cents  = EXCLUDED.price_cents,
                    is_available = true,
                    is_hit       = EXCLUDED.is_hit,
                    aliases      = EXCLUDED.aliases,
                    sizes        = EXCLUDED.sizes,
                    modifiers    = EXCLUDED.modifiers,
                    external_id  = COALESCE(EXCLUDED.external_id, menu_items.external_id)
                """,
                it["category"],
                it["name"],
                it.get("description", ""),
                to_cents(it.get("price", 0)),
                bool(it.get("hit", False)),
                json.dumps(it.get("aliases", []), ensure_ascii=False),
                json.dumps(sizes, ensure_ascii=False),
                json.dumps(mods, ensure_ascii=False),
                it.get("external_id"),
            )

    logger.info("меню синхронизировано: %d категорий, %d позиций", len(cats), len(items))
    return len(items)


async def get_settings() -> dict:
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT data FROM settings WHERE id = 1")
    if not row:
        return {}
    return json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
