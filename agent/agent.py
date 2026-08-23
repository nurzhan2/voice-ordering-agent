"""
Голосовой агент приёма заказов.

    Новофон (SIP) -> LiveKit SIP -> этот агент -> backend -> iiko

Запуск:
    python agent.py console   # локально, с микрофона, без телефонии
    python agent.py dev       # с hot reload
    python agent.py start     # прод (так запускается в docker)

О задержке. Пауза, которую слышит гость, складывается из пяти частей:
распознавание -> вызовы инструментов -> модель -> озвучка -> сеть.
Самая крупная и самая незаметная часть — инструменты: они выполняются
последовательно, и каждый добавляет ещё один круг к языковой модели.
Поэтому здесь ограничено их количество за ход и включён звук работы,
чтобы гость не сидел в тишине.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    function_tool,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

import prompts
from tools import Backend

load_dotenv()
logger = logging.getLogger("agent")

# Звук работы во время вызова инструментов. Гость слышит, что администратор
# вбивает заказ, а не тишину. Для линии приёма заказов это естественно.
# Модуля может не быть в старых версиях — тогда просто работаем без него.
try:
    from livekit.agents import AudioConfig, BackgroundAudioPlayer, BuiltinAudioClip

    HAS_BG_AUDIO = True
except ImportError:  # pragma: no cover
    HAS_BG_AUDIO = False
    logger.info("BackgroundAudioPlayer недоступен, звук работы выключен")


def safe_kwargs(cls, **kwargs):
    """Отбросить параметры, которых нет в этой версии библиотеки.

    Набор параметров у LiveKit меняется от версии к версии. Вместо того
    чтобы падать при запуске, отбрасываем неизвестные и пишем об этом
    в лог — агент поднимется в любом случае.
    """
    import inspect

    try:
        allowed = set(inspect.signature(cls).parameters)
    except (TypeError, ValueError):
        return kwargs
    if "kwargs" in allowed:
        return kwargs
    dropped = [k for k in kwargs if k not in allowed]
    if dropped:
        logger.warning("%s не поддерживает: %s", cls.__name__, ", ".join(dropped))
    return {k: v for k, v in kwargs.items() if k in allowed}


# Модель склонна дополнять список блюдами, которые знает из общих сведений:
# предлагает соевый соус, которого в меню нет, а потом отказывает гостю.
# Ограничение, приклеенное прямо к ответу инструмента, работает надёжнее
# любых указаний в промпте — на результат вызова модель смотрит внимательнее.
def only_these(result: dict) -> dict:
    if not isinstance(result, dict) or "items" not in result:
        return result
    if result["items"]:
        result["внимание"] = (
            "Это ПОЛНЫЙ список по запросу. Других таких блюд в меню НЕТ. "
            "Называй вслух только эти и добавляй только по их id. "
            "Не дополняй список блюдами, которых здесь нет."
        )
    else:
        result["внимание"] = (
            "Ничего не нашлось. Скажи гостю, что такого нет, "
            "и предложи поискать похожее."
        )
    return result


class Operator(Agent):
    def __init__(self, be: Backend, call_id: str, settings: dict) -> None:
        super().__init__(instructions=prompts.build(settings, "voice"))
        self.be = be
        self.call_id = call_id
        self.settings = settings

    async def on_enter(self) -> None:
        await self.session.say(
            prompts.greeting(self.settings, "voice"), allow_interruptions=True
        )

    @function_tool
    async def search_menu(self, context: RunContext, query: str) -> dict:
        """Найти блюдо в меню по названию или его части.

        Args:
            query: название так, как его произнёс гость.
        """
        return only_these(await self.be.search(query))

    @function_tool
    async def recommend(self, context: RunContext, kind: str) -> dict:
        """Что предложить гостю.

        Args:
            kind: main — основное (сеты, пицца, мангал, горячее);
                extra — допродажа (роллы, салаты, десерты);
                garnish — гарниры; sauce — соусы; drink — напитки.
        """
        return only_these(await self.be.recommend(kind, self.call_id))

    @function_tool
    async def add_item(
        self,
        context: RunContext,
        item_id: str,
        quantity: int = 1,
        size: str | None = None,
        modifiers: list[str] | None = None,
    ) -> dict:
        """Добавить позицию в заказ.

        Args:
            item_id: id блюда, полученный из search_menu или recommend.
            quantity: количество порций.
            size: название размера, если у блюда есть размеры.
            modifiers: названия добавок.
        """
        return await self.be.add(
            self.call_id, item_id, quantity, size, modifiers or []
        )

    @function_tool
    async def remove_item(self, context: RunContext, line_id: int) -> dict:
        """Убрать позицию из заказа.

        Args:
            line_id: номер строки из get_order.
        """
        return await self.be.remove(self.call_id, line_id)

    @function_tool
    async def set_quantity(
        self, context: RunContext, line_id: int, quantity: int
    ) -> dict:
        """Изменить количество позиции.

        Args:
            line_id: номер строки из get_order.
            quantity: новое количество, 0 — удалить.
        """
        return await self.be.qty(self.call_id, line_id, quantity)

    @function_tool
    async def get_order(self, context: RunContext) -> dict:
        """Состав заказа, сумма и чего ещё не хватает для оформления."""
        return await self.be.draft(self.call_id)

    @function_tool
    async def set_customer(
        self,
        context: RunContext,
        name: str | None = None,
        address: str | None = None,
        persons: int | None = None,
        cutlery: bool | None = None,
        payment: str | None = None,
        change_from: int | None = None,
        comment: str | None = None,
    ) -> dict:
        """Записать данные гостя. Вызывай сразу, как узнал любое из полей.

        Args:
            name: имя гостя.
            address: адрес доставки.
            persons: на сколько персон приборы.
            cutlery: нужны ли приборы.
            payment: cash, transfer или card.
            change_from: с какой суммы готовить сдачу, в рублях.
            comment: пожелание к заказу.
        """
        return await self.be.customer(
            self.call_id,
            name=name,
            address=address,
            persons=persons,
            cutlery=cutlery,
            payment=payment,
            change_from=change_from,
            comment=comment,
        )

    @function_tool
    async def mark_offered(self, context: RunContext, kind: str) -> dict:
        """Отметить, что допродажу уже предлагали, чтобы не повторяться.

        Args:
            kind: garnish, sauce, drink или extra.
        """
        return await self.be.offered(self.call_id, kind)

    @function_tool
    async def check_delivery(self, context: RunContext, address: str) -> dict:
        """Проверить, доставляем ли по адресу и открыто ли сейчас кафе.

        Вызывай сразу, как гость назвал адрес, до оформления заказа.

        Args:
            address: адрес так, как его назвал гость.
        """
        return await self.be.check_delivery(address)

    @function_tool
    async def confirm_order(self, context: RunContext) -> dict:
        """Оформить заказ. Только после явного согласия гостя."""
        return await self.be.confirm(self.call_id)


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------


def build_stt():
    """Deepgram распознаёт потоково: текст приходит по мере речи, а не
    после её окончания. Без ключа берём OpenAI — медленнее, но работает."""
    if os.getenv("DEEPGRAM_API_KEY"):
        return deepgram.STT(
            **safe_kwargs(
                deepgram.STT,
                model=os.getenv("STT_MODEL", "nova-3"),
                language="ru",
                # Финальную расшифровку отдаём раньше: решение о конце
                # реплики всё равно принимает модель определения хода.
                endpointing_ms=int(os.getenv("STT_ENDPOINTING_MS", "200")),
            )
        )
    logger.warning("DEEPGRAM_API_KEY не задан, использую OpenAI STT (медленнее)")
    return openai.STT(model="whisper-1", language="ru")


def build_llm():
    """max_completion_tokens — жёсткий потолок на длину реплики.

    Промпт просит говорить коротко, но модель регулярно это игнорирует.
    Здесь ограничение на уровне API: длиннее просто не сгенерируется.
    Сто двадцать токенов — это примерно две короткие фразы по-русски.
    """
    return openai.LLM(
        **safe_kwargs(
            openai.LLM,
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            temperature=0.4,
            max_completion_tokens=int(os.getenv("MAX_REPLY_TOKENS", "120")),
        )
    )


def build_tts():
    """ElevenLabs Flash — самая быстрая модель у них по времени до первого
    байта. Без ключа берём голос OpenAI, для отладки достаточно."""
    if os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID"):
        return elevenlabs.TTS(
            **safe_kwargs(
                elevenlabs.TTS,
                model=os.getenv("TTS_MODEL", "eleven_flash_v2_5"),
                voice_id=os.environ["ELEVENLABS_VOICE_ID"],
            )
        )
    logger.warning("ELEVENLABS не настроен, использую OpenAI TTS")
    return openai.TTS(model="gpt-4o-mini-tts", voice="shimmer")


# ---------------------------------------------------------------------------
# Сервер
# ---------------------------------------------------------------------------

server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    """Загрузить модель определения голоса заранее.

    Каждый звонок обрабатывается в отдельном процессе. Без прогрева
    Silero грузится заново при каждом входящем — это лишние сотни
    миллисекунд ровно в тот момент, когда гость ждёт ответа.
    """
    proc.userdata["vad"] = silero.VAD.load()
    logger.info("модель определения голоса прогрета")


server.setup_fnc = prewarm


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    be = Backend()

    # Без этого агент в комнате есть, но звук не публикует
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    caller = participant.attributes.get("sip.phoneNumber", "unknown")
    logger.info("звонок с %s, комната %s", caller, ctx.room.name)

    settings = await be.settings()
    call_id = await be.start_call("phone", caller, ctx.room.name)

    session = AgentSession(
        **safe_kwargs(
            AgentSession,
            # Прогретая модель вместо загрузки при каждом звонке
            vad=ctx.proc.userdata.get("vad") or silero.VAD.load(),
            stt=build_stt(),
            llm=build_llm(),
            tts=build_tts(),
            # Ответ готовится, пока гость договаривает
            preemptive_generation=True,
            turn_detection=MultilingualModel(),
            # Инструменты выполняются последовательно, и каждый добавляет
            # ещё один круг к модели. Три — потолок, дальше гость просто
            # ждёт в тишине.
            max_tool_steps=int(os.getenv("MAX_TOOL_STEPS", "3")),
            # Гость диктует адрес с паузами — даём договорить, не перебиваем
            min_endpointing_delay=float(os.getenv("MIN_WAIT", "0.35")),
            max_endpointing_delay=float(os.getenv("MAX_WAIT", "2.0")),
        )
    )

    # Ссылки на фоновые задачи, иначе сборщик мусора их прибьёт
    background: set = set()

    @session.on("conversation_item_added")
    def _log(ev) -> None:
        text = getattr(ev.item, "text_content", None)
        role = getattr(ev.item, "role", "")
        if not (isinstance(text, str) and text.strip() and isinstance(role, str)):
            return
        task = asyncio.create_task(be.log(call_id, role, text.strip()))
        background.add(task)
        task.add_done_callback(background.discard)

    @session.on("metrics_collected")
    def _metrics(ev) -> None:
        m = getattr(ev, "metrics", None)
        if m is None:
            return
        for field, label in (
            ("end_of_utterance_delay", "пауза до ответа"),
            ("ttft", "модель, первый токен"),
            ("ttfb", "озвучка, первый байт"),
            ("duration", "длительность"),
        ):
            v = getattr(m, field, None)
            if isinstance(v, (int, float)) and v > 0:
                logger.info("ЗАМЕР %-20s %-22s %.2f с", type(m).__name__, label, v)

    async def _shutdown() -> None:
        await be.finish_call(call_id)
        await be.close()

    ctx.add_shutdown_callback(_shutdown)

    await session.start(agent=Operator(be, call_id, settings), room=ctx.room)

    # Звук клавиатуры, пока идут вызовы инструментов. Половина ощущаемой
    # паузы — это тишина; заполненная пауза воспринимается втрое короче.
    if HAS_BG_AUDIO and os.getenv("THINKING_SOUND", "1") != "0":
        try:
            audio = BackgroundAudioPlayer(
                thinking_sound=[
                    AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.5),
                    AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.4),
                ],
            )
            await audio.start(room=ctx.room, agent_session=session)
            logger.info("звук работы включён")
        except Exception:
            logger.exception("не удалось включить звук работы, продолжаю без него")


if __name__ == "__main__":
    cli.run_app(server)
