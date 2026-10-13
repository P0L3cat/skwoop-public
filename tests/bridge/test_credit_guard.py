"""ElevenLabs credit guard: persisted spend counter, floor (default 1000), fail-closed, xAI fallback + logging,
the character-cost header, /healthz fields. Fake ElevenLabs server on 127.0.0.1: no keys, no network, no cost."""
import asyncio
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pytest  # noqa: E402
from lean_http import ClientSession, web  # noqa: E402

from credit_guard import CreditGuard, CreditState, parse_state  # noqa: E402
from providers import TTS  # noqa: E402

PORT = 18140
ENV = ("TTS_PROVIDER", "TTS_FALLBACK", "XAI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
       "ELEVENLABS_BASE_URL", "ELEVENLABS_TIMEOUT_S", "ELEVEN_CREDIT_FLOOR", "ELEVEN_CREDIT_BALANCE",
       "ELEVEN_CREDIT_STATE")


def test_defaults_and_persistence(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVEN_CREDIT_FLOOR", raising=False)
    p = tmp_path / "c.json"
    g = CreditGuard(state_path=p, balance=5863)
    assert g.floor == 1000 and g.remaining() == 5863
    g.charge("hello", 3)
    g.charge("x" * 100, None)                       # no header: estimate at the stricter measured rate (5/3 chars)
    assert g.remaining() == 5863 - 3 - 60
    g2 = CreditGuard(state_path=p, balance=5863)    # restart: same balance in the env -> keep the counter
    assert g2.remaining() == 5800 and g2.describe()["calls"] == 2
    g3 = CreditGuard(state_path=p, balance=4000)    # the operator updated ELEVEN_CREDIT_BALANCE -> re-seed
    assert g3.remaining() == 4000 and g3.state.spent_total == 63
    assert json.loads(p.read_text())["seed"] == 4000


def test_floor_blocks_and_logs(tmp_path, caplog):
    g = CreditGuard(state_path=tmp_path / "c.json", balance=1010, floor=1000)
    assert g.allow("short")[0]
    g.charge("a", 9)
    with caplog.at_level(logging.WARNING, logger="skwoop.credits"):
        ok, why = g.allow("x" * 100)                 # ~5 credits would cross the floor
    assert not ok and "credit floor" in why and "blocked" in caplog.text
    assert g.describe()["blocked"] == 1 and g.describe()["live_allowed"] is False


def test_fail_closed_without_balance(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVEN_CREDIT_BALANCE", raising=False)
    g = CreditGuard(state_path=tmp_path / "none.json")
    ok, why = g.allow("hi")
    assert not ok and "ELEVEN_CREDIT_BALANCE" in why


class Fake:
    def __init__(self):
        self.n = 0

    async def handle(self, request):
        self.n += 1
        await request.json()
        return web.Response(body=b"\x05\x00" * 4800, headers={"character-cost": "7"})


@pytest.fixture
def env(tmp_path):
    saved = {k: os.environ.pop(k, None) for k in ENV}
    os.environ.update(TTS_PROVIDER="elevenlabs", XAI_API_KEY="test-x", ELEVENLABS_API_KEY="test-e",
                      ELEVENLABS_VOICE_ID="voice-test", ELEVENLABS_BASE_URL=f"http://127.0.0.1:{PORT}",
                      ELEVENLABS_TIMEOUT_S="2", ELEVEN_CREDIT_STATE=str(tmp_path / "c.json"),
                      ELEVEN_CREDIT_BALANCE="1016")
    yield tmp_path
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v


class XaiRecorder(TTS):
    """The real ElevenLabs leg (against the local fake server above); the xAI fallback leg records instead."""

    def __init__(self, http):
        super().__init__(http)
        self.xai_calls: list[tuple[str, str | None]] = []

    async def _xai(self, text, voice=None):
        self.xai_calls.append((text, voice))

        return b"\x09\x00" * 100


def _run(texts):
    async def go():
        fake = Fake()
        app = web.Application()
        app.router.add_post("/v1/text-to-speech/{voice}/stream", fake.handle)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        try:
            async with ClientSession() as http:
                t = XaiRecorder(http)
                out = []
                for x in texts:
                    out.append((await t.synth(x), t.last_backend))

                return out, t, fake, t.xai_calls
        finally:
            await runner.cleanup()

    return asyncio.run(go())


def test_live_tts_charges_header_then_falls_back_to_xai_below_floor(env, caplog):
    with caplog.at_level(logging.WARNING):
        out, t, fake, xai = _run(["First live line.", "Second live line.", "Third live line.", "Fourth."])
    backends = [b for _, b in out]
    # 1016 left, floor 1000: 7 + 7 = 14 spent (header); the estimate then learns ~2.3 chars/credit from the
    # headers, so the third (~7) and fourth (~4) would cross the floor -> xAI
    assert backends == ["elevenlabs", "elevenlabs", "xai-fallback", "xai-fallback"], backends
    assert fake.n == 2 and len(xai) == 2 and xai[0][1] == "carina"
    assert t.guard.remaining() == 1002
    assert "credit floor" in caplog.text
    st = json.loads((env / "c.json").read_text())
    assert st["spent"] == 14 and st["blocked"] == 2
    d = t.describe()["credits"]
    assert d["floor"] == 1000 and d["live_allowed"] is False and d["blocked"] == 2


def test_floor_env_override(env):
    os.environ["ELEVEN_CREDIT_FLOOR"] = "0"
    out, t, fake, xai = _run(["One.", "Two.", "Three."])
    assert [b for _, b in out] == ["elevenlabs"] * 3 and fake.n == 3


def test_state_file_is_parsed_at_the_boundary(tmp_path):
    assert parse_state([1, 2]) == CreditState()
    st = parse_state({"seed": 900, "spent": "lots", "calls": 3, "blocked": True, "last_call": 5})
    assert st.seed == 900.0 and st.spent == 0.0 and st.calls == 3 and st.blocked == 0 and st.last_call is None
    p = tmp_path / "c.json"
    p.write_text("{not json")
    g = CreditGuard(state_path=p, balance=2000)                 # a corrupt state file re-seeds, never crashes
    assert g.remaining() == 2000
