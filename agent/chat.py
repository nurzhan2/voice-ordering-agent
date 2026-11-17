"""
Разговор с ботом текстом, в терминале.

Тот же скрипт продаж и те же инструменты, что в голосовом режиме, но без
распознавания и озвучки. Нужен, чтобы обкатывать формулировки быстро и
дёшево: платится только за языковую модель, копейки за диалог.

    python chat.py

Команды прямо в диалоге:
    /заказ    показать текущий состав
    /сброс    начать разговор заново
    /выход
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI

import prompts
from tools import SCHEMAS, Backend, dispatch

load_dotenv()

DIM = "\033[2m"
BOT = "\033[38;5;180m"
YOU = "\033[38;5;110m"
TOOL = "\033[38;5;65m"
OFF = "\033[0m"


async def main() -> None:
    be = Backend()
    oai = AsyncOpenAI()

    try:
        conf = await be.settings()
    except Exception as e:
        sys.exit(f"Бэкенд не отвечает ({e}).\nЗапусти: docker compose up -d backend")

    if conf.get("error"):
        sys.exit(f"Бэкенд вернул ошибку: {conf['error']}")

    call_id = await be.start_call("whatsapp", "+70000000000")
    system = prompts.build(conf, "whatsapp")
    history: list[dict] = []

    print(f"{DIM}{'─' * 62}{OFF}")
    print(f"{DIM}кафе {conf.get('name')} · скрипт продаж · выход: /выход{OFF}")
    print(f"{DIM}{'─' * 62}{OFF}\n")

    greeting = prompts.greeting(conf, "whatsapp")
    print(f"{BOT}Бот:{OFF} {greeting}\n")
    history.append({"role": "assistant", "content": greeting})

    while True:
        try:
            text = (await asyncio.to_thread(input, f"{YOU}Вы:{OFF}  ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/выход", "/exit", "/q"):
            break
        if text == "/заказ":
            print(json.dumps(await be.draft(call_id), ensure_ascii=False, indent=2))
            continue
        if text == "/сброс":
            call_id = await be.start_call("whatsapp", "+70000000000")
            history = []
            print(f"{DIM}разговор начат заново{OFF}\n")
            continue

        history.append({"role": "user", "content": text})
        messages = [{"role": "system", "content": system}, *history]

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
                print(f"\n{BOT}Бот:{OFF} {msg.content}\n")
                history = messages[1:]
                break

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = await dispatch(be, call_id, tc.function.name, args)
                short = json.dumps(result, ensure_ascii=False)
                if len(short) > 130:
                    short = short[:130] + "…"
                print(f"  {TOOL}→ {tc.function.name}{OFF} {DIM}{short}{OFF}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

    await be.finish_call(call_id)
    await be.close()
    print(f"\n{DIM}разговор завершён{OFF}")


if __name__ == "__main__":
    asyncio.run(main())
