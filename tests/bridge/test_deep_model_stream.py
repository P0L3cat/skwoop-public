"""Deep turns streamed from Deep's api_server: the filter that keeps only the answer, the SSE client against a
fake api_server, the session it saves, and the bridge speaking the first sentence before Deep is done."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from lean_http import ClientSession
from lean_http import web
from test_turns import NoLLM, RecordingTTS, _spoken, _turn

import harness.deep_model as deep_model
import harness.skwoop_turn as skwoop_turn


# ------------------------------------------------------------------ SpeechFilter
def _speak(deltas: list[str | None]) -> tuple[list[str], deep_model.SpeechFilter]:
    f = deep_model.SpeechFilter()
    out: list[str] = []
    for d in deltas:
        if d is None:
            f.tool()
        else:
            out += f.feed(d)
    return out + f.finish(), f


def test_filter_speaks_sentences_as_they_complete():
    f = deep_model.SpeechFilter()
    assert f.feed("Latest stable is twenty twenty-six point ") == []
    assert f.feed("nine point four. Big pieces are ") == ["Latest stable is twenty twenty-six point nine point four."]
    assert f.feed("Modbus devices in the UI.") == []
    assert f.finish() == ["Big pieces are Modbus devices in the UI."]


def test_filter_drops_the_announcement_the_ack_nudge_reacts_to():
    said, f = _speak([("I'll look up the current Home Assistant release notes.Latest stable is twenty twenty-six "
                       "point nine point four. Modbus is in the UI now.")])
    assert said == ["Latest stable is twenty twenty-six point nine point four.", "Modbus is in the UI now."]
    assert f.dropped == ["I'll look up the current Home Assistant release notes."]


def test_filter_drops_narration_before_a_tool_call_and_repeats_after_a_nudge():
    said, _ = _speak(["Checking the family wiki", None,
                      "The dishwasher filter gets cleaned monthly. It's on the appliances page. ",
                      "The dishwasher filter gets cleaned every month. It is on the appliances page."])
    assert said == ["The dishwasher filter gets cleaned monthly.", "It's on the appliances page."]


def test_filter_stops_when_the_nudged_rewrite_starts():
    """Recorded 19:14 CT: preamble run into the answer, then Deep's nudge and grok's paraphrased rewrite."""
    said, f = _speak([
        ("I'll look up the current Home Assistant release notes.Latest stable is twenty twenty-six point nine point "
         "four from late September. Big stuff was Modbus devices you pick in the UI. "),
        "Latest stable is twenty twenty-six point nine, last patched as point nine point four in late September. ",
        "Big items were Modbus devices you pick in the UI, plus better cloud speech-to-text."])
    assert said == ["Latest stable is twenty twenty-six point nine point four from late September.",
                    "Big stuff was Modbus devices you pick in the UI."]
    assert f.restarted


def test_filter_caps_the_reply_and_keeps_a_real_first_sentence_that_starts_with_i():
    said, _ = _speak([("I found two notes about the dishwasher. The first note is from June. The second one is "
                       "from August. Both say the filter is cleaned monthly. Nothing newer than that turned up.")])
    assert said[0] == "I found two notes about the dishwasher." and len(said) == deep_model.MAX_SENTENCES


def test_filter_strips_markdown():
    said, _ = _speak(["**Latest** stable is `2026.9.4`."])
    assert said == ["Latest stable is 2026.9.4."]


# ------------------------------------------------------------------ prompt
def test_prompt_is_the_webhook_routes_own(tmp_path, monkeypatch):
    routes = tmp_path / "subs.json"
    routes.write_text(json.dumps({"skwoop-voice": {"prompt": "Heard: {command}\nNotes: {context}\nReply short."},
                                  "siri-voice": {"prompt": "x"}}))
    monkeypatch.setattr(deep_model, "ROUTES_FILE", routes)
    assert deep_model.render_prompt("what's new", "(new pod session)") == \
        "Heard: what's new\nNotes: (new pod session)\nReply short."
    routes.write_text(json.dumps({"skwoop-voice": {"prompt": "Heard: {command} for {user}"}}))
    with pytest.raises(deep_model.StreamUnavailable):
        deep_model.render_prompt("a", "b")
    routes.unlink()
    with pytest.raises(deep_model.StreamUnavailable):
        deep_model.render_prompt("a", "b")


# ------------------------------------------------------------------ against a fake api_server
def _chunk(text: str) -> bytes:
    payload = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}}]}
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _fake_api(port: int, script: list[tuple[float, bytes]], status: int = 200, seen: list | None = None):
    async def handle(request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == "Bearer test-key"
        body = await request.json()
        if seen is not None:
            seen.append(body)
        if status != 200:
            return web.json_response({"error": "nope"}, status=status)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}\n\n')
        for delay, frame in script:
            await asyncio.sleep(delay)
            await resp.write(frame)
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


TOOL = b'event: deep.tool.progress\ndata: {"tool": "search_files", "emoji": "", "label": "search"}\n\n'
SCRIPT = [(0.05, _chunk("Let me check the wiki")), (0.05, TOOL), (0.3, _chunk("The filter is cleaned monthly. ")),
          (0.6, _chunk("It's on the appliances page."))]


@pytest.fixture
def stream_env(tmp_path, monkeypatch):
    routes = tmp_path / "subs.json"
    routes.write_text(json.dumps({"skwoop-voice": {"prompt": "Heard: {command}\nNotes: {context}"}}))
    monkeypatch.setattr(deep_model, "ROUTES_FILE", routes)
    monkeypatch.setattr(skwoop_turn, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setenv("DEEP_MODEL_API_KEY", "test-key")
    return monkeypatch


def test_ask_stream_yields_the_first_sentence_before_deep_finishes(stream_env):
    stream_env.setenv("DEEP_MODEL_BASE_URL", "http://127.0.0.1:18391/v1")
    seen: list = []

    async def go():
        runner = await _fake_api(18391, SCRIPT, seen=seen)
        got: list[tuple[float, str]] = []
        t0 = time.monotonic()
        try:
            async with ClientSession() as http:
                async for s in skwoop_turn.ask_stream(http, "what about the dishwasher", room="office"):
                    got.append((time.monotonic() - t0, s))
        finally:
            await runner.cleanup()
        return got, time.monotonic() - t0

    got, total = asyncio.run(go())
    assert [s for _, s in got] == ["The filter is cleaned monthly.", "It's on the appliances page."]
    assert got[0][0] < total - 0.5                                # spoken ~0.6 s before the stream ended
    content = seen[0]["messages"][0]["content"]
    assert seen[0]["stream"] is True and content.startswith("Heard: what about the dishwasher")
    sess = skwoop_turn.load_session("office")
    assert sess["turns"][-1] == {"heard": "what about the dishwasher",
                                 "reply": "The filter is cleaned monthly. It's on the appliances page."}


@pytest.mark.parametrize("status, url", [(500, "http://127.0.0.1:18393/v1"), (200, "http://127.0.0.1:1/v1")])
def test_ask_stream_is_unavailable_before_anything_is_said(stream_env, status, url):
    stream_env.setenv("DEEP_MODEL_BASE_URL", url)

    async def go():
        runner = await _fake_api(18393, SCRIPT, status=status)
        try:
            async with ClientSession() as http:
                return [s async for s in skwoop_turn.ask_stream(http, "x", room="office")]
        finally:
            await runner.cleanup()

    with pytest.raises(deep_model.StreamUnavailable):
        asyncio.run(go())


def test_not_configured_means_unavailable(monkeypatch):
    monkeypatch.delenv("DEEP_MODEL_BASE_URL", raising=False)
    assert not deep_model.configured()


def test_off_file_turns_streaming_off_live(stream_env, tmp_path):
    stream_env.setenv("DEEP_MODEL_BASE_URL", "http://127.0.0.1:18399/v1")
    stream_env.setattr(deep_model, "OFF_FILE", tmp_path / "deep-stream.off")
    assert deep_model.enabled()
    (tmp_path / "deep-stream.off").touch()
    assert not deep_model.enabled()

    async def go():
        async with ClientSession() as http:
            return [s async for s in skwoop_turn.ask_stream(http, "x", room="office")]

    with pytest.raises(deep_model.StreamUnavailable, match="off"):
        asyncio.run(go())


def test_a_restart_disconnects_so_deep_stops_the_second_call(stream_env):
    stream_env.setenv("DEEP_MODEL_BASE_URL", "http://127.0.0.1:18401/v1")
    notes = "I'll look up the release notes.Latest stable is twenty twenty-six point nine point four. "
    rewrite = [(0.05, _chunk(notes)),
               (0.05, _chunk("Big stuff was Modbus devices in the UI. ")),
               (0.3, _chunk("Latest stable is twenty twenty-six point nine point four, from late September. "))]
    rewrite += [(0.2, _chunk(f"More rewrite text number {i}. ")) for i in range(15)]

    async def go():
        runner = await _fake_api(18401, rewrite)
        t0 = time.monotonic()
        try:
            async with ClientSession() as http:
                got = [s async for s in skwoop_turn.ask_stream(http, "x", room="office")]
        finally:
            await runner.cleanup()
        return got, time.monotonic() - t0

    got, took = asyncio.run(go())
    assert got == ["Latest stable is twenty twenty-six point nine point four.",
                   "Big stuff was Modbus devices in the UI."]
    assert took < 1.5                                             # left at the rewrite, not after 3 s of it


# ------------------------------------------------------------------ the bridge
def test_bridge_speaks_deep_sentences_as_the_stream_delivers_them(tmp_path):
    arrived: dict[str, float] = {}

    async def stream(http, heard, room="pod", facts=None):
        await asyncio.sleep(0.6)
        arrived["first"] = time.monotonic()
        yield "The filter is cleaned monthly."
        await asyncio.sleep(1.2)
        arrived["last"] = time.monotonic()
        yield "It's on the appliances page."

    tts = RecordingTTS()
    texts = asyncio.run(_turn(18395, tmp_path, "what's in my notes about the dishwasher", NoLLM(), tts,
                              deep_delay=30, deep_reply="webhook must not run",
                              deep_model=stream))
    assert _spoken(texts)[-2:] == [("The filter is cleaned monthly.", False), ("It's on the appliances page.", False)]
    assert [c[0] for c in tts.calls] == ["The filter is cleaned monthly.", "It's on the appliances page."]
    assert arrived["last"] - arrived["first"] > 1.0              # the first sentence did not wait for the second


def test_bridge_falls_back_to_the_webhook_when_the_stream_is_unavailable(tmp_path):
    async def stream(http, heard, room="pod", facts=None):
        raise deep_model.StreamUnavailable("HTTP 503")
        yield ""

    tts = RecordingTTS()
    texts = asyncio.run(_turn(18397, tmp_path, "what's in my notes about the dishwasher", NoLLM(), tts,
                              deep_delay=0.2, deep_reply="From the webhook.",
                              deep_model=stream))
    assert _spoken(texts)[-1] == ("From the webhook.", False)
