"""Barge-in: the detector on synthetic levels, and an ESP32-style pod sim against the real bridge (the pod keeps
streaming its echo-cancelled mic while Skwoop talks and declares features.barge_in = "server")."""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from array import array

import barge
import opus
import skwoop_bridge
from dsp import resample
from filler import FillerSet, FillerTiming
from stubs import StubLLM, StubSTT, StubTTS
from test_filler import SMALL, _write_set
from lean_http import ClientSession
from lean_ws import WSMsgType
from test_webpod import _serve


def _run_levels(levels: list[tuple[float, float]]) -> int | None:
    """(mic dB, ref dB) per 20 ms frame -> index of the frame that fired, or None."""
    d = barge.BargeInDetector()
    for i, (mic, ref) in enumerate(levels):
        if d.push(mic, ref, 20):
            return i
    return None


def _jit(rng: random.Random, x: float, s: float = 2.0) -> float:
    return x + rng.uniform(-s, s)


def test_echo_alone_never_fires_with_or_without_aec():
    rng = random.Random(1)
    for echo_rel in (-30.0, -6.0):                      # AEC'd residual, and a plain speaker next to the mic
        lv = [(_jit(rng, -20 + echo_rel, 3), _jit(rng, -20, 3)) for _ in range(500)]   # 10 s of reply
        assert _run_levels(lv) is None, echo_rel


def test_voice_over_playback_fires_within_300_ms():
    rng = random.Random(2)
    lv = [(_jit(rng, -45), _jit(rng, -20)) for _ in range(50)]         # 1 s: echo 25 dB under the reply
    lv += [(_jit(rng, -18), _jit(rng, -20)) for _ in range(50)]        # Alex talks
    hit = _run_levels(lv)
    assert hit is not None and 50 <= hit <= 50 + 15


def test_voice_between_sentences_fires_on_the_floor_alone():
    rng = random.Random(3)
    lv = [(_jit(rng, -65), -120.0) for _ in range(30)]                 # quiet room, nothing playing
    lv += [(_jit(rng, -30), -120.0) for _ in range(30)]
    hit = _run_levels(lv)
    assert hit is not None and 30 <= hit <= 45


def test_loud_start_of_a_reply_only_trains_the_echo():
    lv = [(-14.0, -20.0)] * 15 + [(-14.0, -20.0)] * 100                # loud echo from the first frame on
    assert _run_levels(lv) is None


def test_reference_window():
    r = barge.Reference(window_s=0.5)
    loud = array("h", [8000] * 960).tobytes()
    r.sent(10.0, loud)
    assert r.active(10.3) and r.db(10.3) > -15 and not r.active(10.6) and r.db(10.6) == -120.0


# ------------------------------------------------------------------ pod sim
IN, OUT = 16000, 24000
FRAME_IN, FRAME_OUT = IN * 60 // 1000, OUT * 60 // 1000


class TwoTurnSTT(StubSTT):
    def __init__(self) -> None:
        self.heard: list[bytes] = []

    async def transcribe(self, pcm: bytes, rate: int) -> str:
        self.heard.append(pcm)
        return "tell me a long story" if len(self.heard) == 1 else "never mind"


class StoryLLM(StubLLM):
    async def stream_sentences(self, messages, fast: bool = False):
        if "never mind" in str(messages[-1]):
            yield "Okay."
            return
        for s in ("Once upon a time there was a pod.", "It lived on a desk.", "It talked a lot."):
            yield s


class ToneTTS(StubTTS):
    """1.5 s per sentence: a 220 Hz tone at about -15 dBFS."""

    async def synth(self, text, mood=None, previous_text=None, next_text=None) -> bytes:
        n = int(OUT * 1.5)
        return array("h", (int(math.sin(2 * math.pi * 220 * i / OUT) * 6000) for i in range(n))).tobytes()


def _speech(n: int, t0: int) -> list[float]:
    """A voice-like signal: 150 Hz harmonics with a 4 Hz syllable envelope, about -16 dBFS."""
    out = []
    for i in range(n):
        t = (i + t0) / IN
        env = 0.6 + 0.4 * abs(math.sin(2 * math.pi * 2 * t))
        out.append(env * sum(math.sin(2 * math.pi * 150 * k * t) / k for k in (1, 2, 3)) * 4500)
    return out


def _pcm(x: list[float]) -> bytes:
    return array("h", (int(max(-32768.0, min(32767.0, v))) for v in x)).tobytes()


def _rms(pcm: bytes) -> float:
    a = array("h", pcm)
    return math.sqrt(sum(v * v for v in a) / len(a))


async def _pod_sim(port: int, echo_gain: float, inject_after_s: float | None) -> dict:
    app = skwoop_bridge.build_app(port)
    app.on_startup.clear()
    app.on_cleanup.clear()
    stt = TwoTurnSTT()
    app["stt"], app["llm"], app["tts"] = stt, StoryLLM(), ToneTTS()
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    _write_set(tmp, SMALL, seconds=0.25)
    app["fillers"], app["filler_timing"] = FillerSet.load(tmp), FillerTiming(first_s=30, gap_min_s=30, gap_max_s=31,
                                                                              long_after_s=60, landing_after_s=60)
    runner, port = await _serve(app)
    enc, dec = opus.Encoder(IN, 1, "voip"), opus.Decoder(OUT, 1)
    played: list[bytes] = []          # the pod's speaker queue (24 kHz frames)
    ev: dict = {"stops": [], "starts": [], "frames": [], "inject": None}
    try:
        async with ClientSession() as http:
            ws = await http.ws_connect(f"ws://127.0.0.1:{port}/xiaozhi/v1/", headers={"Device-Id": "sim-esp32"})
            await ws.send_str(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                                          "features": {"aec": True, "barge_in": "server"},
                                          "audio_params": {"format": "opus", "sample_rate": IN, "channels": 1,
                                                           "frame_duration": 60}}))

            async def recv() -> None:
                async for m in ws:
                    now = time.monotonic()
                    if m.type == WSMsgType.BINARY:
                        played.append(dec.decode(m.data, FRAME_OUT))
                        ev["frames"].append(now)
                    elif m.type == WSMsgType.TEXT:
                        msg = json.loads(m.data)
                        if msg.get("type") == "hello":
                            ev["hello"] = msg
                        if msg.get("type") == "tts" and msg.get("state") == "stop":
                            ev["stops"].append(now)
                            played.clear()                  # xiaozhi drops its queue on tts stop
                        if msg.get("type") == "tts" and msg.get("state") == "sentence_start":
                            ev["starts"].append((now, msg.get("text")))

            rx = asyncio.create_task(recv())
            await ws.send_str(json.dumps({"type": "listen", "state": "start", "mode": "realtime"}))
            t_start, sample = time.monotonic(), 0
            while time.monotonic() - t_start < 9.0:
                now = time.monotonic()
                rng = random.Random(sample)
                mic = [rng.gauss(0.0, 30.0) for _ in range(FRAME_IN)]
                if now - t_start < 0.6:                     # turn 1: "tell me a long story"
                    mic = [m + v for m, v in zip(mic, _speech(FRAME_IN, sample), strict=True)]
                if played:                                  # echo of what the speaker plays now (AEC residual)
                    out = array("h", resample(played.pop(0), FRAME_OUT, FRAME_IN))
                    mic = [m + v * echo_gain for m, v in zip(mic, out, strict=True)]
                first = next((t for t, s in ev["starts"] if s and s.startswith("Once")), None)
                if inject_after_s is not None and first is not None and now - first >= inject_after_s:
                    ev["inject"] = ev["inject"] or now
                    if now - ev["inject"] < 1.0:
                        mic = [m + v for m, v in zip(mic, _speech(FRAME_IN, sample), strict=True)]
                if ws.closed:                               # the follow-up window (SKWOOP_FOLLOWUP_SECS) ran out:
                    break                                   # the bridge sent the pod back to idle
                await ws.send_bytes(enc.encode(_pcm(mic), FRAME_IN))
                sample += FRAME_IN
                await asyncio.sleep(max(0.0, t_start + sample / IN - time.monotonic()))
            await ws.close()
            rx.cancel()
    finally:
        await runner.cleanup()
    ev["heard"] = stt.heard
    return ev


def test_sim_pod_echo_alone_lets_the_whole_reply_play():
    ev = asyncio.run(_pod_sim(18321, echo_gain=0.3, inject_after_s=None))      # echo only ~10 dB under the reply
    assert ev["hello"].get("features") == {"barge_in": True}
    assert [s for _, s in ev["starts"]] == ["Once upon a time there was a pod.", "It lived on a desk.",
                                            "It talked a lot."]
    assert len(ev["heard"]) == 1                     # no second turn: Skwoop's own voice never barged in


def test_sim_pod_voice_over_reply_stops_it_and_keeps_the_first_words():
    ev = asyncio.run(_pod_sim(18323, echo_gain=0.03, inject_after_s=1.0))
    assert ev["inject"] is not None and ev["stops"], ev
    stop = next(t for t in ev["stops"] if t > ev["inject"])
    latency = stop - ev["inject"]
    assert latency < 0.6, latency                    # ~240 ms of voice + one frame + cancel
    late = [t for t in ev["frames"] if t > stop + 0.05]
    assert len([t for t in late if t < stop + 1.0]) == 0     # nothing of the cut reply after the stop
    assert [s for _, s in ev["starts"]].count("It talked a lot.") == 0
    assert len(ev["heard"]) == 2
    pcm = ev["heard"][1]
    k = 2 * FRAME_IN
    loud = sum(_rms(pcm[i:i + k]) > 1000 for i in range(0, len(pcm) // k * k, k))
    assert loud * 0.06 >= 0.9, loud                  # STT got all ~1 s of what Alex said, the onset included
    print(f"barge-in stop latency {latency * 1000:.0f} ms, speech kept {loud * 60} ms")


# ------------------------------------------------------------------ web pod (client-side detection)
def test_sidecar_passes_the_barge_flag_and_declares_client_detection():
    import webpod.server as wp
    cmd = wp.parse_command('{"type": "listen", "state": "start", "mode": "auto", "barge": true}')
    assert wp.to_bridge(cmd) == {"type": "listen", "state": "start", "mode": "auto", "barge": True}
    assert wp.to_bridge(wp.parse_command('{"type": "listen", "state": "start", "mode": "auto"}')) == \
        {"type": "listen", "state": "start", "mode": "auto"}
    assert wp.device_hello()["features"] == {"mcp": True, "barge_in": "client"}


def test_web_pod_barge_cuts_the_reply_and_hears_the_preroll():
    """The page heard Alex over Skwoop: listen start barge + 400 ms preroll + the rest, through the sidecar."""
    import webpod.server as wp
    from test_webpod import _cfg

    async def go():
        port = 18331
        app = skwoop_bridge.build_app(port)
        app.on_startup.clear()
        app.on_cleanup.clear()
        stt = TwoTurnSTT()
        app["stt"], app["llm"], app["tts"] = stt, StoryLLM(), ToneTTS()
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        _write_set(tmp, SMALL, seconds=0.25)
        app["fillers"] = FillerSet.load(tmp)
        app["filler_timing"] = FillerTiming(first_s=30, gap_min_s=30, gap_max_s=31, long_after_s=60, landing_after_s=60)
        bridge, bridge_port = await _serve(app)
        sidecar, side_port = await _serve(wp.make_app(_cfg(bridge_port)))
        said = _pcm(_speech(IN, 0))                                 # 1 s of voice
        events: list[tuple[float, dict]] = []
        try:
            async with ClientSession() as http, http.ws_connect(f"http://127.0.0.1:{side_port}/pod") as ws:
                await ws.send_str(json.dumps({"type": "hello", "device": "barge"}))
                await ws.receive(timeout=5)
                await ws.send_str(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
                await ws.send_bytes(said[: IN])                              # turn 1, 0.5 s
                await ws.send_str(json.dumps({"type": "listen", "state": "stop", "mode": "manual"}))
                t_barge = None
                while True:
                    m = await ws.receive(timeout=10)
                    if m.type != WSMsgType.TEXT:
                        continue
                    obj = json.loads(m.data)
                    events.append((time.monotonic(), obj))
                    if t_barge is None and obj.get("state") == "sentence_start":
                        await asyncio.sleep(0.5)                              # Skwoop is talking
                        t_barge = time.monotonic()
                        await ws.send_str(json.dumps({"type": "listen", "state": "start", "mode": "auto",
                                                      "barge": True}))
                        await ws.send_bytes(said)                             # preroll + the rest, at once
                        await ws.send_bytes(b"\x00\x00" * IN * 2)             # then silence: the VAD ends it
                    if t_barge is not None and obj.get("type") == "stt" and obj.get("text") == "never mind":
                        break
        finally:
            await sidecar.cleanup()
            await bridge.cleanup()
        return t_barge, events, stt

    t_barge, events, stt = asyncio.run(go())
    stop = next(t for t, o in events if t > t_barge and o.get("type") == "tts" and o.get("state") == "stop")
    assert stop - t_barge < 0.3
    assert [o.get("text") for _, o in events if o.get("state") == "sentence_start"] == \
        ["Once upon a time there was a pod."]
    assert _rms(stt.heard[1][: 2 * (IN // 5)]) > 1000        # the first 100 ms of what Alex said made it to STT


def test_js_detector_matches_python():
    """webpod/static/app.js bargePush, run under node, fires on the same frames as barge.py."""
    import re
    import shutil
    import subprocess
    from pathlib import Path

    import pytest
    if not shutil.which("node"):
        pytest.skip("no node")
    src = (Path(barge.__file__).parent / "webpod/static/app.js").read_text()
    consts = re.search(r"  const SILENT_DB = .*?;\n", src).group(0)
    fn = re.search(r"  function bargePush\(micDb, ref, ms\) \{.*?\n  \}\n", src, re.S).group(0)
    rng = random.Random(7)
    cases = {"aec_echo": [(_jit(rng, -50, 3), _jit(rng, -20, 3)) for _ in range(500)],
             "raw_echo": [(_jit(rng, -26, 3), _jit(rng, -20, 3)) for _ in range(500)],
             "voice_over": [(_jit(rng, -45), _jit(rng, -20)) for _ in range(50)] +
                           [(_jit(rng, -18), _jit(rng, -20)) for _ in range(50)],
             "voice_gap": [(_jit(rng, -65), -120.0) for _ in range(30)] + [(_jit(rng, -30), -120.0) for _ in range(30)]}
    js = consts + "const barge = {};\n" + fn + """
const cases = JSON.parse(process.argv[1]); const out = {};
for (const [k, lv] of Object.entries(cases)) { Object.assign(barge, { floor: -60, echo: -20, elapsed: 0, acc: 0 });
  out[k] = null; for (let i = 0; i < lv.length; i++) if (bargePush(lv[i][0], lv[i][1], 20)) { out[k] = i; break; } }
console.log(JSON.stringify(out));"""
    r = subprocess.run(["node", "-e", js, json.dumps(cases)], capture_output=True, text=True, check=True)
    assert json.loads(r.stdout) == {k: _run_levels(v) for k, v in cases.items()}
