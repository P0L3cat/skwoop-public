"""toolsets.pick over the router's routes and the router eval sets."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import harness
import tools.ha_service as house
import tools.house_tools as house_tools
import router.quick as quick
import router
import harness.toolsets as toolsets
from harness.toolsets import Decline, Toolset

EVAL = Path(__file__).resolve().parents[2] / "bridge" / "eval"
# what the eval's "expect" label needs: the tool offered, or the read that answers it
COVERS: dict[str, tuple[str, str]] = {"calendar": ("calendar", "calendar"), "forecast": ("forecast", "forecast"),
                                      "bot_status": ("bot_status", "bot_status"), "snapshot": ("-", "snapshot"),
                                      "todo": ("lists", "list"), "web_search": ("web_search", "web"),
                                      "lights": ("lights", "snapshot"), "send_text": ("send_text", "-")}


def _route(text: str) -> router.Route:
    answers = quick.QuickAnswers(quick.WeatherSource(lambda: asyncio.sleep(0)),
                                 quick.TimerBook(ring=lambda d, r, s: asyncio.sleep(0)))

    return router.route(text, lambda t: answers.match(t, "dev"), lambda t: house.match(t))


def _pick(text: str, reminders: bool = True) -> Toolset | Decline:
    return toolsets.pick(_route(text), text, reminders)


def _eval_texts() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for name in ("router_eval.jsonl", "router_heldout.jsonl"):
        items += [json.loads(line) for line in (EVAL / name).read_text().splitlines() if line.strip()]

    return items


def test_every_offered_tool_has_a_schema() -> None:
    assert set(toolsets.SCHEMAS) == set(toolsets.TOOL_NAMES)
    assert set(house_tools.TOOL_NAMES) <= set(toolsets.TOOL_NAMES)
    assert all(f'"name": "{n}"' in json.dumps(toolsets.SCHEMAS[n]) for n in toolsets.SCHEMAS)


def test_the_four_bench_asks_are_facts_first_with_no_tool_rounds() -> None:
    live = _pick("Summarize the latest Home Assistant release for me.")
    notes = _pick("What's in my notes about the dishwasher?")
    wife = _pick("Do you remember who my wife is?")
    simple = _pick("Compare cats and dogs as pets in one sentence.")
    assert isinstance(live, Toolset) and live.kind == "live" and live.reads == ("web",)
    assert isinstance(notes, Toolset) and notes.kind == "notes" and notes.reads == ("notes",)
    assert isinstance(wife, Toolset) and wife.kind == "memory-fact" and wife.reads == ("recall",)
    assert isinstance(simple, Toolset) and simple.kind == "chat" and simple.reads == ()
    assert all(t.tools == () for t in (live, notes, wife, simple))


@pytest.mark.parametrize("text, why", [("Play some music.", "music"),
                                       ("Write a note that the car needs an oil change.", "note write"),
                                       ("Find a tikka recipe and save it to my notes.", "note write"),
                                       ("Turn on the lights.", "instant")])
def test_declined_asks_stay_on_the_other_paths(text: str, why: str) -> None:
    picked = _pick(text)
    assert isinstance(picked, Decline) and why in picked.why


def test_every_eval_ask_gets_at_most_four_tools_and_no_admin_tool() -> None:
    admin = {"bridge_status", "harness_flag", "restart", "reasoning", "starlink_status", "starlink_reboot"}
    picked = [p for item in _eval_texts() if isinstance(p := _pick(str(item["text"])), Toolset)]
    assert len(picked) > 50
    for p in picked:
        tools = toolsets.offered(p, (), True)
        assert len(tools) <= toolsets.MAX_TOOLS and not admin & set(tools), p


def test_eval_asks_get_the_tool_or_read_they_need() -> None:
    missing = []
    for item in _eval_texts():
        need = COVERS.get(str(item["expect"]))
        if item["tier"] == "instant" or need is None:
            continue

        p = _pick(str(item["text"]))
        tool, read = need
        if not (isinstance(p, Toolset) and (tool in p.tools or read in p.reads)):
            missing.append((item["text"], item["expect"], p))

    assert missing == []


def test_offered_adds_missed_reads_keeps_the_cap_and_puts_escalate_last() -> None:
    house_set = _pick("Turn the lamp to half.")
    assert isinstance(house_set, Toolset)
    assert toolsets.offered(house_set, (), True)[-1] == "escalate"
    assert "escalate" not in toolsets.offered(house_set, (), False)
    grown = toolsets.offered(house_set, ("forecast", "calendar"), True)
    assert len(grown) == toolsets.MAX_TOOLS and grown[-1] == "escalate"
    assert toolsets.offered(house_set, ("forecast",), True, question=True) == ("forecast", "escalate")
    wife = _pick("Do you remember who my wife is?")
    assert isinstance(wife, Toolset) and toolsets.offered(wife, ("recall_memory",), True) == ("recall_memory",
                                                                                              "escalate")


def test_calendar_window_uses_the_exact_day_when_the_words_say_it() -> None:
    assert harness.calendar_window("What's on my calendar today?") == (0, 1)
    assert harness.calendar_window("What's my next meeting?") == (0, 7)


def test_list_add_gets_no_read_internet_gets_history_and_weather_elsewhere_is_live() -> None:
    add = _pick("Add milk to the shopping list.")
    assert isinstance(add, Toolset) and add.reads == () and add.tools[0] == "lists"
    net = _pick("Is the internet down?")
    assert isinstance(net, Toolset) and net.kind == "house" and "history" in net.tools
    chicago = _pick("What's the weather in Chicago?")
    assert isinstance(chicago, Toolset) and chicago.kind == "live"


@pytest.mark.parametrize("heard, query", [
    ("Summarize the latest Home Assistant release for me.", "the latest Home Assistant release"),
    ("Hey Skwoop, can you look up the newest iPhone price, please?", "the newest iPhone price"),
    ("Who won the Cowboys game last night?", "Who won the Cowboys game last night"),
    ("Summarize.", "Summarize."),
])
def test_the_web_lead_searches_the_bare_ask(heard: str, query: str) -> None:
    assert toolsets.search_query(heard) == query


@pytest.mark.parametrize("text", ["Who was Abraham Lincoln?", "What is a platypus?", "How do magnets work?",
                                  "Tell me about the Roman Empire."])
def test_basic_info_asks_get_a_grokipedia_lead(text: str) -> None:
    picked = _pick(text)
    assert isinstance(picked, Toolset) and picked.kind == "basic-info" and picked.reads == ("wiki",)


@pytest.mark.parametrize("text", ["Who is the president of France?", "What's my wifi password?",
                                  "Compare cats and dogs as pets in one sentence."])
def test_live_personal_and_chat_asks_are_not_basic_info(text: str) -> None:
    picked = _pick(text)
    assert not isinstance(picked, Toolset) or picked.kind != "basic-info"


@pytest.mark.parametrize("text", ["Remember that the plumber comes Tuesday.", "Remember that the gate code changed.",
                                  "No, forget that.", "Forget that."])
def test_memory_writes_and_undo_get_remember_and_forget_last(text: str) -> None:
    picked = _pick(text)
    assert isinstance(picked, Toolset) and picked.kind == "memory-write"
    assert picked.tools == ("remember", "forget_last") and picked.reads == ()


@pytest.mark.parametrize("text", ["Remind me to call the vet tomorrow at 3.", "Remind me tomorrow to call the vet.",
                                  "Remember to buy milk.", "Don't let me forget the trash tonight.",
                                  "Set a reminder for the dentist on Friday."])
def test_reminders_get_remind_and_forget_last(text: str) -> None:
    picked = _pick(text)
    assert isinstance(picked, Toolset) and picked.kind == "reminder" and picked.tools == ("remind", "forget_last")
    off = _pick(text, reminders=False)
    assert isinstance(off, Decline) and "reminders are off" in off.why


@pytest.mark.parametrize("text", ["Remind me what my wife's name is.", "Never mind.", "Cancel that."])
def test_memory_questions_and_a_bare_never_mind_are_not_reminders(text: str) -> None:
    picked = _pick(text)
    assert not isinstance(picked, Toolset) or picked.kind not in ("reminder", "memory-write")


def test_never_mind_is_an_undo_only_while_a_reminder_is_in_flight() -> None:
    assert toolsets.is_undo("Never mind.", cancel=True) and toolsets.is_undo("No, cancel that.", cancel=True)
    assert not toolsets.is_undo("Never mind.") and toolsets.is_undo("Forget that.")


def test_write_tools_appear_only_on_their_own_kinds() -> None:
    for item in _eval_texts():
        picked = _pick(str(item["text"]))
        if isinstance(picked, Toolset):
            assert ("remind" in picked.tools) == (picked.kind == "reminder")
            assert ("remember" in picked.tools) == (picked.kind == "memory-write")
