"""ElevenLabs TTS tests against a fake ElevenLabs HTTP server on 127.0.0.1: no keys, no network, no cost.
The xAI fallback is stubbed too.   python -m pytest -q tests/test_tts_eleven.py
"""
import asyncio
import json
import math
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
_tmp = tempfile.mkdtemp()
os.environ.setdefault("STATUS_FILE", os.path.join(_tmp, "status.json"))
os.environ.setdefault("INBOX_DIR", os.path.join(_tmp, "inbox"))

import pytest  # noqa: E402
from lean_http import ClientSession, web  # noqa: E402

import skwoop_bridge  # noqa: E402
from stubs import StubLLM, StubSTT  # noqa: E402
from providers import TTS, TTS_RATE  # noqa: E402

PORT = 18120
VOICE = "voice-test-123"
# 0.25 s of a 440 Hz tone, s16le mono @ 24 kHz: what pcm_24000 should look like
PCM = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / TTS_RATE)))
               for i in range(TTS_RATE // 4))
XAI_PCM = b"\x01\x00" * 100
ENV_KEYS = ("TTS_PROVIDER", "TTS_FALLBACK", "XAI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
            "ELEVENLABS_MODEL", "ELEVENLABS_BASE_URL", "ELEVENLABS_TIMEOUT_S", "ELEVENLABS_ENDPOINT",
            "ELEVENLABS_TONE", "ELEVENLABS_STABILITY", "TTS_LANGUAGE", "XAI_TTS_VOICE", "XAI_FALLBACK_VOICE")


class FakeEleven:
    """Records requests; mode = ok | error | slow."""
    def __init__(self):
        self.mode, self.requests = "ok", []

    async def handle(self, request):
        self.requests.append({"path": request.path, "query": dict(request.query),
                              "key": request.headers.get("xi-api-key"), "body": await request.json()})
        if self.mode == "error":
            return web.json_response({"detail": {"status": "quota_exceeded"}}, status=401)
        if self.mode == "slow":
            await asyncio.sleep(2)
        resp = web.StreamResponse(headers={"Content-Type": "audio/pcm", "character-cost": "7"})
        await resp.prepare(request)
        for i in range(0, len(PCM), 1001):  # odd-sized chunks, like a real stream
            await resp.write(PCM[i:i + 1001])
            if self.mode == "drip" and i == 0:
                await asyncio.sleep(0.3)
            if self.mode == "cut" and i >= 2002:
                request.transport.close()           # connection drops mid-sentence
                return resp
        await resp.write_eof()
        return resp


async def _fake_server(fake):
    app = web.Application()
    app.router.add_post("/v1/text-to-speech/{voice}/stream", fake.handle)
    app.router.add_post("/v1/text-to-dialogue/stream", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    return runner


@pytest.fixture(autouse=True)
def clean_env():
    saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    os.environ.update(TTS_PROVIDER="elevenlabs", XAI_API_KEY="test-not-a-key", ELEVENLABS_API_KEY="test-el-key",
                      ELEVENLABS_VOICE_ID=VOICE, ELEVENLABS_BASE_URL=f"http://127.0.0.1:{PORT}",
                      ELEVENLABS_TIMEOUT_S="0.5")
    yield
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


def _tts(http):
    t = TTS(http)
    t.xai_calls = []

    async def fake_xai(text, voice=None):
        t.xai_calls.append(text)
        t.xai_voices = getattr(t, "xai_voices", []) + [voice]
        return XAI_PCM
    t._xai = fake_xai
    return t


def _run(mode, texts, setup=None):
    """Synthesize texts against the fake server; returns (results, tts, fake)."""
    async def go():
        fake = FakeEleven()
        fake.mode = mode
        runner = await _fake_server(fake)
        try:
            async with ClientSession() as http:
                t = _tts(http)
                if setup:
                    setup(t)
                out = []
                for text in texts:
                    try:
                        out.append(await t.synth(text))
                    except Exception as e:  # noqa: BLE001
                        out.append(e)
                return out, t, fake
        finally:
            await runner.cleanup()
    return asyncio.run(go())


def test_success_returns_pcm_and_sends_the_right_request():
    (pcm,), t, fake = _run("ok", ["Skwoop is deploying the bridge."])
    assert pcm == PCM                                   # bytes passed through untouched
    assert len(pcm) % 2 == 0 and len(pcm) / 2 / TTS_RATE == 0.25
    assert t.last_backend == "elevenlabs" and t.xai_calls == [] and t.fallbacks == 0
    (req,) = fake.requests
    assert req["path"] == f"/v1/text-to-speech/{VOICE}/stream"
    assert req["query"] == {"output_format": "pcm_24000"}
    assert req["key"] == "test-el-key"
    # Oct 2: ELEVENLABS_TONE=mood is the default: the try-8 library's delivery tag for the reply's mood
    assert req["body"] == {"text": "[warmly] Skwoop is deploying the bridge.", "model_id": "eleven_v4_turbo"}


def test_model_and_stability_from_env():
    os.environ.update(ELEVENLABS_MODEL="eleven_flash_v2_5", ELEVENLABS_STABILITY="0.4", TTS_LANGUAGE="en")
    _, _, fake = _run("ok", ["Hello there, Alex."])
    body = fake.requests[0]["body"]
    assert body["model_id"] == "eleven_flash_v2_5"
    assert body["voice_settings"] == {"stability": 0.4} and body["language_code"] == "en"


def test_http_error_falls_back_to_xai():
    (pcm,), t, fake = _run("error", ["The deploy failed."])
    assert pcm == XAI_PCM and t.xai_calls == ["The deploy failed."]
    assert t.last_backend == "xai-fallback" and t.fallbacks == 1
    assert t.last_error == "ElevenLabs HTTP 401" and len(fake.requests) == 1


def test_timeout_falls_back_to_xai():
    (pcm,), t, _ = _run("slow", ["This one is slow."])
    assert pcm == XAI_PCM and t.last_backend == "xai-fallback"
    assert t.last_error == "TimeoutError"


def test_missing_key_falls_back_without_calling_elevenlabs():
    os.environ.pop("ELEVENLABS_API_KEY")
    (pcm,), t, fake = _run("ok", ["No key yet."])
    assert pcm == XAI_PCM and fake.requests == [] and t.last_backend == "xai-fallback"


def test_fallback_uses_the_skwoop_like_xai_voice():
    (pcm,), t, _ = _run("error", ["Fallback voice check."])
    assert pcm == XAI_PCM and t.xai_voices == ["carina"]                 # default: soft, soothing
    os.environ.update(XAI_FALLBACK_VOICE="luna", XAI_TTS_VOICE="eve")
    (pcm,), t, _ = _run("error", ["Fallback voice check."])
    assert t.xai_voices == ["luna"] and t.describe()["fallback_voice"] == "luna"
    assert t.describe()["voice"] == VOICE


def test_no_fallback_raises_and_no_keys_refuses_to_start():
    os.environ["TTS_FALLBACK"] = "none"
    (err,), t, _ = _run("error", ["The deploy failed."])
    assert isinstance(err, RuntimeError) and t.xai_calls == []
    os.environ.pop("ELEVENLABS_API_KEY")
    with pytest.raises(SystemExit):
        TTS(None)


def test_recovers_after_a_failure():
    def flip(t):  # first call fails, second succeeds
        orig = t._elevenlabs
        state = {"n": 0}

        async def flaky(text, *a, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("ElevenLabs HTTP 503")
            return await orig(text, *a, **kw)
        t._elevenlabs = flaky
    (a, b), t, _ = _run("ok", ["One.", "Two."], setup=flip)
    assert a == XAI_PCM and b == PCM
    assert t.last_backend == "elevenlabs" and t.fallbacks == 1 and t.last_error == ""


def test_tone_tags_hint():
    os.environ["ELEVENLABS_TONE"] = "tags"
    _, _, fake = _run("ok", ["The deploy failed.", "All tests passed.", "It is three o'clock."])
    texts = [r["body"]["text"] for r in fake.requests]
    assert texts == ["[gently] The deploy failed.", "[cheerfully] All tests passed.", "It is three o'clock."]
    os.environ["ELEVENLABS_TONE"] = "off"
    _, _, fake = _run("ok", ["The deploy failed."])
    assert fake.requests[0]["body"]["text"] == "The deploy failed."


def test_dialogue_endpoint():
    os.environ.update(ELEVENLABS_ENDPOINT="dialogue", ELEVENLABS_STABILITY="0.5")
    (pcm,), _, fake = _run("ok", ["Hello from the dialogue endpoint."])
    assert pcm == PCM
    req = fake.requests[0]
    assert req["path"] == "/v1/text-to-dialogue/stream" and req["query"] == {"output_format": "pcm_24000"}
    assert req["body"] == {"inputs": [{"text": "[warmly] Hello from the dialogue endpoint.", "voice_id": VOICE}],
                           "model_id": "eleven_v4_turbo", "settings": {"stability": 0.5}}


def test_healthz_fields():
    async def go():
        fake = FakeEleven()
        el = await _fake_server(fake)
        app = skwoop_bridge.build_app(18121)
        app.on_startup.clear()
        app.on_cleanup.clear()
        app["stt"] = StubSTT()
        app["llm"] = StubLLM()
        http = ClientSession()
        app["tts"] = t = _tts(http)
        app["store"].inbox.synth = t.synth       # what on_startup does: messages render at queue time
        app["store"].inbox.write_key = "test-only-key"
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 18121).start()
        seen = []
        try:
            async with http:
                async def hz():
                    async with http.get("http://127.0.0.1:18121/healthz") as r:
                        seen.append(await r.json())
                await hz()                       # nothing spoken yet
                await t.synth("First reply.")
                await hz()                       # ElevenLabs worked
                async with http.post("http://127.0.0.1:18121/inbox", json={"from": "Skwoop", "text": "Build is green."},
                                     headers={"Authorization": "Bearer test-only-key"}) as r:
                    seen.append((r.status, await r.json()))
                fake.mode = "error"
                await t.synth("Second reply.")
                await hz()                       # fell back
        finally:
            await runner.cleanup()
            await el.cleanup()
        return seen

    start, ok, (inbox_status, inbox), fell = asyncio.run(go())
    assert inbox_status == 200 and inbox["item"]["duration_s"] >= 0.25   # bridge wiring: inbox used ElevenLabs
    print("\n[/healthz]", json.dumps(start), "\n[/healthz]", json.dumps(ok), "\n[/healthz]", json.dumps(fell))
    assert start["tts"] == "elevenlabs" and start["tts_model"] == "eleven_v4_turbo"
    assert start["tts_fallback"] == "xai" and start["tts_last_backend"] is None and start["tts_fallbacks"] == 0
    assert ok["tts_last_backend"] == "elevenlabs" and ok["tts_last_error"] is None
    assert fell["tts_last_backend"] == "xai-fallback" and fell["tts_fallbacks"] == 1
    assert fell["tts_last_error"] == "ElevenLabs HTTP 401"


def test_healthz_unchanged_for_other_providers():
    os.environ["TTS_PROVIDER"] = "xai"
    t = TTS(None)
    assert t.describe() == {"provider": "xai", "fallback": None, "last_backend": None}


def _stream(mode, text):
    """synth_stream against the fake server -> (chunks with arrival times, tts, charges)."""
    async def go():
        fake = FakeEleven()
        fake.mode = mode
        runner = await _fake_server(fake)
        try:
            async with ClientSession() as http:
                t = _tts(http)
                t.library = None
                charges = []
                t.guard.charge = lambda text, cost: charges.append(cost) or 0.0
                t0, got = asyncio.get_running_loop().time(), []
                async for c in t.synth_stream(text):
                    got.append((asyncio.get_running_loop().time() - t0, c))
                return got, t, charges
        finally:
            await runner.cleanup()
    return asyncio.run(go())


def test_stream_plays_the_first_audio_before_the_sentence_is_done():
    got, t, charges = _stream("drip", "A long first sentence.")
    assert b"".join(c for _, c in got) == PCM and all(len(c) % 2 == 0 for _, c in got)
    assert got[0][0] < 0.25 and got[-1][0] >= 0.3          # first chunk before the server finished
    assert t.last_backend == "elevenlabs" and charges == [7.0] and t.xai_calls == []


def test_stream_error_before_audio_falls_back_to_xai():
    got, t, charges = _stream("error", "This one fails.")
    assert [c for _, c in got] == [XAI_PCM] and t.last_backend == "xai-fallback" and charges == []


def test_stream_cut_midway_stops_without_repeating_in_another_voice():
    got, t, charges = _stream("cut", "This one drops.")
    audio = b"".join(c for _, c in got)
    assert 0 < len(audio) < len(PCM) and PCM.startswith(audio)
    assert t.xai_calls == [] and charges == [7.0]          # billed once, no xAI repeat


def test_stream_off_switch_gives_one_piece(monkeypatch):
    monkeypatch.setenv("SKWOOP_TTS_STREAM", "0")
    got, t, _ = _stream("ok", "One piece please.")
    assert [c for _, c in got] == [PCM]


def test_parallel_streams_each_charge_their_own_cost():
    async def go():
        fake = FakeEleven()
        fake.mode = "drip"
        runner = await _fake_server(fake)
        try:
            async with ClientSession() as http:
                t = _tts(http)
                t.library = None
                charges = []
                t.guard.charge = lambda text, cost: charges.append((text, cost)) or 0.0

                async def one(text):
                    return b"".join([c async for c in t.synth_stream(text)])
                await asyncio.gather(one("First of two."), one("Second of two."))
                return charges
        finally:
            await runner.cleanup()
    charges = asyncio.run(go())
    assert sorted(charges) == [("First of two.", 7.0), ("Second of two.", 7.0)]
