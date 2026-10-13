"""Ack clips (one bucket of lines per kind x mood, several takes per line): render script, picking, bucket
fallback, POST /ack to a fake pod, /healthz.
No keys, no network, no cost: clips come from tests/acklib.py (fake chirps).
  python -m pytest -q tests/test_acks.py
"""
import asyncio
import json
import os
import re
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("STATUS_FILE", os.path.join(_tmp, "status.json"))
os.environ.setdefault("INBOX_DIR", os.path.join(_tmp, "inbox"))

import opus  # noqa: E402
import pytest  # noqa: E402
from lean_http import ClientSession, web  # noqa: E402
from lean_ws import WSMsgType  # noqa: E402

import acks  # noqa: E402
import fake_pod  # noqa: E402
import skwoop_bridge  # noqa: E402
from stubs import StubLLM, StubSTT  # noqa: E402
import acklib  # noqa: E402
from acks import AckLibrary  # noqa: E402
from providers import TTS_RATE  # noqa: E402

LIB = {"takes": 2,
       "tags": {"happy": ["[brightly]", "[cheerfully]"], "idle": ["[warmly]", "[softly]"],
                "sleep": ["[sleepily]", "[tired, slow]"], "sad": ["[apologetically]", "[remorsefully]"]},
       "kinds": {
           "light_up": {"default": "happy", "moods": {
               "happy": ["Okay, I turned it up!", "Brighter now!", "There, a little brighter."],
               "idle": ["Turned up.", "A bit brighter."],
               "sleep": {"takes": 1, "lines": ["Okay... brighter!", "Sleepy, but turned up."]}}},
           "solo": {"default": "idle", "moods": {"idle": ["Only line."], "happy": ["Only happy line!"],
                                                 "sleep": ["Okay... only line."]}},
           "cant_do": {"default": "sad", "moods": {
               "sad": ["Sorry, I couldn't.", "Oops, sorry."],
               "idle": {"tags": ["[gently]"], "lines": ["That didn't work."]}}},   # one tag -> one take
           "unrendered": {"default": "idle", "moods": {"idle": {"takes": 1, "lines": ["Not rendered yet."]}}}}}
REC = 3 * 2 + 2 * 2 + 2 * 1 + 3 * 2 + 2 * 2 + 1 + 1   # 24 recordings
BUCKETS, LINES = 9, 14
KEY = "test-only-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def fname(kind, mood, text, take=0):
    return f"{kind}.{mood}.{acks.text_id(text)}.{take}.wav"


@pytest.fixture
def lib(tmp_path):
    """A small library rendered with the fake provider; 'unrendered' has its clip removed."""
    path, out = tmp_path / "acks.json", tmp_path / "clips"
    path.write_text(json.dumps(LIB))
    assert acklib.render(path, out) == REC
    os.remove(out / fname("unrendered", "idle", "Not rendered yet."))
    return path, out


# ------------------------------------------------------------------ library
def test_takes_get_different_tags_and_stay_put(lib):
    m = json.loads((lib[1] / "manifest.json").read_text())["clips"]
    for text in LIB["kinds"]["light_up"]["moods"]["happy"]:
        k = f"light_up/happy/{acks.text_id(text)}"
        assert {m[f"{k}/0"]["direction"], m[f"{k}/1"]["direction"]} == {"[brightly]", "[cheerfully]"}
    tags = ["[a]", "[b]", "[c]"]
    assert acks.take_tag("Hi!", 0, tags) == acks.take_tag("Hi!", 0, tags)          # stable per text
    assert len({acks.take_tag("Hi!", n, tags) for n in range(3)}) == 3
    spread = {acks.take_tag(f"line {i}", 0, tags) for i in range(30)}             # first takes use every tag
    assert spread == set(tags)


def test_library_rejects_bad_buckets(tmp_path):
    bad = [{"kinds": {"k": {"default": "idle", "moods": {"idle": ["Same.", "Same."]}}}},     # duplicate line
           {"kinds": {"k": {"default": "happy", "moods": {"idle": ["Hi."]}}}},             # default has no bucket
           {"kinds": {"k": {"default": "idle", "moods": {"idle": ["Hi."], "grumpy": ["Hmph."]}}}},
           {"kinds": {"k": {"default": "idle", "moods": {"idle": []}}}}]                   # empty bucket
    for n, b in enumerate(bad):
        (tmp_path / f"{n}.json").write_text(json.dumps(b))
        with pytest.raises(ValueError):
            acks.load_library(tmp_path / f"{n}.json")


def test_real_library_loads():
    kinds = acks.load_library(acks.DEFAULT_LIBRARY)
    buckets = [(k, m, b) for k, s in kinds.items() for m, b in s["moods"].items()]
    assert len(kinds) == 20 and len(buckets) >= 60
    for k, m, b in buckets:
        assert m in acks.MOODS and 6 <= len(b["lines"]) <= 10, (k, m)
        assert 1 <= b["takes"] <= len(b["tags"]) and len(set(b["tags"])) == len(b["tags"]), (k, m)
        assert all(t.startswith("[") and t.endswith("]") for t in b["tags"]), (k, m)
        assert all("{" not in ln and len(ln) <= 40 for ln in b["lines"]), (k, m)    # short, no name slots
        if m == "alert":
            assert all(len(ln) <= 28 for ln in b["lines"]), (k, m)               # alert lines are brisk
    for k, s in kinds.items():
        assert {"idle", "happy"} & set(s["moods"]), k        # the fallback buckets exist for every kind
    every = [ln for _, _, b in buckets for ln in b["lines"]]
    assert len(every) == len(set((k, m, ln) for k, m, b in buckets for ln in b["lines"]))


# ------------------------------------------------------------------ render script
def test_clip_format_is_24k_16bit_mono(lib):
    import wave
    with wave.open(str(lib[1] / fname("light_up", "sleep", "Sleepy, but turned up."))) as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (TTS_RATE, 2, 1)


def test_default_voice_is_skwoop_try8(monkeypatch):
    import providers
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    assert providers.voice_settings()["eleven_voice_id"] == ""
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "")
    assert providers.voice_key("elevenlabs").startswith("elevenlabs::")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-from-env")
    assert providers.voice_settings()["eleven_voice_id"] == "voice-from-env"
    assert providers.voice_key("elevenlabs").startswith("elevenlabs:voice-from-env:")


SOUNDS_MOANY = re.compile(r"\b(m+h?m+|mmkay|o+h+|a+h+|hmm+|shh+|whisper\w*|breath\w*|hush\w*|moan\w*|sultry|sigh\w*|"
                          r"plug me)\b", re.I)
MOANY_TAGS = re.compile(r"soft|hush|breath|whisper|drows|half asleep|sigh|exhale|moan|sultry|murmur|sensual", re.I)


def test_sleepy_lines_are_cute_tired_not_moany():
    """Alex, Oct 1: the old sleep lines ("Mm, okay...", "Mmhm...", [softly, half asleep]) sounded moaning."""
    kinds = acks.load_library(acks.DEFAULT_LIBRARY)
    checked = [(k, m, b) for k, s in kinds.items() for m, b in s["moods"].items() if m == "sleep" or k == "low_battery"]
    assert len(checked) == 18 + 2                                         # 18 sleep buckets + low_battery idle/alert
    for k, m, b in checked:
        assert len(b["lines"]) >= 10, (k, m)
        assert not [ln for ln in b["lines"] if SOUNDS_MOANY.search(ln)], (k, m)
        assert not [t for t in b["tags"] if MOANY_TAGS.search(t)], (k, m)
        if m == "sleep":
            assert "[yawns]" in b["tags"], k                              # the yawn is a tag, never spoken text
            assert not [ln for ln in b["lines"] if "*" in ln or "yawn" in ln.lower().replace("yawny", "")], k


# ------------------------------------------------------------------ picking
def test_random_pick_never_repeats_and_uses_every_take(lib):
    a = AckLibrary(*lib)
    picks = [a.pick("light_up") for _ in range(300)]
    seq = [p["text"] for p in picks]
    assert all(x != y for x, y in zip(seq, seq[1:], strict=False)) and set(seq) == set(
        LIB["kinds"]["light_up"]["moods"]["happy"])
    assert len({p["file"] for p in picks}) == 6 and {p["mood"] for p in picks} == {"happy"}
    assert {a.pick("solo")["text"] for _ in range(5)} == {"Only line."}     # one line: repeats are fine
    a.pick("light_up", "idle")
    assert a.pick("light_up")["text"] != seq[-1]                         # "last" is kept per bucket


def test_mood_resolution(lib):
    a = AckLibrary(*lib)
    assert a.resolve_mood("light_up") == "happy"                         # kind default
    assert a.resolve_mood("light_up", "sleep") == "idle"                 # explicit, but sleep is retired -> idle
    assert a.resolve_mood("light_up", "happy") == "happy"                # explicit
    assert a.resolve_mood("light_up", pod_mood="sleep") == "idle"        # a sleepy pod gets the calm idle voice
    assert a.resolve_mood("light_up", "neutral") == "idle"               # tone classifier name
    assert a.resolve_mood("light_up", pod_mood="neutral") == "idle"      # pod mood the kind has
    assert a.resolve_mood("light_up", pod_mood="sad") == "happy"         # pod mood it hasn't -> default
    assert a.resolve_mood("cant_do", pod_mood="sad") == "sad"
    with pytest.raises(ValueError):
        a.resolve_mood("light_up", "grumpy")
    with pytest.raises(KeyError):
        a.resolve_mood("no_such_kind")


def test_bucket_fallback_idle_then_happy_then_tts(lib):
    path, out = lib
    a = AckLibrary(path, out)
    p = a.pick("solo", "idle")
    assert (p["mood"], p["emotion"], p["source"], p["text"]) == ("idle", "neutral", "clip", "Only line.")
    for f in out.glob("solo.idle.*"):
        f.unlink()
    p = a.pick("solo", "idle")
    assert (p["mood"], p["requested"], p["emotion"], p["source"], p["text"]) == \
        ("happy", "idle", "happy", "clip", "Only happy line!")
    for f in out.glob("solo.happy.*"):
        f.unlink()
    p = a.pick("solo", "idle")                                           # nothing rendered: live TTS, own words
    assert (p["source"], p["pcm"], p["text"], p["mood"], p["emotion"]) == \
        ("tts", None, "Only line.", "idle", "neutral")
    p = a.pick("light_up", "sad")                                        # no sad bucket -> its idle bucket
    assert (p["mood"], p["source"]) == ("idle", "clip") and p["text"] in ("Turned up.", "A bit brighter.")
    p = a.pick("cant_do", "alert")                                       # no alert, idle, rendered
    assert (p["mood"], p["text"]) == ("idle", "That didn't work.")
    for take in (0, 1):                                                  # half a bucket: play the rest
        (out / fname("light_up", "idle", "A bit brighter.", take)).unlink()
    assert {a.pick("light_up", "idle")["text"] for _ in range(6)} == {"Turned up."}


def test_sleep_is_retired_but_its_clips_stay(lib, monkeypatch):
    """Alex, Oct 1: the sleepy voice is breathy and has no real use. Never spoken; files kept; calm idle instead."""
    path, out = lib
    a = AckLibrary(path, out)
    assert (out / fname("solo", "sleep", "Okay... only line.")).is_file()
    for _ in range(8):
        p = a.pick("solo", "sleep")
        assert (p["mood"], p["emotion"], p["source"], p["text"]) == ("idle", "neutral", "clip", "Only line.")
        assert ".sleep." not in p["file"]
        assert a.pick("light_up", pod_mood="sleep")["mood"] == "idle"
    lib2 = json.loads(path.read_text())
    lib2["kinds"]["nap"] = {"default": "sleep", "moods": {"sleep": ["Zzz."], "idle": ["Resting."]}}
    path.write_text(json.dumps(lib2))
    assert a.resolve_mood("nap") == "idle"                               # a sleepy default -> idle
    p = a.pick("nap")                                                    # not rendered: live TTS in idle words
    assert (p["source"], p["mood"], p["text"]) == ("tts", "idle", "Resting.")
    for f in out.glob("solo.idle.*"):
        f.unlink()
    for f in out.glob("solo.happy.*"):
        f.unlink()
    p = a.pick("solo", "sleep")                                          # only the sleepy clip left: still not played
    assert (p["source"], p["mood"]) == ("tts", "idle")
    monkeypatch.setattr(acks, "RETIRED_MOODS", {})                       # ACK_RETIRED_MOODS="" brings it back
    p = a.pick("solo", "sleep")
    assert (p["mood"], p["source"], p["text"]) == ("sleep", "clip", "Okay... only line.")
    assert acks._retired("sleep:idle, thinking") == {"sleep": "idle", "thinking": "idle"}


def test_bad_takes_are_never_played(lib):
    path, out = lib
    man = json.loads((out / "manifest.json").read_text())
    for take in (0, 1):
        for t in ("Okay, I turned it up!", "Brighter now!"):
            man["clips"][acks.clip_key("light_up", "happy", t, take)]["bad"] = "STT: repeat"
    (out / "manifest.json").write_text(json.dumps(man))
    a = AckLibrary(path, out)
    assert {a.pick("light_up", "happy")["text"] for _ in range(10)} == {"There, a little brighter."}


def test_stale_clip_with_old_text_is_not_played(lib):
    path, out = lib
    lib2 = json.loads(path.read_text())
    lib2["kinds"]["solo"]["moods"]["idle"] = ["A new line."]
    path.write_text(json.dumps(lib2))
    a = AckLibrary(path, out)
    p = a.pick("solo")                                                    # idle has no clip for its new text
    assert (p["mood"], p["text"]) == ("happy", "Only happy line!")
    del lib2["kinds"]["solo"]["moods"]["happy"]
    path.write_text(json.dumps(lib2))
    os.utime(path, ns=(1, 1))                                             # make sure the change is seen
    p = a.pick("solo")
    assert (p["source"], p["text"]) == ("tts", "A new line.")


# ------------------------------------------------------------------ bridge: POST /ack to a fake pod
class StubTTS:
    provider = "stub"

    def __init__(self):
        self.said = []

    library = None        # the providers.TTS / house.AckSpeaker surface: no SkwoopOS clips here
    last_backend = ""

    async def synth(self, text, mood=None, previous_text=None, next_text=None):
        self.said.append(text)
        return b"\x20\x00" * (TTS_RATE // 2)


async def _bridge(port, lib):
    app = skwoop_bridge.build_app(port)
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["stt"] = StubSTT()
    app["llm"] = StubLLM()
    app["tts"] = StubTTS()
    app["acks"] = AckLibrary(*lib)
    app["store"].write_key = KEY
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return app, runner


async def _until_stop(ws):
    frames, audio = [], 0
    while True:
        msg = await asyncio.wait_for(ws.receive(), 10)
        if msg.type == WSMsgType.BINARY:
            if audio == 0:
                frames.append("audio")
            audio += 1
            opus.Decoder(TTS_RATE, 1).decode(msg.data, TTS_RATE * 60 // 1000)   # real Opus
            continue
        m = json.loads(msg.data)
        frames.append((m["type"], m.get("state") or m.get("emotion"), m.get("text")))
        if m["type"] == "tts" and m.get("state") == "stop":
            return frames, audio


def test_ack_over_websocket(lib):
    async def go():
        port = 18131
        app, runner = await _bridge(port, lib)
        server = f"http://127.0.0.1:{port}"
        out = {}
        try:
            async with ClientSession() as http:
                async def post(body, headers=AUTH):
                    async with http.post(f"{server}/ack", json=body, headers=headers) as r:
                        return r.status, await r.json()

                out["no_pod"] = await post({"kind": "light_up"})
                ws, _, _ = await fake_pod.connect(http, server)
                out["noauth"] = await post({"kind": "light_up"}, headers={})
                out["unknown"] = await post({"kind": "warp_drive"})
                out["badmood"] = await post({"kind": "light_up", "mood": "grumpy"})

                out["sleep"] = await post({"kind": "light_up", "mood": "sleep"})   # retired -> idle
                out["sleep_frames"] = await _until_stop(ws)

                session = next(iter(app["live"].values()))
                session.set_mood("sad")                                    # e.g. the last reply was bad news
                out["cant"] = await post({"kind": "cant_do"})              # no mood: follows the pod
                out["cant_frames"] = await _until_stop(ws)
                out["light"] = await post({"kind": "light_up"})            # pod mood sad, kind has none
                out["light_frames"] = await _until_stop(ws)
                out["light2"] = await post({"kind": "light_up", "mood": "happy"})
                await _until_stop(ws)

                out["tts"] = await post({"kind": "unrendered"})            # no clip -> live TTS
                out["tts_frames"] = await _until_stop(ws)
                await ws.close()
            out["said"] = app["tts"].said
        finally:
            await runner.cleanup()
        return out

    o = asyncio.run(go())
    assert o["no_pod"][0] == 409 and o["noauth"][0] == 401
    assert o["unknown"][0] == 404 and "light_up" in o["unknown"][1]["kinds"]
    assert o["badmood"][0] == 400 and "grumpy" in o["badmood"][1]["error"]

    status, r = o["sleep"]
    assert status == 200 and (r["mood"], r["emotion"], r["source"]) == ("idle", "neutral", "clip")
    assert r["text"] in LIB["kinds"]["light_up"]["moods"]["idle"]
    frames, audio = o["sleep_frames"]
    assert frames == [("tts", "start", None), ("llm", "neutral", ""), ("tts", "sentence_start", r["text"]),
                      "audio", ("tts", "stop", None)]
    man = json.loads((lib[1] / "manifest.json").read_text())["clips"]
    durs = {man[f"light_up/idle/{acks.text_id(r['text'])}/{t}"]["duration_s"] for t in (0, 1)}
    assert audio in {-(-int(d * 1000) // 60) for d in durs}            # every 60 ms of the clip, as Opus

    assert o["cant"][1]["mood"] == "sad" and o["cant_frames"][0][1] == ("llm", "sad", "")
    assert o["cant"][1]["text"] in LIB["kinds"]["cant_do"]["moods"]["sad"]
    assert o["light"][1]["mood"] == "happy" and o["light_frames"][0][1] == ("llm", "happy", "")
    assert o["light"][1]["text"] != o["light2"][1]["text"]             # no repeat across requests
    assert o["tts"][1]["source"] == "tts" and o["said"] == ["Not rendered yet."]
    assert o["tts_frames"][0][:3] == [("tts", "start", None), ("llm", "neutral", ""),
                                      ("tts", "sentence_start", "Not rendered yet.")]


def test_healthz_and_list(lib):
    async def go():
        port = 18132
        app, runner = await _bridge(port, lib)
        try:
            async with ClientSession() as http:
                async with http.get(f"http://127.0.0.1:{port}/healthz") as r:
                    hz = await r.json()
                os.remove(lib[1] / fname("light_up", "sleep", "Sleepy, but turned up."))
                async with http.get(f"http://127.0.0.1:{port}/healthz") as r:
                    hz2 = await r.json()
                async with http.get(f"http://127.0.0.1:{port}/ack") as r:
                    lst = await r.json()
        finally:
            await runner.cleanup()
        return hz, hz2, lst

    hz, hz2, lst = asyncio.run(go())
    assert (hz["ack_kinds"], hz["ack_buckets"], hz["ack_lines"], hz["ack_recordings"], hz["ack_clips"]) == \
        (4, BUCKETS, LINES, REC, REC - 1)
    assert hz2["ack_clips"] == REC - 2
    assert lst["kinds_list"]["light_up"]["moods"]["sleep"] == {
        "tags": ["[sleepily]", "[tired, slow]"], "takes": 1, "lines": ["Okay... brighter!", "Sleepy, but turned up."],
        "clips": 1}
    assert lst["kinds_list"]["cant_do"]["default"] == "sad" and lst["missing"] == 2
    assert hz["ack_voice"] == "fake" and hz["ack_voice_ok"] is False      # clips not in the live (stub) voice
