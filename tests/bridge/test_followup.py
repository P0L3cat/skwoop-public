"""Follow-up listen (Oct 4 2026): after a reply finishes playing, the bridge keeps listening SKWOOP_FOLLOWUP_SECS
(default 3) for an answer; with no speech it stops listening, tells the pod ("listen stop", reason followup_timeout)
and closes a xiaozhi pod's channel so it goes back to idle / the wake word. The web pod keeps its socket."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import math
from array import array

import skwoop_bridge
from test_tone import FakeWS, StubLLM, StubTTS, ToneSTT

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"
FRAME = skwoop_bridge.FRAME_MS
SILENCE = b"\x00\x00" * (16000 * FRAME // 1000)
VOICE = array("h", (int(math.sin(i / 3.0) * 6000) for i in range(16000 * FRAME // 1000))).tobytes()


class ClosingWS(FakeWS):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def close(self):
        self.closed = True


def _followup(env: dict) -> float:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SKWOOP_FOLLOWUP")} | env
    out = subprocess.run([sys.executable, "-c", "import skwoop_bridge; print(skwoop_bridge.FOLLOWUP_S)"],
                         cwd=BRIDGE, env=clean, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return float(out.stdout.split()[-1])


def test_window_is_3_s_by_default_and_configurable():
    assert _followup({}) == 3.0
    assert _followup({"SKWOOP_FOLLOWUP_SECS": "5"}) == 5.0
    assert _followup({"SKWOOP_FOLLOWUP_S": "8"}) == 8.0                       # the old name still works
    assert _followup({"SKWOOP_FOLLOWUP_SECS": "2", "SKWOOP_FOLLOWUP_S": "8"}) == 2.0


def _turn(device="aa:bb:cc:00:00:01", feed=SILENCE, stream=True, during=None, window=0.3, wait=1.0, interim=None):
    """One spoken reply in auto mode, then `feed` mic frames for `wait` s. Returns (frames, ws, session)."""
    async def go():
        old = skwoop_bridge.FOLLOWUP_S
        skwoop_bridge.FOLLOWUP_S = window
        try:
            app = skwoop_bridge.build_app(0)
            app["stt"], app["llm"], app["tts"] = ToneSTT(), StubLLM(["Sure, it's done."]), StubTTS()
            ws = ClosingWS()
            s = skwoop_bridge.Session(ws, device, app)
            s.mode = "auto"
            await s.respond(b"\x00\x00" * 16000)
            assert s.listening and s.followup_listen                  # the follow-up window opened
            s.use_stream = stream                                      # a pod streaming its mic into STT
            if interim is not None:                                    # what the live STT stream has heard so far
                s.stt_stream = type("Stream", (), {"interims": 4, "interim_text": interim})()
            t = 0.0
            while t < wait and not ws.closed:
                if during and abs(t - during[0]) < 1e-6:
                    await s.on_text(json.dumps(during[1]))
                if s.listening:
                    s._accept_pcm(feed)
                await asyncio.sleep(FRAME / 1000)
                t = round(t + FRAME / 1000, 3)
            s._cancel_followup_timer()
            return ws.frames, ws, s
        finally:
            skwoop_bridge.FOLLOWUP_S = old
    return asyncio.run(go())


def _stops(frames):
    return [f for f in frames if f.get("type") == "listen" and f.get("state") == "stop"]


def test_silence_after_the_reply_stops_listening_and_sends_the_pod_to_idle():
    frames, ws, s = _turn()
    assert [f["reason"] for f in _stops(frames)] == ["followup_timeout"]
    assert ws.closed                                                   # xiaozhi / Pod Studio: idle, wake word
    assert not s.listening and not s.followup_listen


def test_empty_stt_interims_on_silence_still_time_out():
    # xAI sends an empty interim about every second on silence (seen live Oct 4): that is not Alex talking
    frames, ws, s = _turn(interim="")
    assert _stops(frames) and ws.closed


def test_stt_hearing_words_keeps_listening():
    frames, ws, s = _turn(interim="yeah can you")
    assert not _stops(frames) and not ws.closed


def test_batch_stt_pod_times_out_too():
    frames, ws, s = _turn(stream=False)
    assert _stops(frames) and ws.closed


def test_speech_in_the_window_keeps_listening():
    frames, ws, s = _turn(feed=VOICE, wait=0.9)
    assert not _stops(frames) and not ws.closed and s.listening


def test_web_pod_gets_listen_stop_but_keeps_its_socket():
    frames, ws, s = _turn(device="web-pod-desk")
    assert _stops(frames) and not ws.closed and not s.listening


def test_push_to_talk_inside_the_window_cancels_the_timeout():
    start = {"type": "listen", "state": "start", "mode": "manual"}
    frames, ws, s = _turn(during=(0.12, start))
    assert not _stops(frames) and not ws.closed and s.listening


def test_firmware_auto_reopen_keeps_the_window():
    # xiaozhi firmware sends "listen start auto" itself right after "tts stop": the 3 s window still applies
    start = {"type": "listen", "state": "start", "mode": "auto"}
    frames, ws, s = _turn(during=(0.06, start))
    assert _stops(frames) and ws.closed


def test_zero_disables_the_follow_up():
    async def go():
        old = skwoop_bridge.FOLLOWUP_S
        skwoop_bridge.FOLLOWUP_S = 0.0
        try:
            app = skwoop_bridge.build_app(0)
            app["stt"], app["llm"], app["tts"] = ToneSTT(), StubLLM(["Done."]), StubTTS()
            s = skwoop_bridge.Session(ClosingWS(), "aa:bb:cc:00:00:01", app)
            s.mode = "auto"
            await s.respond(b"\x00\x00" * 16000)
            return s
        finally:
            skwoop_bridge.FOLLOWUP_S = old
    s = asyncio.run(go())
    assert not s.listening and not s.followup_listen
