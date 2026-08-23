"""
Импорт номенклатуры из iiko в menu.yaml.

    docker compose exec backend python -m app.import_iiko > menu_iiko.yaml

Дальше файл нужно просмотреть глазами: проставить категориям tier,
добавить алиасы под голос и убрать лишние позиции. Полностью
автоматически это делать нельзя — бот должен понимать, что предлагать
первым, а из iiko такой информации не приходит.
"""

from __future__ import annotations

import asyncio
import re
import sys

from . import iiko

# Во что складывать категории iiko. Ключ — кусок названия в нижнем регистре.
GUESS = {
    "сет": ("sets", 1),
    "комбо": ("sets", 1),
    "пицц": ("pizza", 1),
    "мангал": ("mangal", 1),
    "шашлык": ("mangal", 1),
    "гриль": ("mangal", 1),
    "горяч": ("hot", 1),
    "втор": ("hot", 1),
    "ролл": ("rolls", 2),
    "суши": ("rolls", 2),
    "салат": ("salads", 2),
    "десерт": ("desserts", 2),
    "выпеч": ("desserts", 2),
    "гарнир": ("garnish", 3),
    "соус": ("sauces", 3),
    "напит": ("drinks", 3),
    "вода": ("drinks", 3),
    "чай": ("drinks", 3),
    "кофе": ("drinks", 3),
}

ADDON = {"garnish": "garnish", "sauces": "sauce", "drinks": "drink"}


def classify(group_name: str) -> tuple[str, int]:
    low = (group_name or "").lower()
    for key, (cat, tier) in GUESS.items():
        if key in low:
            return cat, tier
    return "hot", 2


def aliases_for(name: str) -> list[str]:
    """Заготовка алиасов: нижний регистр, без скобок, отдельные слова.

    Это только черновик. Реальные варианты произношения нужно дописать
    руками — именно от них зависит, поймёт бот гостя или нет.
    """
    clean = re.sub(r"\(.*?\)", "", name).strip().lower()
    out = {clean}
    words = [w for w in re.split(r"[\s,«»\"-]+", clean) if len(w) > 3]
    if words:
        out.add(words[0])
    return sorted(out - {""})


def esc(s: str) -> str:
    return '"' + str(s).replace('"', "'") + '"'


async def main() -> None:
    if not iiko.enabled:
        sys.exit("IIKO_API_LOGIN и IIKO_ORGANIZATION_ID не заданы в .env")

    products = await iiko.fetch_menu()
    groups = {g["id"]: g.get("name", "") for g in await iiko.fetch_groups()}

    cats: dict[str, tuple[str, int]] = {}
    lines: list[str] = []

    for p in products:
        if p.get("type") not in (None, "Dish", "Good"):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        price = 0
        for size in p.get("sizePrices", []):
            price = (size.get("price") or {}).get("currentPrice") or 0
            if price:
                break
        if not price:
            continue

        cat, tier = classify(groups.get(p.get("parentGroup"), ""))
        cats[cat] = (groups.get(p.get("parentGroup"), cat), tier)

        lines.append(f"  - name: {esc(name)}")
        lines.append(f"    category: {cat}")
        lines.append(f"    price: {int(price)}")
        lines.append(f"    external_id: {esc(p['id'])}")
        al = aliases_for(name)
        if al:
            lines.append("    aliases: [" + ", ".join(esc(a) for a in al) + "]")
        lines.append("")

    print("# Выгружено из iiko. ОБЯЗАТЕЛЬНО просмотреть перед применением:")
    print("#  1. проверить tier у категорий")
    print("#  2. дописать алиасы — как гость произносит блюдо вслух")
    print("#  3. удалить позиции, которых нет в доставке")
    print()
    print("categories:")
    for key, (title, tier) in sorted(cats.items(), key=lambda x: x[1][1]):
        addon = f", addon_kind: {ADDON[key]}" if key in ADDON else ""
        print(f"  - {{key: {key}, name: {esc(title)}, tier: {tier}{addon}}}")
    print()
    print("items:")
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
