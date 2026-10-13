"""Whole turns through the real bridge (browser -> web pod sidecar -> skwoop_bridge): the three tiers (instant quick
answers and scripts, the pod harness, Deep), one voice for every model, and the thinking filler covering a slow fast
or Deep reply. No network."""
from __future__ import annotations

import asyncio
import math
import random
import struct
from itertools import pairwise
from pathlib import Path

import tools.ha_service as house
import tools.messaging as messaging
import skwoop_bridge
import router.quick as quick
import router
import harness.toolsets as toolsets
from harness import HandOff, TurnStats
from tools.house_tools import HA, HouseTools
import webpod.server as wp
from lean_http import ClientSession, web
from tools.music import Music, Spotify, TokenSource
from test_music import FakeSpotify
from filler import FillerSet, FillerTiming
from stubs import StubLLM, StubSTT, StubTTS
from test_filler import SMALL, _write_set
from test_webpod import _browser_turn, _cfg, _serve

TONE = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 300 * i / 16000))) for i in range(16000))
FAST = FillerTiming(first_s=0.25, gap_min_s=0.3, gap_max_s=0.4, long_after_s=5, landing_after_s=0.5)


class SaidSTT(StubSTT):
    def __init__(self, said: str) -> None:
        self.said = said

    async def transcribe(self, pcm: bytes, rate: int) -> str:
        return self.said


class RecordingTTS(StubTTS):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def synth(self, text: str, mood: str | None = None, previous_text: str | None = None,
                    next_text: str | None = None) -> bytes:
        self.calls.append((text, previous_text))

        return b"\x00\x00" * 2400


class SlowChat(StubLLM):
    """A real-mode LLM (so the 2B router runs) that takes `delay` before its first sentence."""

    mode = "xai"

    def __init__(self, delay: float, sentences: list[str]) -> None:
        self.delay = delay
        self.sentences = sentences
        self.fast: list[bool] = []

    async def stream_sentences(self, messages: list[dict[str, str]], fast: bool = False):
        self.fast.append(fast)
        await asyncio.sleep(self.delay)
        for s in self.sentences:
            yield s


class SlowHarness:
    """The pod harness without a model: `delay`, then `sentences`; `escalate` hands the turn to Deep with a fact."""

    def __init__(self, delay: float, sentences: list[str], escalate: str | None = None) -> None:
        self.delay, self.out, self.escalate = delay, sentences, escalate
        self.heard: list[str] = []

    def pick(self, device: str, route: router.Route, text: str) -> toolsets.Toolset | None:
        picked = toolsets.pick(route, text)
        return None if isinstance(picked, toolsets.Decline) else picked

    async def turn(self, toolset: toolsets.Toolset, heard: str, device: str, room: str, history: list[dict],
                   stats: TurnStats):
        self.heard.append(heard)
        await asyncio.sleep(self.delay)
        if self.escalate:
            raise HandOff(self.escalate, ["lights: the living room light is on"])
        for s in self.out:
            yield s


class NoLLM(StubLLM):
    mode = "xai"

    async def stream_sentences(self, messages: list[dict[str, str]], fast: bool = False):
        raise AssertionError("a quick intent must not reach the LLM")
        yield ""


async def _turn(port: int, tmp: Path, said: str, llm: StubLLM, tts: RecordingTTS,
                quick_answers: quick.QuickAnswers | None = None, deep_delay: float | None = None,
                deep_reply: str = "", harness: SlowHarness | None = None,
                deep_facts: list[list[str] | None] | None = None,
                texts: messaging.Texts | None = None, deep_model=None,
                music: Music | None = None) -> list[dict[str, object]]:
    app = skwoop_bridge.build_app(port)
    app["ask_deep_stream"] = deep_model
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["stt"], app["llm"], app["tts"] = SaidSTT(said), llm, tts
    app["store"].inbox.synth = tts.synth
    _write_set(tmp, SMALL, seconds=0.25)
    fillers = FillerSet.load(tmp)
    fillers.rng = random.Random(2)
    app["fillers"], app["filler_timing"] = fillers, FAST
    if quick_answers is not None:
        app["quick"] = quick_answers

    if harness is not None:
        app["harness"] = harness
        app["house_tools"] = HouseTools(HA(None, "", ""), app["quick"].timers, app["quick"],
                                        texts or messaging.Texts({}, _never_send), None)
    if texts is not None:
        app["texts"] = texts
    if music is not None:
        app["music"] = music

    if deep_delay is not None:
        delay = deep_delay

        async def slow_deep(http: ClientSession | None, heard: str, room: str = "pod",
                              facts: list[str] | None = None) -> str:
            if deep_facts is not None:
                deep_facts.append(facts)
            await asyncio.sleep(delay)

            return deep_reply

        app["ask_deep"] = slow_deep

    bridge, bridge_port = await _serve(app)
    sidecar, side_port = await _serve(wp.make_app(_cfg(bridge_port)))
    try:
        texts, _audio = await _browser_turn(side_port, {"type": "hello", "device": "turns"}, TONE)
    finally:
        await sidecar.cleanup()
        await bridge.cleanup()

    return texts


async def _never_send(phone: str, body: str) -> messaging.SendResult:
    raise AssertionError("no text goes out in tests")


def _spoken(texts: list[dict[str, object]]) -> list[tuple[str, bool]]:
    return [(str(t["text"]), bool(t.get("filler"))) for t in texts if t.get("state") == "sentence_start"]


def test_quick_weather_is_instant_and_skips_the_llm(tmp_path):
    async def ha() -> object:
        return {"state": "partlycloudy", "attributes": {"temperature": 80}}

    tts = RecordingTTS()
    answers = quick.QuickAnswers(quick.WeatherSource(ha), quick.TimerBook(ring=lambda d, r, s: asyncio.sleep(0)))
    texts = asyncio.run(_turn(18171, tmp_path, "What's the weather like right now?", NoLLM(), tts, answers))
    assert _spoken(texts) == [("Right now it's eighty degrees and partly cloudy.", False)]
    assert tts.calls == [("Right now it's eighty degrees and partly cloudy.", None)]


def test_harness_reply_has_no_filler_when_quick(tmp_path):
    tts, fast = RecordingTTS(), SlowHarness(0.0, ["Crows have three hearts."])
    texts = asyncio.run(_turn(18173, tmp_path, "tell me something fun", NoLLM(), tts, harness=fast))
    assert _spoken(texts) == [("Crows have three hearts.", False)]
    assert fast.heard == ["tell me something fun"]
    assert tts.calls == [("Crows have three hearts.", None)]        # one voice: the same synth as every reply


def test_slow_chat_reply_is_covered_by_filler(tmp_path):
    tts, fast = RecordingTTS(), SlowHarness(1.0, ["Crows have three hearts.", "And blue blood."])
    texts = asyncio.run(_turn(18175, tmp_path, "tell me something fun", NoLLM(), tts, harness=fast))
    spoken = _spoken(texts)
    fillers = [t for t, f in spoken if f]
    assert spoken[-2:] == [("Crows have three hearts.", False), ("And blue blood.", False)]
    assert fillers[0] in ("Hm, looking into this.", "One sec.") and fillers[-1] in ("Got it.", "Oh, here!")
    assert len(fillers) >= 3                                                  # start, still..., landing
    assert all(a != b for a, b in pairwise(fillers))
    kinds = [(t.get("type"), t.get("state")) for t in texts]
    assert kinds.count(("tts", "start")) == 1 and kinds[-1] == ("tts", "stop")
    first_filler = next(i for i, t in enumerate(texts) if t.get("filler"))
    assert texts[first_filler - 1].get("emotion") == "thinking"              # the face thinks while it plays
    assert [c[0] for c in tts.calls] == ["Crows have three hearts.", "And blue blood."]
    assert tts.calls[1][1] == "Crows have three hearts."                 # the next sentence gets its context


def test_reply_just_after_the_first_filler_fades_straight_into_the_answer(tmp_path):
    # the bridge host's chit-chat case: reply audio ~0.3 s after the first filler; a landing line here only added delay
    tts, fast = RecordingTTS(), SlowHarness(0.4, ["Crows have three hearts."])
    spoken = _spoken(asyncio.run(_turn(18179, tmp_path, "tell me something fun", NoLLM(), tts, harness=fast)))
    assert [f for _, f in spoken] == [True, False]                            # one filler, cut, then the answer
    assert spoken[0][0] in ("Hm, looking into this.", "One sec.")


def test_slow_deep_reply_is_covered_and_spoken_sentence_by_sentence(tmp_path):
    tts = RecordingTTS()
    texts = asyncio.run(_turn(18177, tmp_path, "what's in my notes about the dishwasher", NoLLM(), tts,
                              deep_delay=0.8,
                              deep_reply="You have two meetings. The first is at ten."))
    spoken = _spoken(texts)
    assert spoken[0][1] and spoken[-2:] == [("You have two meetings.", False), ("The first is at ten.", False)]
    assert [c[0] for c in tts.calls] == ["You have two meetings.", "The first is at ten."]


def test_without_the_harness_live_questions_go_to_deep_not_plain_chat(tmp_path):
    # SKWOOP_HARNESS=0 or no HA token: FAST turns must not fall back to a model that would guess "No, the TV is off."
    tts = RecordingTTS()
    texts = asyncio.run(_turn(18181, tmp_path, "Is the TV on?", NoLLM(), tts, deep_delay=0.0,
                              deep_reply="The Apple TV is idle."))
    assert _spoken(texts)[-1] == ("The Apple TV is idle.", False)


def test_harness_hands_off_to_deep_with_its_facts_under_one_filler_run(tmp_path):
    tts, facts = RecordingTTS(), []
    fast = SlowHarness(0.3, [], escalate="needs several steps")
    texts = asyncio.run(_turn(18183, tmp_path, "can you check what the bots are working on", NoLLM(), tts,
                              harness=fast, deep_delay=0.8, deep_reply="Deep ran the inbox sweep.",
                              deep_facts=facts))
    spoken = _spoken(texts)
    assert spoken[0][1] and spoken[-1] == ("Deep ran the inbox sweep.", False)
    assert facts == [["lights: the living room light is on"]]                # Deep gets what was looked up
    kinds = [(t.get("type"), t.get("state")) for t in texts]
    assert kinds.count(("tts", "start")) == 1                                # one stream, filler straight into Deep


def test_question_about_a_light_never_fires_the_script(tmp_path, monkeypatch):
    fired: list[str] = []

    async def no_fire(http, hit):
        fired.append(hit.ack_id)
        return hit

    monkeypatch.setattr(house, "_fire", no_fire)
    tts, fast = RecordingTTS(), SlowHarness(0.0, ["Yes, it's on at 40 percent."])
    texts = asyncio.run(_turn(18185, tmp_path, "Is the bedroom light on?", NoLLM(), tts, harness=fast))
    assert fired == [] and fast.heard == ["Is the bedroom light on?"]
    assert _spoken(texts) == [("Yes, it's on at 40 percent.", False)]


def test_plain_command_fires_the_script_instantly(tmp_path, monkeypatch):
    fired: list[str] = []

    async def fake_fire(http, hit):
        fired.append(hit.ack_id)
        return hit

    monkeypatch.setattr(house, "_fire", fake_fire)
    fast = SlowHarness(0.0, ["never"])
    texts = asyncio.run(_turn(18187, tmp_path, "Turn off the living room light.", NoLLM(), RecordingTTS(),
                              harness=fast))
    assert fired == ["living_off"] and fast.heard == []
    assert len(_spoken(texts)) == 1 and not _spoken(texts)[0][1]


def test_pending_text_confirm_is_answered_before_routing(tmp_path):
    sent: list[tuple[str, str]] = []

    async def sender(phone: str, body: str) -> messaging.SendResult:
        sent.append((phone, body))
        return "sent"

    texts = messaging.Texts({"mom": messaging.Contact("mom", "Mom", "+15550000001")}, sender)
    texts.propose("web-pod-turns", "mom", "running late")
    fast = SlowHarness(0.0, ["never"])
    out = asyncio.run(_turn(18189, tmp_path, "yes send it", NoLLM(), RecordingTTS(), harness=fast, texts=texts))
    assert _spoken(out) == [("Sent to Mom.", False)]
    assert sent == [("+15550000001", "running late.")] and fast.heard == []


class StreamingTTS(StubTTS):
    """synth_stream: the first sentence is slow to synthesize, later ones quick; records start/end times."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, float]] = []

    async def synth(self, text: str, mood: str | None = None, previous_text: str | None = None,
                    next_text: str | None = None) -> bytes:
        return b"".join([c async for c in self.synth_stream(text, mood, previous_text)])

    async def synth_stream(self, text: str, mood: str | None = None, previous_text: str | None = None,
                           next_text: str | None = None):
        loop = asyncio.get_running_loop()
        self.log.append(("start", text, loop.time()))
        await asyncio.sleep(0.3 if text.startswith("First") else 0.01)
        yield b"\x01\x00" * 1000
        yield b"\x02\x00" * 1000
        self.log.append(("end", text, loop.time()))


def test_sentences_synthesize_in_parallel_and_play_in_order(tmp_path):
    tts = StreamingTTS()
    fast = SlowHarness(0.0, ["First sentence takes a while.", "Second is quick.", "Third too."])
    texts = asyncio.run(_turn(18177, tmp_path, "tell me three things", NoLLM(), tts, harness=fast))
    assert [t for t, filler in _spoken(texts) if not filler] == ["First sentence takes a while.", "Second is quick.",
                                                                 "Third too."]
    start2 = next(t for ev, s, t in tts.log if ev == "start" and s == "Second is quick.")
    end1 = next(t for ev, s, t in tts.log if ev == "end" and s.startswith("First"))
    assert start2 < end1                                   # sentence 2 was synthesizing while 1 still was


def test_pacer_carries_partial_frames_and_counts_underruns():
    sent: list[bytes] = []
    now = [0.0]

    async def send(frame: bytes) -> None:
        sent.append(frame)

    async def go() -> skwoop_bridge.FramePacer:
        p = skwoop_bridge.FramePacer(send, lead=1000.0, clock=lambda: now[0])
        step = skwoop_bridge.OUT_FRAME * 2
        await p.feed(b"\x01" * (step + step // 2))      # 1.5 frames: one goes out, half waits
        assert len(sent) == 1
        await p.feed(b"\x02" * (step // 2))             # the next sentence completes it: no silence between
        assert len(sent) == 2 and sent[1] == b"\x01" * (step // 2) + b"\x02" * (step // 2)
        now[0] = 5.0                                     # audio arrived after the client ran dry
        await p.feed(b"\x03" * step)
        await p.feed(b"\x04" * 10)
        await p.flush()                                  # the end of the reply is padded once
        assert len(sent) == 4 and sent[3].startswith(b"\x04" * 10) and sent[3].endswith(b"\x00")
        return p

    assert asyncio.run(go()).underruns == 1


# ---------------------------------------------------------------- Oct 4: music asks skip Deep (music.py)

async def _music_turn(port: int, tmp: Path, said: str, tts: RecordingTTS, off: bool = False) -> list[dict[str, object]]:
    auth = tmp / "auth.json"
    auth.write_text('{"providers": {"spotify": {"access_token": "tok", "expires_at": "2099-01-01T00:00:00+00:00"}}}')
    if off:
        (tmp / "music-fast.off").touch()

    async def no_refresh(force: bool) -> None:
        raise AssertionError("a fresh token needs no refresh")

    fake = FakeSpotify()
    runner = web.AppRunner(fake.app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port + 2).start()
    try:
        async with ClientSession() as http:
            spotify = Spotify(http, TokenSource(auth, no_refresh), base=f"http://127.0.0.1:{port + 2}/v1")
            music = Music(http, spotify, pick_url=f"http://127.0.0.1:{port + 2}/pick/v1/chat/completions",
                          off_flag=tmp / "music-fast.off")
            return await _turn(port, tmp, said, NoLLM(), tts, deep_delay=0.3, deep_reply="Deep played it.",
                               music=music)
    finally:
        await runner.cleanup()


def test_clear_music_ask_plays_without_deep_or_filler(tmp_path):
    tts = RecordingTTS()
    texts = asyncio.run(_music_turn(18401, tmp_path, "Play Mr. Brightside.", tts))
    assert _spoken(texts) == [("Playing Mr. Brightside by The Killers.", False)]


def test_music_fast_path_off_hands_the_ask_to_deep(tmp_path):
    tts = RecordingTTS()
    texts = asyncio.run(_music_turn(18405, tmp_path, "Play Mr. Brightside.", tts, off=True))
    assert _spoken(texts)[-1] == ("Deep played it.", False)
