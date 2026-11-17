"""Agent-side pieces that don't need the voice stack: guards, prompt, tool routing."""

from guards import COMPLETE_LIST_NOTE, NOTHING_FOUND_NOTE, only_these, safe_kwargs

import prompts
import tools

SETTINGS = {"name": "Тарелка", "operator_name": "Амина", "min_order": 700}


def test_search_results_are_marked_complete():
    r = only_these({"items": [{"id": "1", "name": "Шашлык"}]})
    assert r["внимание"] == COMPLETE_LIST_NOTE


def test_empty_search_tells_the_model_to_say_so():
    assert only_these({"items": []})["внимание"] == NOTHING_FOUND_NOTE


def test_non_search_results_pass_through_untouched():
    assert only_these({"ok": True}) == {"ok": True}


def test_safe_kwargs_drops_arguments_the_library_lacks():
    class OldSession:
        def __init__(self, stt, llm):
            pass

    assert safe_kwargs(OldSession, stt=1, llm=2, max_tool_steps=3) == {"stt": 1, "llm": 2}


def test_safe_kwargs_keeps_everything_for_open_signatures():
    class AnySession:
        def __init__(self, **kwargs):
            pass

    assert safe_kwargs(AnySession, anything=1) == {"anything": 1}


def test_voice_prompt_carries_contrast_examples_and_settings():
    p = prompts.build(SETTINGS, "voice")
    assert "Робот:" in p
    assert "ТЫ НЕ ЗНАЕШЬ МЕНЮ" in p
    assert "Минимум 700 рублей" in p


def test_chat_prompt_skips_spoken_style_examples():
    p = prompts.build(SETTINGS, "whatsapp")
    assert "Робот:" not in p
    assert "ТЫ НЕ ЗНАЕШЬ МЕНЮ" in p


def test_greeting_names_the_place_and_operator():
    g = prompts.greeting(SETTINGS, "voice")
    assert "Тарелка" in g and "Амина" in g
    assert len(g.split()) <= 10


class RecordingBackend:
    """Stands in for tools.Backend and records which method each tool reaches."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append(name)
            return {"items": []} if name in ("search", "recommend") else {"ok": True}

        return method


def sample_args(schema: dict) -> dict:
    by_type = {"string": "x", "integer": 1, "boolean": True, "array": []}
    props = schema["function"]["parameters"].get("properties", {})
    return {k: by_type.get(v.get("type"), "x") for k, v in props.items()}


async def test_every_declared_tool_is_routed():
    # Catches drift between the schemas the model sees and what dispatch handles.
    be = RecordingBackend()
    for schema in tools.SCHEMAS:
        name = schema["function"]["name"]
        result = await tools.dispatch(be, "call-1", name, sample_args(schema))
        assert result != {"error": "неизвестный инструмент"}, name
    assert len(be.calls) == len(tools.SCHEMAS)


async def test_whatsapp_search_goes_through_the_same_guard():
    be = RecordingBackend()
    r = await tools.dispatch(be, "call-1", "search_menu", {"query": "соевый соус"})
    assert r["внимание"] == NOTHING_FOUND_NOTE
