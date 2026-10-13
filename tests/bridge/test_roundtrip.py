"""Local round trip: real bridge with the e2e fakes, real web pod in front, both on port 0.

one_turn goes through the web pod. fake_pod talks to the bridge directly for one turn.
No live pod, no network.
"""
from __future__ import annotations

import asyncio
import math
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import test_e2e  # noqa: E402 - sets the bridge env, then imports skwoop_bridge
import fake_pod  # noqa: E402
import skwoop_bridge  # noqa: E402
import webpod_smoke  # noqa: E402
from lean_http import Application, AppRunner, web  # noqa: E402
from providers import pcm_to_wav  # noqa: E402
from webpod import server as wp  # noqa: E402


class _BusyLLM(test_e2e.StubLLM):
    """Same stub replies as test_e2e.StubLLM, with a typed constructor."""

    def __init__(self) -> None:
        self.last_system = ""


class _Pod:
    """The attributes fake_pod.run reads for one manual turn."""

    def __init__(self, server: str, wav: str, out: str) -> None:
        self.server = server
        self.wav = wav
        self.out = out
        self.say: str | None = None
        self.mode = "manual"
        self.timeout = 20.0
        self.fast = True
        self.device: str | None = None


def _tone(path: Path) -> None:
    rate = 16000
    n = int(rate * 1.2)
    pcm = b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(n))
    path.write_bytes(pcm_to_wav(pcm, rate))


async def _listen(app: Application) -> tuple[AppRunner, int]:
    runner = AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()

    return runner, runner.addresses[-1][1]


async def _roundtrip(tmp: Path) -> tuple[webpod_smoke.TurnResult, int]:
    app = skwoop_bridge.build_app(0)
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["stt"], app["llm"], app["tts"] = test_e2e.E2eSTT(), _BusyLLM(), test_e2e.StubTTS()
    app["store"].inbox.synth = app["tts"].synth
    app["store"].upsert_bot("Skwoop", "busy", "deploying")
    app["store"].upsert_bot("helper", "ok")
    bridge, bridge_port = await _listen(app)
    app["port"] = bridge_port
    side_app = wp.make_app(wp.WebPodConfig(
        host="127.0.0.1", port=0, tls_dir=HERE.parent / "no-tls-here", access_key="",
        bridge_url=f"ws://127.0.0.1:{bridge_port}/xiaozhi/v1/", device_token=""))
    side, side_port = await _listen(side_app)
    try:
        wav = tmp / "q.wav"
        _tone(wav)
        res = await webpod_smoke.one_turn(
            f"ws://127.0.0.1:{side_port}/pod", None, "smoke", "", webpod_smoke.read_pcm16k(wav), 20.0)
        pod = await fake_pod.run(_Pod(f"http://127.0.0.1:{bridge_port}", str(wav), str(tmp / "reply.wav")))
    finally:
        await side.cleanup()
        await bridge.cleanup()

    return res, pod


def test_roundtrip(tmp_path: Path) -> None:
    res, pod = asyncio.run(_roundtrip(tmp_path))
    assert res.errors == [], res.errors
    assert res.device == "web-pod-smoke"
    assert res.heard == "what is the team doing"
    assert "Skwoop is deploying the bridge." in res.said
    assert "Everything else is quiet." in res.said
    assert res.first_audio_s is not None and res.first_reply_s is not None
    assert len(res.pcm) > 40000
    assert pod == 0
