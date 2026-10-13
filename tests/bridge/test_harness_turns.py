"""Whole turns through the real bridge (browser -> web pod sidecar -> skwoop_bridge) with the harness injected (a
scripted model, fake memory, fake Deep): the usual reply path (tone, fillers, TTS), the Deep fallback before the
first sentence, the lost line after it, the limit line, memory writes, reminders, and SKWOOP_HARNESS=0 building
nothing. No network, no model, nothing texted, nothing written."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from lean_http import ClientSession, web
from test_filler import SMALL, _write_set
from test_pod_agent import ScriptedModel
from test_turns import FAST, TONE, NoLLM, RecordingTTS, SaidSTT, _spoken
from test_webpod import _browser_turn, _cfg, _serve

import harness
import memory.memory_write as memory_write
import tools.messaging as messaging
import skwoop_bridge
import router.quick as quick
import webpod.server as wp
from filler import FillerSet
from tools.grokipedia import Lead
from harness import Harness, TurnStats
from tools.house_tools import HA, HouseTools
from memory.memory_recall import MemoryFact
from memory.memory_write import MemoryWriter, RetainItem
from harness.pod_agent import (
    LIMIT_LINE, LimitReached, ModelEvent, ModelRequest, ModelUnavailable, TextDelta, ToolCall, Usage,
)
from tools.quick_search import SearchAnswer
from tools.reminders import SAID_CANCELLED, Reminders
from harness.toolsets import TOOLSETS

PORTS = iter(range(18611, 18700, 2))
NOW = datetime(2026, 10, 6, 9, 30, tzinfo=ZoneInfo("America/Chicago"))


async def _never_send(phone: str, body: str) -> messaging.SendResult:
    raise AssertionError("no text goes out in tests")


class Deep:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.asked: list[tuple[str, list[str] | None]] = []

    async def __call__(self, http: ClientSession | None, heard: str, room: str = "pod",
                       facts: list[str] | None = None) -> str:
        self.asked.append((heard, facts))
        return self.reply


class FakeStore:
    def __init__(self) -> None:
        self.retained: list[RetainItem] = []

    async def recall(self, query: str) -> tuple[MemoryFact, ...]:
        return (MemoryFact("world", "The trash goes out on Mondays."),)

    async def retain(self, item: RetainItem) -> None:
        self.retained.append(item)

    async def delete(self, doc_id: str) -> None:
        raise AssertionError("nothing to delete here")


class FakeCalendar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def service(self, domain: str, service: str, data: dict[str, str]) -> object:
        self.calls.append((domain, service, data))
        return []


class FakeWiki:
    def __init__(self) -> None:
        self.topics: list[str] = []

    async def lead(self, topic: str) -> Lead | None:
        self.topics.append(topic)
        return Lead("Platypus", "The platypus is an egg-laying mammal of eastern Australia.")


class FakeSearch:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def ask(self, query: str, *args: object) -> SearchAnswer:
        self.asked.append(query)
        return SearchAnswer("Home Assistant 2026.10 shipped on October 1.", 1, 0.0)


async def _recall(query: str) -> str:
    return "From Skwoop's long-term memory:\n- the user's wife is named Robin."


def _house(search: FakeSearch | None = None, texts: messaging.Texts | None = None) -> HouseTools:
    answers = quick.QuickAnswers(quick.WeatherSource(lambda: asyncio.sleep(0)),
                                 quick.TimerBook(ring=lambda d, r, s: asyncio.sleep(0)))
    return HouseTools(HA(None, "", ""), answers.timers, answers, texts or messaging.Texts({}, _never_send),
                      search, recall=_recall)  # type: ignore[arg-type]  # SAFETY: duck-typed fake search


async def _turn(tmp: Path, said: str, model: ScriptedModel, deep: Deep, store: FakeStore | None = None,
                wiki: FakeWiki | None = None, reminders: Reminders | None = None,
                deep_fallback: bool = True) -> tuple[list[dict[str, object]], web.Application]:
    port = next(PORTS)
    tts = RecordingTTS()
    app = skwoop_bridge.build_app(port)
    app["ask_deep_stream"] = None
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["stt"], app["llm"], app["tts"] = SaidSTT(said), NoLLM(), tts
    app["store"].inbox.synth = tts.synth
    _write_set(tmp, SMALL, seconds=0.25)
    fillers = FillerSet.load(tmp)
    fillers.rng = random.Random(2)
    app["fillers"], app["filler_timing"] = fillers, FAST
    app["house_tools"] = _house()
    memory = MemoryWriter(store or FakeStore(), lambda: NOW)
    app["harness"] = Harness(model, app["house_tools"], memory, wiki, reminders, deep_fallback, now=lambda: NOW,
                             alert=lambda text: app["store"].set_alert(text))
    app["ask_deep"] = deep
    bridge, bridge_port = await _serve(app)
    sidecar, side_port = await _serve(wp.make_app(_cfg(bridge_port)))
    try:
        texts, _ = await _browser_turn(side_port, {"type": "hello", "device": "turns"}, TONE)
    finally:
        await sidecar.cleanup()
        await bridge.cleanup()

    return texts, app


def _answer(*parts: str) -> list[ModelEvent]:
    return [TextDelta(p) for p in parts] + [Usage(700, 500_000)]


def test_harness_answers_through_the_usual_reply_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="skwoop.bridge")
    deep = Deep("never")
    model = ScriptedModel([_answer("Dogs are loyal and need walks, ", "while cats are independent and low-key.")])
    texts, _ = asyncio.run(_turn(tmp_path, "Compare cats and dogs as pets in one sentence.", model, deep))
    assert _spoken(texts)[-1] == ("Dogs are loyal and need walks, while cats are independent and low-key.", False)
    assert deep.asked == [] and [r.tier.model for r in model.requests] == ["grok-4.20-0309-non-reasoning"]
    tones = [t for t in texts if t.get("type") == "llm" and t.get("emotion") in ("happy", "sad", "neutral")]
    assert tones                                                      # the reply tone still goes to the face
    line = next(r.getMessage() for r in caplog.records if "turn profile" in r.getMessage())
    assert "route=harness-chat" in line and "harness=1 tier=fast toolset=chat" in line
    assert "usage_prompt_tokens=700" in line and "rounds=1" in line


def test_memory_fact_is_prefilled_and_answered_in_one_round(tmp_path: Path) -> None:
    model = ScriptedModel([_answer("Your wife is Robin.")])
    texts, _ = asyncio.run(_turn(tmp_path, "Do you remember who my wife is?", model, Deep("never")))
    assert _spoken(texts)[-1] == ("Your wife is Robin.", False)
    system = str(model.requests[0].messages[0]["content"])
    assert "[recall] From Skwoop's long-term memory:" in system and "Robin" in system


def test_failure_before_the_first_sentence_falls_back_to_deep_with_the_facts(tmp_path: Path) -> None:
    deep = Deep("Robin is your wife.")
    model = ScriptedModel([ModelUnavailable("HTTP 502")])
    texts, _ = asyncio.run(_turn(tmp_path, "Do you remember who my wife is?", model, deep))
    assert _spoken(texts)[-1] == ("Robin is your wife.", False)
    assert deep.asked and deep.asked[0][1] is not None and "Robin" in deep.asked[0][1][0]


def test_the_model_escalating_hands_the_turn_to_deep(tmp_path: Path) -> None:
    deep = Deep("Deep did the several steps.")
    model = ScriptedModel([[ToolCall("c1", "escalate", '{"reason": "needs several steps"}')]])
    texts, _ = asyncio.run(_turn(tmp_path, "Tell me a joke.", model, deep))
    assert _spoken(texts)[-1] == ("Deep did the several steps.", False) and len(deep.asked) == 1


def test_failure_after_the_first_sentence_ends_on_the_lost_line(tmp_path: Path) -> None:
    class Breaks(ScriptedModel):
        async def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]:
            self.requests.append(req)
            yield TextDelta("Here is the first full sentence of it. ")
            yield TextDelta("And then")
            raise ModelUnavailable("stream: ClientPayloadError")

    deep = Deep("never")
    texts, _ = asyncio.run(_turn(tmp_path, "Tell me a joke.", Breaks([]), deep))
    assert [t for t, f in _spoken(texts) if not f] == ["Here is the first full sentence of it.", harness.LOST_LINE]
    assert deep.asked == []


def test_the_limit_line_raises_the_alert_and_never_retries_on_deep(tmp_path: Path) -> None:
    deep = Deep("never")
    model = ScriptedModel([LimitReached(LIMIT_LINE, True, "HTTP 403")])
    texts, app = asyncio.run(_turn(tmp_path, "Tell me a joke.", model, deep))
    assert _spoken(texts)[-1] == (LIMIT_LINE, False) and deep.asked == []
    assert "Grok limit" in str(app["store"].feed()["alert"]["text"])


def test_fallback_off_says_it_cannot_instead_of_deep(tmp_path: Path) -> None:
    deep = Deep("never")
    model = ScriptedModel([ModelUnavailable("HTTP 502")])
    texts, _ = asyncio.run(_turn(tmp_path, "Tell me a joke.", model, deep, deep_fallback=False))
    assert _spoken(texts)[-1] == (harness.CANT_LINE, False) and deep.asked == []


@pytest.mark.parametrize("env, house, built, reminders", [
    ({}, True, False, False),
    ({"SKWOOP_HARNESS": "0"}, True, False, False),
    ({"SKWOOP_HARNESS": "1"}, False, False, False),
    ({"SKWOOP_HARNESS": "1"}, True, True, True),
    ({"SKWOOP_HARNESS": "1", "SKWOOP_REMINDERS": "0"}, True, True, False),
])
def test_wire_harness_builds_only_when_on_with_house_tools(env: dict[str, str], house: bool, built: bool,
                                                           reminders: bool) -> None:
    async def go() -> tuple[bool, bool]:
        app = skwoop_bridge.build_app(0)
        async with ClientSession() as http:
            app["http"] = http
            if house:
                app["house_tools"] = _house()
            skwoop_bridge.wire_harness(app, env)
            h = app.get("harness")
            return h is not None, h is not None and h.reminders is not None

    assert asyncio.run(go()) == (built, reminders)


def test_a_basic_info_ask_is_answered_with_the_grokipedia_lead(tmp_path: Path) -> None:
    wiki = FakeWiki()
    model = ScriptedModel([_answer("The platypus is an egg-laying mammal from eastern Australia.")])
    texts, _ = asyncio.run(_turn(tmp_path, "What is a platypus?", model, Deep("never"), wiki=wiki))
    assert _spoken(texts)[-1] == ("The platypus is an egg-laying mammal from eastern Australia.", False)
    assert wiki.topics == ["platypus"]
    assert "[wiki] Grokipedia, Platypus: The platypus is an egg-laying" in str(model.requests[0].messages[0]["content"])


def test_remember_that_writes_one_tagged_fact(tmp_path: Path) -> None:
    store = FakeStore()
    model = ScriptedModel([[ToolCall("c1", "remember", '{"fact": "The plumber comes on Tuesdays."}')]])
    texts, _ = asyncio.run(_turn(tmp_path, "Remember that the plumber comes on Tuesdays.", model, Deep("never"),
                                 store))
    assert _spoken(texts)[-1] == ("Got it: The plumber comes on Tuesdays.", False)
    assert [i["content"] for i in store.retained] == ["The plumber comes on Tuesdays."]
    tags = store.retained[0]["tags"]
    assert tags[0] == "source:skwoop-pod" and tags[1].startswith("room:") and len(tags) == 2


def test_a_health_fact_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    store = FakeStore()
    model = ScriptedModel([[ToolCall("c1", "remember", '{"fact": "Alex takes the blood pressure pills at 9."}')]])
    texts, _ = asyncio.run(_turn(tmp_path, "Remember that I take my blood pressure pills at 9.", model,
                                 Deep("never"), store))
    assert _spoken(texts)[-1] == (memory_write.SAID_HEALTH, False) and store.retained == []


def test_a_reminder_is_said_back_and_held_unwritten(tmp_path: Path) -> None:
    cal = FakeCalendar()
    rem = Reminders(cal.service, tmp_path / "pending.json", lambda: 1_791_300_000.0)       # Oct 6 2026, 10:20 AM CT
    model = ScriptedModel([[ToolCall("c1", "remind", '{"text": "Call the vet", "when": "2026-10-13T15:00"}')]])
    texts, _ = asyncio.run(_turn(tmp_path, "Remind me to call the vet next Tuesday at 3.", model, Deep("never"),
                                 reminders=rem))
    assert _spoken(texts)[-1] == ("I'll remind you Tuesday, October 13 at 3 PM: Call the vet.", False)
    assert [t["function"]["name"] for t in model.requests[0].tools] == ["remind", "forget_last", "escalate"]
    assert [h.text for h in rem.held] == ["Call the vet"] and cal.calls == []
    assert "2026" in str(model.requests[0].messages[0]["content"])                    # the year, for ISO dates


def test_never_mind_while_held_drops_the_reminder(tmp_path: Path) -> None:
    rem = Reminders(FakeCalendar().service, tmp_path / "pending.json", lambda: 1_791_300_000.0)
    asyncio.run(rem.remind({"text": "Call the vet", "when": "2026-10-13T15:00"}, "web-pod-turns"))
    model = ScriptedModel([[ToolCall("c1", "forget_last", "{}")]])
    texts, _ = asyncio.run(_turn(tmp_path, "Never mind.", model, Deep("never"), reminders=rem))
    assert _spoken(texts)[-1] == (SAID_CANCELLED, False) and rem.held == []


def test_the_answer_to_what_time_goes_back_to_the_reminder(tmp_path: Path) -> None:
    rem = Reminders(FakeCalendar().service, tmp_path / "pending.json", lambda: 1_791_300_000.0)
    asyncio.run(rem.remind({"text": "Call the vet", "when": "", "ask": "What time?"}, "web-pod-turns"))
    model = ScriptedModel([[ToolCall("c1", "remind", '{"text": "Call the vet", "when": "2026-10-06T15:00"}')]])
    texts, _ = asyncio.run(_turn(tmp_path, "Three in the afternoon.", model, Deep("never"), reminders=rem))
    assert _spoken(texts)[-1] == ("I'll remind you today at 3 PM: Call the vet.", False) and len(rem.held) == 1


# ---------- the registry over the real house tools (Harness.turn directly)
def _direct(h: Harness, kind: str, heard: str, device: str = "dev") -> list[str]:
    async def go() -> list[str]:
        return [s async for s in h.turn(TOOLSETS[kind], heard, device, "office", [], TurnStats())]  # type: ignore[index]

    return asyncio.run(go())


def test_send_text_called_twice_still_sends_nothing() -> None:
    texts = messaging.Texts(messaging.parse_contacts('mom:\n  name: Mom\n  phone: "+15550000001"\n'), _never_send)
    house = _house(texts=texts)
    model = ScriptedModel([[ToolCall("a", "send_text", '{"to": "Mom", "message": "hi"}'),
                            ToolCall("b", "send_text", '{"to": "Mom", "message": "hi again"}')]])
    said = _direct(Harness(model, house, MemoryWriter(FakeStore(), lambda: NOW)), "texting", "Text Mom hi")
    assert said[-1] == "Send it?" and house.texts.waiting("dev") is not None     # proposed, waiting for a yes


def test_a_question_gets_no_action_tool_and_never_acts_through_the_registry() -> None:
    model = ScriptedModel([[ToolCall("a", "lights", '{"light": "living", "power": "on"}')], _answer("It's off.")])
    said = _direct(Harness(model, _house(), MemoryWriter(FakeStore(), lambda: NOW)), "house", "Is the lamp on?")
    assert [t["function"]["name"] for t in model.requests[0].tools] == ["escalate"]
    assert said == ["It's off."] and "isn't available" in str(model.requests[1].messages[-1]["content"])


def test_the_web_lead_uses_the_proxy_search_and_a_missing_search_offers_deep() -> None:
    search = FakeSearch()
    model = ScriptedModel([_answer("Version 2026.10 shipped on October 1.")])
    h = Harness(model, _house(search), MemoryWriter(FakeStore(), lambda: NOW))
    assert _direct(h, "live", "Summarize the latest Home Assistant release for me.") == [
        "Version 2026.10 shipped on October 1."]
    assert search.asked == ["the latest Home Assistant release"]
    assert "[web] Home Assistant 2026.10" in str(model.requests[0].messages[0]["content"])
    offline = ScriptedModel([_answer("I can't check that from here.")])
    _direct(Harness(offline, _house(), MemoryWriter(FakeStore(), lambda: NOW)), "live", "latest HA release")
    assert [t["function"]["name"] for t in offline.requests[0].tools] == ["web_search", "escalate"]
