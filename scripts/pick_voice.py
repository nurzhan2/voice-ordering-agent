"""
Подбор голоса для бота.

    python scripts/pick_voice.py            русские голоса из общей библиотеки
    python scripts/pick_voice.py mine       голоса, добавленные в твой аккаунт
    python scripts/pick_voice.py demo ID    озвучить приветствие этим голосом

Последняя команда сохраняет mp3 рядом — можно послушать самому
и отправить заказчику на выбор.
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv("ELEVENLABS_API_KEY", "")
BASE = "https://api.elevenlabs.io/v1"

PHRASE = (
    "Служба доставки кафе Тарелка, оператор Амина. Добрый день! "
    "Подскажите, как я могу к вам обращаться?"
)

# Что ищем в описании голоса. У эндпоинта нет фильтра по языку,
# поэтому отбираем сами по меткам и тексту описания.
RU_HINTS = ("russian", "русск", "ru-", "slavic")


def head(text: str) -> None:
    print(f"\n{text}\n" + "─" * 72)


def get(path: str, **params) -> dict:
    r = httpx.get(
        f"{BASE}{path}",
        headers={"xi-api-key": KEY},
        params={k: v for k, v in params.items() if v is not None},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"Ошибка {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    return r.json()


def looks_russian(v: dict) -> bool:
    labels = v.get("labels") or {}
    haystack = " ".join(
        str(x).lower()
        for x in (
            v.get("name", ""),
            v.get("description", ""),
            labels.get("language", ""),
            labels.get("accent", ""),
            v.get("language", ""),
            " ".join(v.get("verified_languages", []) or [])
            if isinstance(v.get("verified_languages"), list) else "",
        )
    )
    return any(h in haystack for h in RU_HINTS)


def shared() -> None:
    """Женские разговорные голоса из библиотеки, с отбором русских."""
    found: list[dict] = []

    # Забираем несколько страниц: русских в общей выдаче немного.
    for page in range(4):
        data = get(
            "/shared-voices",
            page_size=100,
            gender="female",
            page=page,
        )
        voices = data.get("voices", [])
        found += [v for v in voices if looks_russian(v)]
        if not data.get("has_more"):
            break

    if not found:
        print("Через API русские голоса не отфильтровались.")
        print("Открой библиотеку в браузере, там есть фильтр по языку:")
        print("  https://elevenlabs.io/app/voice-library")
        print("\nВыбери голос → Add to my voices → потом:")
        print("  python scripts/pick_voice.py mine")
        return

    head(f"Похоже на русские женские голоса ({len(found)})")
    for v in found:
        lb = v.get("labels") or {}
        print(f"{v.get('name', '')[:24]:26} {lb.get('age', ''):14} "
              f"{lb.get('use_case', '')[:18]:20}")
        print(f"   id: {v['voice_id']}")
        if v.get("preview_url"):
            print(f"   послушать: {v['preview_url']}")
        print()

    print("Понравился — добавь в кабинете (Add to my voices), затем:")
    print("  python scripts/pick_voice.py mine")


def mine() -> None:
    data = get("/voices")
    voices = data.get("voices", [])
    head(f"Голоса в твоём аккаунте ({len(voices)})")
    for v in voices:
        lb = v.get("labels") or {}
        mark = " ← похож на русский" if looks_russian(v) else ""
        print(f"{v.get('name', '')[:24]:26} {v['voice_id']}"
              f"  {lb.get('use_case', '')}{mark}")
    print("\nПослушать:  python scripts/pick_voice.py demo <voice_id>")
    print("Выбрал — впиши в agent/.env:  ELEVENLABS_VOICE_ID=<id>")


def demo(voice_id: str) -> None:
    """Озвучивает приветствие тем же голосом и моделью, что и бот."""
    r = httpx.post(
        f"{BASE}/text-to-speech/{voice_id}",
        headers={"xi-api-key": KEY, "Content-Type": "application/json"},
        json={
            "text": PHRASE,
            "model_id": os.getenv("TTS_MODEL", "eleven_flash_v2_5"),
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=60,
    )
    if r.status_code >= 400:
        print(f"Ошибка {r.status_code}: {r.text[:300]}")
        return
    name = f"demo_{voice_id[:8]}.mp3"
    with open(name, "wb") as f:
        f.write(r.content)
    print(f"Готово: {name}")
    print("Послушай сам и отправь заказчику — пусть выберет.")


if __name__ == "__main__":
    if not KEY:
        sys.exit("ELEVENLABS_API_KEY не задан в agent/.env")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "shared"
    if cmd == "mine":
        mine()
    elif cmd == "demo":
        if len(sys.argv) < 3:
            sys.exit("Укажи id:  python scripts/pick_voice.py demo <voice_id>")
        demo(sys.argv[2])
    else:
        shared()