"""
Защиты вокруг модели и библиотеки, общие для обоих каналов.

Вынесены отдельно от agent.py, чтобы не тянуть LiveKit туда, где он не нужен:
WhatsApp-бот и тесты импортируют этот модуль без голосового стека.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger("agent.guards")

COMPLETE_LIST_NOTE = (
    "Это ПОЛНЫЙ список по запросу. Других таких блюд в меню НЕТ. "
    "Называй вслух только эти и добавляй только по их id. "
    "Не дополняй список блюдами, которых здесь нет."
)
NOTHING_FOUND_NOTE = "Ничего не нашлось. Скажи гостю, что такого нет, и предложи поискать похожее."


def only_these(result: Any) -> Any:
    """Приклеить к ответу поиска указание, что список полный.

    Модель склонна дополнять список блюдами из общих знаний: предлагает
    соевый соус, которого в меню нет, а потом отказывает гостю. То же правило
    в системном промпте она игнорировала; приклеенное к результату вызова —
    соблюдает.
    """
    if not isinstance(result, dict) or "items" not in result:
        return result
    result["внимание"] = COMPLETE_LIST_NOTE if result["items"] else NOTHING_FOUND_NOTE
    return result


def safe_kwargs(cls: Any, **kwargs: Any) -> dict[str, Any]:
    """Отбросить параметры, которых нет в установленной версии библиотеки.

    Набор аргументов у LiveKit меняется от версии к версии. Вместо падения
    на старте агент отбрасывает неизвестные и пишет в лог, что именно.
    """
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
