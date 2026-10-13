"""Stitched speech (stitch.py, POST /say-template). No keys, no network, no cost.
  python -m pytest -q tests/test_stitch.py
"""
import asyncio
import json
import math
import os
import statistics
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ.setdefault("STATUS_FILE", os.path.join(_tmp, "status.json"))
os.environ.setdefault("INBOX_DIR", os.path.join(_tmp, "inbox"))

import pytest  # noqa: E402
from lean_http import ClientSession, web  # noqa: E402
from lean_ws import WSMsgType  # noqa: E402

import acks  # noqa: E402
import dsp  # noqa: E402
import fake_pod  # noqa: E402
import skwoop_bridge  # noqa: E402
from stubs import StubLLM, StubSTT  # noqa: E402
import acklib  # noqa: E402
import sig  # noqa: E402
import stitch  # noqa: E402
from synth_audio import overlap_root  # noqa: E402
from acks import AckLibrary  # noqa: E402
from providers import TTS_RATE  # noqa: E402
from stitch import StitchLibrary  # noqa: E402

KEY = "test-only-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
ALL4 = ["happy", "idle", "sleep", "alert"]
SAMPLE = {
    "tags": {"idle": ["[warmly]"], "happy": ["[cheerfully]"], "sleep": ["[tired, slow]"], "sad": ["[apologetically]"]},
    "takes": 1,
    "kinds": {
        "c.lead": {"moods": {"idle": ["Okay, setting it to...", "Your timer for...", "Sorry, I can't reach..."],
                             "happy": ["Okay, setting it to..."], "sleep": ["Okay, setting it to..."],
                             "sad": ["Sorry, I can't reach..."]}},
        "pct.final": {"moods": {"idle": ["fifty percent."], "happy": ["fifty percent."]}},
        "dur.mid": {"moods": {"idle": ["twenty minutes...", "five minutes..."]}},
        "dur.plus": {"moods": {"idle": ["one hour and..."]}},
        "c.end": {"moods": {"idle": ["starts now!"]}},
        "device.final": {"default": "sad", "moods": {"sad": ["the lights."]}},
        "line.joke": {"moods": {"idle": ["Joke one!", "Joke two!"]}},
    },
    "vocab": {"carrier": {"setting_to": {"lead": "Okay, setting it to..."}, "timer_for": {"lead": "Your timer for..."},
                          "starts_now": {"end": "starts now!"},
                          "sorry_cant_reach": {"lead": "Sorry, I can't reach..."}},
              "device": {"lights": {"say": "the lights", "plural": True}}},
    "templates": {
        "light_set": {"parts": ["lead:setting_to", "pct:final"], "moods": ALL4, "default": "idle"},
        "timer_set": {"parts": ["lead:timer_for", "dur:mid", "end:starts_now"], "moods": ALL4, "default": "idle"},
        "joke": {"parts": ["line:joke"], "moods": ["happy", "idle"], "default": "idle"},
        "err_device": {"parts": ["lead:sorry_cant_reach", "device:final"], "moods": ["sad"], "default": "sad"},
    },
}


@pytest.fixture
def sample(tmp_path):
    path, out = tmp_path / "stitch.json", tmp_path / "clips"
    path.write_text(json.dumps(SAMPLE))
    acklib.render(path, out)
    return path, out


# ------------------------------------------------------------------ words
def test_words():
    assert [stitch.num_words(n) for n in (0, 7, 21, 100, 105, 300, 2000, 1500)] == [
        "zero", "seven", "twenty-one", "one hundred", "one hundred five", "three hundred", "two thousand",
        "one thousand five hundred"]
    assert [stitch.ordinal_words(n) for n in (1, 2, 3, 12, 20, 21, 30, 31)] == [
        "first", "second", "third", "twelfth", "twentieth", "twenty-first", "thirtieth", "thirty-first"]
    assert stitch.pct_words(49.6) == "fifty percent" and stitch.temp_words(-1) == "minus one degree"
    assert stitch.dur_words(45) == ["forty-five seconds"] and stitch.dur_words(60) == ["one minute"]
    assert stitch.dur_words(1800) == ["half an hour"] and stitch.dur_words(4800) == ["one hour", "twenty minutes"]
    assert stitch.dur_words(3725) == ["one hour, two minutes and five seconds"]       # no pieces: read live
    assert stitch.time_words("15:45") == ("three forty-five", "in the afternoon")
    assert stitch.time_words("7:05 am") == ("seven oh five", "in the morning")
    assert stitch.time_words("12:00") == ("noon", "") and stitch.time_words("0:00") == ("midnight", "")
    assert stitch.time_words("22:00") == ("ten o'clock", "at night")
    assert stitch.date_words("2026-10-12") == ("October", "twelfth")
    with pytest.raises(ValueError):
        stitch.time_words("25:00")


# ------------------------------------------------------------------ the real piece library
# ------------------------------------------------------------------ say: moods, fallback, joins
def test_say_stitches_one_mood_and_falls_back(sample):
    st = StitchLibrary(*sample)
    r = st.say("light_set", {"pct": 50}, "happy")
    assert (r["source"], r["mood"], r["text"], r["pieces"]) == ("stitch", "happy",
                                                                "Okay, setting it to... fifty percent.", 2)
    assert len(r["pcm"]) > 0
    r = st.say("light_set", {"pct": 50}, "sleep")         # no sleepy "fifty percent." -> the whole line idle
    assert (r["source"], r["mood"], r["requested"]) == ("stitch", "idle", "sleep")
    r = st.say("light_set", {"pct": 60})                  # missing piece -> live TTS, same words
    assert r["source"] == "tts" and r["pcm"] is None and r["text"] == "Okay, setting it to... sixty percent."
    assert r["missing"] == ["pct.final/idle: sixty percent."]
    r = st.say("timer_set", {"dur": 4800})
    assert r["text"] == "Your timer for... one hour and... twenty minutes... starts now!" and r["pieces"] == 4
    assert st.say("joke")["text"] in ("Joke one!", "Joke two!")
    r = st.say("err_device", {"device": "lights"})
    assert (r["mood"], r["emotion"], r["text"]) == ("sad", "sad", "Sorry, I can't reach... the lights.")
    man = json.loads((sample[1] / "manifest.json").read_text())        # a take the STT check marked bad
    man["clips"][acks.clip_key("pct.final", "happy", "fifty percent.", 0)]["bad"] = "STT twice: 'fifty, fifty'"
    (sample[1] / "manifest.json").write_text(json.dumps(man))
    st2 = StitchLibrary(*sample)
    r = st2.say("light_set", {"pct": 50}, "happy")        # never played: the whole sentence goes idle
    assert (r["source"], r["mood"]) == ("stitch", "idle")
    with pytest.raises(KeyError):
        st.say("warp_drive")
    with pytest.raises(ValueError):
        st.say("light_set", {})


_wav = sig.write_wav
_tone = sig.tone
V3 = stitch.JOIN_PROFILES["v3"]


def test_join_trims_silence_and_pauses_by_punctuation(tmp_path):
    n = TTS_RATE // 2
    pad = [0.0] * (TTS_RATE // 4)
    a, b, c = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "c.wav"
    for p in (a, b, c):
        _wav(p, pad + [0.001 * v for v in sig.noise(len(pad), 1.0, 1)] + _tone() + pad)
    keep, xf = int(stitch.KEEP_S * TTS_RATE), int(stitch.XFADE_S * TTS_RATE)
    piece = n + 2 * keep                                     # breath-level noise (-60 dB) is trimmed too
    pcm = stitch.join([("Okay, setting it to,", a, {}), ("fifty percent.", b, {}), ("Oh, and.", c, {})], profile="v3")
    x = sig.floats(pcm)
    expect = 3 * piece + int(V3["pause_comma"] * TTS_RATE) + int(V3["pause_sent"] * TTS_RATE) - 2 * xf
    assert abs(len(x) - expect) < 0.012 * TTS_RATE
    assert abs(x[0]) < 1e-3 and abs(x[-1]) < 1e-3          # faded edges, no clicks
    run = stitch.join([("one", a, {}), ("two", b, {})], profile="v3")    # no punctuation: runs on, crossfaded
    assert abs(len(run) // 2 - (2 * piece - xf)) < 0.012 * TTS_RATE
    assert [stitch.pause_after(t, "v3") for t in ("It's...", "Okay,", "done.", "done!", "done?", "and")] == [
        V3["pause_run"], V3["pause_comma"], V3["pause_sent"], V3["pause_sent"], V3["pause_sent"], 0.0]
    assert 0.02 <= stitch.XFADE_S <= 0.04


def test_join_measured_gaps_match_the_pauses(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _wav(a, [0.0] * 2400 + _tone() + [0.0] * 2400)
    _wav(b, [0.0] * 2400 + _tone(330) + [0.0] * 2400)
    for text, pause in (("Okay,", V3["pause_comma"]), ("Done.", V3["pause_sent"])):
        x = sig.floats(stitch.join([(text, a, {}), ("next.", b, {})], profile="v3"))
        n = int(0.005 * TTS_RATE)                                  # 5 ms frames, 40 dB under the tone = silence
        db = sig.frame_db(x, n)
        top = max(db)
        loud = [i for i, d in enumerate(db) if d >= top - 40]
        gap = sum(d < top - 40 for d in db[loud[0]:loud[-1]]) * n
        assert abs(gap / TTS_RATE - pause) < 0.025, (text, gap / TTS_RATE)


def test_equal_power_crossfade_keeps_the_level(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _wav(a, sig.noise(TTS_RATE // 2, 0.1, 3))
    _wav(b, sig.noise(TTS_RATE // 2, 0.1, 4))
    x = sig.floats(stitch.join([("one", a, {}), ("two", b, {})]))
    xf = int(stitch.XFADE_S * TTS_RATE)
    mid = TTS_RATE // 2 - xf                                  # the overlap
    over, body = sig.rms(x[mid: mid + xf]), sig.rms(x[mid - 4 * xf: mid - xf])
    assert abs(20 * math.log10(over / body)) < 1.5            # no dip (linear fades dip ~3 dB on uncorrelated audio)


def test_join_matches_loudness_to_the_carrier_and_at_each_join(tmp_path):
    a, b, c = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "c.wav"
    _wav(a, _tone(amp=0.2))
    _wav(b, _tone(amp=0.2 * 10 ** (-8 / 20)))               # 8 dB quieter: brought up by the max, 4 dB
    _wav(c, _tone(amp=0.2 * 10 ** (-2 / 20)))               # 2 dB: matched
    rep = []
    stitch.join([("one,", a, {}), ("two,", b, {}), ("three.", c, {})], report=rep, profile="v3")
    assert rep[0]["level_gain_db"] == stitch.LEVEL_MAX_DB and abs(rep[1]["level_gain_db"] - 2) < 0.2
    assert all(V3["onset_min_db"] <= r["edge_gain_db"] <= stitch.EDGE_MAX_DB for r in rep)
    rep = []
    stitch.join([("x,", b, {}), ("y.", a, {})], ref=1, report=rep, profile="v3")   # the carrier is the 2nd piece
    assert "level_gain_db" not in rep[0]                       # the carrier keeps its level; the others move
    x = dsp.floats(sig.floats(stitch.join([("one,", b, {}), ("two.", c, {})], ref=0, profile="v3")))
    pieces_db = statistics.median([stitch.active_db(stitch._load(p)) for p in (b, c)])
    assert abs(stitch.active_db(x) - pieces_db) < 0.5          # matched to a quiet carrier, but not quieter overall
    # manifest trim still applies: +3 dB
    loud = stitch.join([("x.", a, {"target": -20, "lufs": -23})], profile="v3")
    quiet = stitch.join([("x.", a, {"target": -20, "lufs": -20})], profile="v3")
    ratio = max(map(abs, sig.floats(loud))) / max(map(abs, sig.floats(quiet)))
    assert abs(20 * math.log10(ratio) - 3) < 0.1


def _trailing(body_s=0.35, decay_s=0.5, f=220.0):
    """A '...' piece: a steady syllable, then a long fading drawl (-3 dB -> -35 dB)."""
    tail = [0.2 * 10 ** ((-3 - 32 * (i / TTS_RATE) / decay_s) / 20)
            * math.sin(2 * math.pi * f * (i / TTS_RATE + body_s))
            for i in range(int(decay_s * TTS_RATE))]
    return _tone(f, 0.2, body_s) + tail


def test_v3_cuts_the_trailing_drawl_and_runs_on(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _wav(a, _trailing())
    _wav(b, _tone(330.0, 0.2, 0.5))
    x = sig.floats(stitch.join([("It's...", a, {}), ("three.", b, {})], profile="v3"))
    assert len(x) < (0.35 + 0.5 + 0.5 - 0.25) * TTS_RATE            # most of the 0.5 s drawl is gone
    n = int(0.005 * TTS_RATE)
    db = sig.frame_db(x, n)
    top = max(db)
    loud = [i for i, d in enumerate(db) if d > top - 40]
    assert sum(d < top - 40 for d in db[loud[0]:loud[-1]]) * n / TTS_RATE <= 0.03   # mid-sentence gap: 0-30 ms
    rep = []
    stitch.join([("It's...", a, {}), ("three.", b, {})], profile="v3", report=rep)
    assert rep[0]["pause_s"] <= 0.03 and rep[0]["edge_gain_db"] >= V3["onset_min_db"]
    last = stitch.join([("three.", b, {}), ("It's...", a, {})], profile="v3")
    assert len(last) / 2 / TTS_RATE > 0.5 + 0.35 + 0.3               # the last piece keeps its tail


def _f0(x):
    """Pitch of a clean tone from its rising zero crossings."""
    z = [i for i in range(1, len(x)) if x[i - 1] <= 0 < x[i]]
    return (len(z) - 1) * TTS_RATE / (z[-1] - z[0])


def test_wsola_speeds_up_without_changing_pitch():
    y = stitch.wsola(dsp.floats(_tone(220.0, 0.2, 0.5)), 1.45)
    assert abs(len(y) / TTS_RATE - 0.5 / 1.45) < 0.02
    assert abs(_f0(y) - 220) < 4
    assert list(stitch.wsola(dsp.floats(_tone()), 1.0)) == pytest.approx(_tone())


def test_join_profile_flag():
    assert stitch.JOIN in stitch.JOIN_PROFILES and set(stitch.JOIN_PROFILES) == {"v3", "overlap"}
    assert stitch.pause_after("It's...", "v3") <= 0.03
    assert stitch.pause_after("Okay,", "v3") <= 0.08 and stitch.pause_after("Done.", "v3") <= 0.12


# ------------------------------------------------------------------ takes, bad clips, whole lines, retired moods
TAKES = {
    "tags": {"idle": ["[warmly]", "[calmly]"], "happy": ["[cheerfully]", "[brightly]"], "sleep": ["[tired, slow]"]},
    "takes": 2,
    "kinds": {
        "c.lead": {"moods": {"idle": ["Okay, setting it to..."], "happy": ["Okay, setting it to..."],
                             "sleep": {"takes": 1, "lines": ["Okay, setting it to..."]}}},
        "pct.final": {"moods": {"idle": ["fifty percent."], "happy": ["fifty percent."],
                                "sleep": {"takes": 1, "lines": ["fifty percent."]}}},
        "line.status": {"moods": {"idle": ["Okay, setting it to fifty percent!"]}},
    },
    "vocab": {"carrier": {"setting_to": {"lead": "Okay, setting it to..."}}},
    "templates": {"light_set": {"parts": ["lead:setting_to", "pct:final"], "moods": ALL4, "default": "idle"}},
}


@pytest.fixture
def takes_lib(tmp_path):
    path, out = tmp_path / "stitch.json", tmp_path / "clips"
    path.write_text(json.dumps(TAKES))
    acklib.render(path, out)
    man = json.loads((out / "manifest.json").read_text())

    def put(kind, mood, text, take, f, energy):                # give each take a known pitch and energy
        e = man["clips"][acks.clip_key(kind, mood, text, take)]
        _wav(out / e["file"], _tone(f, 0.2, 0.6))
        e["level"] = {"lufs": -20.0, "target": -20.0, "lufs_in": energy}
    for mood in ("idle", "happy"):
        put("c.lead", mood, "Okay, setting it to...", 0, 220.0, -24.0)
        put("c.lead", mood, "Okay, setting it to...", 1, 220.0, -24.0)
        put("pct.final", mood, "fifty percent.", 0, 300.0, -16.0)          # far: higher, louder
        put("pct.final", mood, "fifty percent.", 1, 226.0, -23.0)          # close to the carrier
    (out / "manifest.json").write_text(json.dumps(man))
    return path, out, man


def _takes_of(r):
    return [f.rsplit(".", 2)[-2] for f in r["takes"]]


def test_take_selection_fits_the_carrier(takes_lib):
    path, out, man = takes_lib
    st = StitchLibrary(path, out)
    picks = {st.say("light_set", {"pct": 50}, "happy")["takes"][1] for _ in range(12)}
    assert picks == {man["clips"][acks.clip_key("pct.final", "happy", "fifty percent.", 1)]["file"]}


def test_bad_takes_are_never_used(takes_lib):
    path, out, man = takes_lib
    man["clips"][acks.clip_key("pct.final", "happy", "fifty percent.", 1)]["bad"] = "STT twice"
    (out / "manifest.json").write_text(json.dumps(man))
    st = StitchLibrary(path, out)
    bad = man["clips"][acks.clip_key("pct.final", "happy", "fifty percent.", 1)]["file"]
    for _ in range(12):
        r = st.say("light_set", {"pct": 50}, "happy")
        assert r["source"] == "stitch" and bad not in r["takes"]


def test_a_whole_recorded_line_beats_stitching(takes_lib, tmp_path):
    path, out, man = takes_lib
    st = StitchLibrary(path, out)
    r = st.say("light_set", {"pct": 50}, "idle")             # line.status says the same words
    assert (r["source"], r["text"], r["pieces"], r["mood"]) == ("line", "Okay, setting it to fifty percent!", 1, "idle")
    r = st.say("light_set", {"pct": 50}, "happy")            # no happy whole line: a happy stitch beats an idle line
    assert (r["source"], r["mood"]) == ("stitch", "happy")
    ack = {"takes": 1, "tags": {"happy": ["[cheerfully]"]},
           "kinds": {"light_set": {"default": "happy", "moods": {"happy": ["Okay, setting it to fifty percent."]}}}}
    apath, aout = tmp_path / "acks.json", tmp_path / "ackclips"
    apath.write_text(json.dumps(ack))
    acklib.render(apath, aout)
    st2 = StitchLibrary(path, out, whole_lines=[AckLibrary(apath, aout)])
    r = st2.say("light_set", {"pct": 50}, "happy")           # ... and an ack with the same words beats it too
    clip = f"light_set.happy.{acks.text_id(ack['kinds']['light_set']['moods']['happy'][0])}.0.wav"
    assert (r["source"], r["mood"], r["file"]) == ("line", "happy", clip)


def test_sleep_is_never_stitched_and_maps_to_calm_idle(takes_lib):
    path, out, man = takes_lib
    for k in list(man["clips"]):                              # no whole line in the way
        if k.startswith("line."):
            del man["clips"][k]
    (out / "manifest.json").write_text(json.dumps(man))
    st = StitchLibrary(path, out)
    sleepy = {e["file"] for k, e in man["clips"].items() if "/sleep/" in k}
    assert sleepy and all((out / f).is_file() for f in sleepy)     # the sleepy files stay on disk
    for _ in range(10):
        r = st.say("light_set", {"pct": 50}, "sleep")
        assert (r["source"], r["mood"], r["requested"], r["emotion"]) == ("stitch", "idle", "sleep", "neutral")
        assert not sleepy & set(r["takes"])
        car = next(e for e in man["clips"].values() if e["file"] == r["takes"][0])
        assert car["direction"] == "[calmly]"                       # calm takes first for a retired mood
    assert st.describe()["retired_moods"] == {"sleep": "idle"}


# ------------------------------------------------------------------ POST /say-template
class StubTTS:
    provider = "stub"

    def __init__(self):
        self.said = []

    library = None        # the providers.TTS / house.AckSpeaker surface: no SkwoopOS clips here
    last_backend = ""

    async def synth(self, text, mood=None, previous_text=None, next_text=None):
        self.said.append(text)
        return b"\x20\x00" * (TTS_RATE // 2)


async def _until_stop(ws):
    frames = []
    while True:
        msg = await asyncio.wait_for(ws.receive(), 10)
        if msg.type == WSMsgType.BINARY:
            if "audio" not in frames:
                frames.append("audio")
            continue
        m = json.loads(msg.data)
        frames.append((m["type"], m.get("state") or m.get("emotion"), m.get("text")))
        if m["type"] == "tts" and m.get("state") == "stop":
            return frames


def test_say_template_endpoint(sample):
    async def go():
        port = 18141
        app = skwoop_bridge.build_app(port)
        app.on_startup.clear()
        app.on_cleanup.clear()
        app["stt"] = StubSTT()
        app["llm"] = StubLLM()
        app["tts"] = StubTTS()
        app["acks"] = AckLibrary(*sample)
        app["stitch"] = StitchLibrary(*sample)
        app["store"].write_key = KEY
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        server, out = f"http://127.0.0.1:{port}", {}
        try:
            async with ClientSession() as http:
                async def post(body, headers=AUTH):
                    async with http.post(f"{server}/say-template", json=body, headers=headers) as r:
                        return r.status, await r.json()

                out["noauth"] = await post({"template": "light_set", "values": {"pct": 50}}, headers={})
                out["no_pod"] = await post({"template": "light_set", "values": {"pct": 50}})
                out["dry"] = await post({"template": "light_set", "values": {"pct": 50}, "dry_run": True})
                out["unknown"] = await post({"template": "warp_drive", "dry_run": True})
                out["badval"] = await post({"template": "light_set", "values": {"pct": "lots"}, "dry_run": True})
                out["badmood"] = await post({"template": "light_set", "values": {"pct": 5}, "mood": "grumpy",
                                             "dry_run": True})
                ws, _, _ = await fake_pod.connect(http, server)
                out["play"] = await post({"template": "light_set", "values": {"pct": 50}, "mood": "happy"})
                out["play_frames"] = await _until_stop(ws)
                out["live"] = await post({"template": "light_set", "values": {"pct": 60}})
                out["live_frames"] = await _until_stop(ws)
                await ws.close()
                async with http.get(f"{server}/say-template") as r:
                    out["list"] = await r.json()
                async with http.get(f"{server}/healthz") as r:
                    out["hz"] = await r.json()
            out["said"] = app["tts"].said
        finally:
            await runner.cleanup()
        return out

    o = asyncio.run(go())
    assert o["noauth"][0] == 401 and o["no_pod"][0] == 409
    s, r = o["dry"]
    assert s == 200 and r["text"] == "Okay, setting it to... fifty percent." and r["source"] == "stitch"
    assert r["audio_s"] > 0 and "room" not in r
    assert o["unknown"][0] == 404 and "light_set" in o["unknown"][1]["templates"]
    assert o["badval"][0] == 400 and o["badmood"][0] == 400
    s, r = o["play"]
    assert s == 200 and (r["mood"], r["source"]) == ("happy", "stitch")
    assert o["play_frames"] == [("tts", "start", None), ("llm", "happy", ""),
                                ("tts", "sentence_start", "Okay, setting it to... fifty percent."), "audio",
                                ("tts", "stop", None)]
    assert o["live"][1]["source"] == "tts" and o["said"] == ["Okay, setting it to... sixty percent."]
    assert o["live_frames"][2] == ("tts", "sentence_start", "Okay, setting it to... sixty percent.")
    assert o["list"]["templates"] == 4 and o["list"]["templates_list"]["light_set"]["parts"][1] == "pct:final"
    assert o["hz"]["stitch_templates"] == 4 and o["hz"]["stitch_clips"] > 0


# ------------------------------------------------------------------ overlap join (STITCH_JOIN=overlap)
# fixtures/overlap-pair: A = "hiss ahh" (noise then a voiced vowel), B = "ahh buzz" (the same vowel 3 dB quieter,
# then a tone), each with the render-time sidecar ("<clip>.align.json": alignment and v1 edge analysis).
OV = str(overlap_root())


def _ov_pieces():
    out = []
    for name in ("a", "b"):
        path = os.path.join(OV, f"{name}.wav")
        sc = stitch.overlap_sidecar(path)
        out.append({"x": stitch._load(path), "ana": sc["analysis"], "ol": sc.get("ol"), "or": sc.get("or")})
    return out


def test_dtw_basics():
    m = _ov_pieces()[0]["ana"]["or"]["mfcc"]
    assert stitch.dtw_path(m, m) == [(i, i) for i in range(len(m))]
    assert stitch.dtw_path([[0.0], [1.0], [2.0]], [[0.0], [2.0]]) == [(0, 0), (1, 0), (2, 1)]
    assert stitch.dtw_path([], [[1.0]]) == []


def test_overlap_join_cuts_inside_the_shared_word_seamlessly():
    pieces = _ov_pieces()
    b = pieces[1]["x"]
    rep = []
    y = stitch.overlap_join(pieces, report=rep)
    (j,) = rep
    assert j["word"] == "ahh" and j["cut"] == "voiced" and j["xcorr"] > 0.95
    cut = j["cut_a"]
    assert 0.3 * TTS_RATE < cut < 0.7 * TTS_RATE                  # inside the vowel, not at an edge
    seg = y[cut - 480:cut + 480]                                  # one continuous vowel: no gap, no click
    assert sig.rms(seg) > 0.05
    ref = y[int(0.35 * TTS_RATE):cut + 960]
    steps = max(abs(q - p) for p, q in zip(ref, ref[1:], strict=False))
    assert max(abs(q - p) for p, q in zip(seg, seg[1:], strict=False)) <= 1.2 * steps
    assert abs(j["gain_db"] - 20 * math.log10(1 / 0.7)) < 1.0    # the quieter B side is lifted to A's level
    assert len(y) == j["cut_a"] + len(b) - j["cut_b"]             # nothing of B before its cut, all of it after
    with pytest.raises(ValueError):
        stitch.overlap_join([pieces[0], dict(pieces[1], ana=None)])


def test_overlap_profile_needs_sidecars_and_falls_back_to_v3(tmp_path):
    clips = []
    for name, text in (("a", "hiss..."), ("b", "buzz.")):
        (tmp_path / f"{name}.wav").write_bytes(open(os.path.join(OV, f"{name}.wav"), "rb").read())
        clips.append((text, str(tmp_path / f"{name}.wav"), {}))
    rep = []
    stitch.join(clips, report=rep, profile="overlap")
    assert rep[0]["fallback"] == "v3" and "at_s" in rep[1]        # no sidecars: the v3 join
    for name in ("a", "b"):
        (tmp_path / f"{name}.align.json").write_text(open(os.path.join(OV, f"{name}.align.json")).read())
    rep = []
    y = stitch.join(clips, report=rep, profile="overlap")
    assert len(rep) == 1 and rep[0]["word"] == "ahh" and "fallback" not in rep[0]
    full = sum(os.path.getsize(c[1]) - 44 for c in clips) // 2
    assert len(y) // 2 < full - 0.25 * TTS_RATE                   # the shared vowel is said once, not twice
    sc = json.loads((tmp_path / "b.align.json").read_text())
    sc["ol"] = "oh"
    (tmp_path / "b.align.json").write_text(json.dumps(sc))
    rep = []
    stitch.join(clips, report=rep, profile="overlap")
    assert rep[0]["fallback"] == "v3"                             # the shared words must match


def test_overlap_join_is_fast_enough_for_live_play():
    pieces = _ov_pieces()
    stitch.overlap_join(pieces)
    t = time.perf_counter()
    for _ in range(5):
        stitch.overlap_join(pieces)
    assert (time.perf_counter() - t) / 5 < 0.05                   # well under 50 ms a join (measured ~2-5 ms)


def test_overlap_profile_is_the_default():
    # overlap is the default join; pieces without sidecars still join as v3
    assert stitch.JOIN_PROFILES["overlap"]["fallback"] == "v3"
    assert os.environ.get("STITCH_JOIN") or stitch.JOIN == "overlap"
