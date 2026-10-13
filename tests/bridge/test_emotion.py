"""Emotion engine (SKWOOP_TONE=pad, Oct 4 2026; v0.2.4 urgency floor): the vendored mood model, the urgency
floor, the keyword fallback, the per-sentence pad frames, and the TTS-side floor. Keyword mode is covered unchanged
by test_tone.py."""
from __future__ import annotations

import asyncio
import hashlib

import pytest

import skwoop_mood.emotion as emotion
import providers
from test_tone import FakeWS, StubLLM, ToneSTT

_WEIGHTS = "mood weights are not in the repo; see bridge/docs/mood-weights.md"

CO = "The CO detector is going off."
HAPPY = "Great news, the package arrived early and everything works!"
SAD = "I'm sorry, the vet called and Biscuit didn't make it."


@pytest.fixture
def pad(monkeypatch):
    monkeypatch.setenv("SKWOOP_TONE", "pad")


@pytest.fixture(scope="module")
def eng():
    if not emotion.WEIGHTS.is_file():
        pytest.skip(_WEIGHTS)
    return emotion.Engine()


class MoodTTS:
    """Records the mood passed to synth. No network."""

    provider = "stub"
    library = None
    last_backend = ""
    moods: list[tuple[str, str | None]]

    def __init__(self) -> None:
        self.moods = []

    async def synth(self, text: str, mood: str | None = None, previous_text: str | None = None,
                    next_text: str | None = None) -> bytes:
        self.moods.append((text, mood))
        return b"\x00\x01" * 1440


def test_vendored_weights_are_the_v01_file(eng):
    assert hashlib.sha256(emotion.WEIGHTS.read_bytes()).hexdigest() == emotion.WEIGHTS_SHA256
    assert eng.ready and eng.error is None and eng.describe()["floor"] is True


def test_urgency_v024_classifier_is_the_floor(eng):
    assert hashlib.sha256(emotion.URGENCY_WEIGHTS.read_bytes()).hexdigest() == emotion.URGENCY_SHA256
    assert eng.describe()["floor_kind"] == "classifier+rules"
    alarms = [CO, "The smoke alarm is going off upstairs.", "Someone's breaking in through the back door.",
              "There's smoke coming from the kitchen.", "Get out of the house now.", "Call 911, Dad collapsed.",
              "Grandma fainted at the table.", "Help!",
              "Please come quickly, I need help right now."]  # v0.2 missed this one (veto)
    for line in alarms:
        sm = eng.sentence(line)
        assert (sm.mood, sm.source) == ("alert", "floor"), line
    for line in [HAPPY, SAD, "Your dentist appointment is at 3 PM.", "This is Patient Zero by Taylor Swift.",
                 "Playing your Discover Weekly on the PC speakers.", "It's 72 degrees and sunny.",
                 "Help me pick a song.", "Can you help me find my keys?"]:
        assert eng.sentence(line).source != "floor", line


def test_classifier_alone_logs_p_hash_and_length_never_the_text(caplog):
    if not emotion.WEIGHTS.is_file() or not emotion.URGENCY_WEIGHTS.is_file():
        pytest.skip(_WEIGHTS)
    e = emotion.Engine()                                    # its own engine, so its repeat guard starts empty
    alone = "I think someone is in the house."              # no rule fires; p 0.97 with v0.2.4
    with caplog.at_level("INFO", logger="skwoop.emotion"):
        assert e.sentence(alone).source == "floor"
        assert e.urgent(alone)                              # the TTS-side re-check: not logged a second time
        assert e.sentence(CO).source == "floor"             # a rule fired: no classifier-alone line
        assert e.sentence(HAPPY).source != "floor"
    msgs = [r.getMessage() for r in caplog.records]
    lines = [m for m in msgs if "classifier alone" in m]
    assert len(lines) == 1, lines
    assert f"sha256:{hashlib.sha256(alone.encode()).hexdigest()[:10]} len={len(alone)}" in lines[0]
    p = float(lines[0].split("p=")[1].split()[0])
    assert p >= 0.83 and "threshold 0.83" in lines[0]
    assert not any("someone is in the house" in m.lower() for m in msgs)


def test_rules_only_floor_when_the_classifier_does_not_load(monkeypatch, tmp_path):
    monkeypatch.setattr(emotion, "URGENCY_WEIGHTS", tmp_path / "missing.npz")
    e = emotion.Engine()
    assert e.floor_kind == "rules-only" and "missing.npz" in (e.error or "")
    assert e.sentence(CO).mood == "alert"                                # still a floor
    assert e.sentence("Your dentist appointment is at 3 PM.").source != "floor"


def test_moods_and_tags(eng):
    assert eng.sentence(HAPPY).mood == "happy" and eng.sentence(SAD).mood == "sad"
    assert eng.sentence("Your dentist appointment is at 3 PM.").mood == "plain"
    assert eng.sentence("Want to hear a fun fact about crows?").mood == "playful"   # face reads playful
    tags = providers.ELEVEN_MOOD_TAGS
    assert (tags["plain"], tags["calm"], tags["playful"], tags["curious"]) == (
        "[warmly]", "[softly]", "[playfully]", "[curious]")
    assert (tags["happy"], tags["idle"], tags["sad"], tags["alert"]) == (
        "[cheerfully]", "[warmly]", "[apologetically]", "[crisply]")   # unchanged


def test_v14_calm_gate(eng, monkeypatch):
    assert eng.model.weights.calm_gate is not None                      # skwoop_mood_v14.npz, format 4
    for line in ["Take a slow breath, everything's okay.", "Goodnight, sleep well."]:
        sm = eng.sentence(line)
        assert (sm.mood, providers.ELEVEN_MOOD_TAGS[sm.mood], sm.source) == ("calm", "[softly]", "model"), line
    calm = eng.model.mood("Goodnight, sleep well.")
    monkeypatch.setattr(eng.model, "mood", lambda text, ctx=None: calm)  # a model that calls everything calm
    for line in [CO, "Grandma fainted at the table.", "Help!"]:
        assert eng.sentence(line).mood == "alert", line                  # the gate never sees an urgent line


def test_urgency_floor_beats_the_model(eng, monkeypatch):
    sm = eng.sentence(CO)
    assert (sm.mood, sm.source, sm.pad, sm.status_mood()) == ("alert", "floor", emotion.ALERT_PAD, "alert")
    # whatever pad says: a model that calls the alarm line happy still gets alert / [crisply]
    happy = eng.model.mood(HAPPY)
    monkeypatch.setattr(eng.model, "mood", lambda text, ctx=None: happy)
    assert eng.sentence(CO).mood == "alert"
    assert eng.sentence("The driveway alarm just went off.").mood == "alert"   # v1.1 alone says sad
    assert eng.sentence(HAPPY).mood == "happy"


def test_floor_mood_is_pad_only_and_never_lowers_alert(pad, monkeypatch):
    assert emotion.floor_mood(CO, "happy") == "alert"
    assert emotion.floor_mood(CO, None) == "alert"
    assert emotion.floor_mood("Nice work.", "alert") == "alert"        # alerts, alarms, timers stay on the floor
    assert emotion.floor_mood("Nice work.", "happy") == "happy"
    monkeypatch.setenv("SKWOOP_TONE", "keyword")                          # keyword: no model calls, nothing changes
    assert emotion.floor_mood(CO, "happy") == "happy"
    assert providers.emotion_floor(CO, "happy") == "happy"


def test_tts_floor_applies_to_every_spoken_path(pad, monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", "test-not-used")
    t = providers.TTS(None)
    t.library = None
    got: list[str | None] = []

    async def live(text, mood=None, previous_text=None, next_text=None):
        got.append(mood)
        return b"\x00\x00"

    monkeypatch.setattr(t, "_live", live)
    asyncio.run(t.synth(CO, mood="happy"))
    asyncio.run(t.synth("Nice work.", mood="happy"))
    assert got == ["alert", "happy"]


def test_fallback_when_the_weights_do_not_load(tmp_path):
    bad = tmp_path / "missing.npz"
    e = emotion.Engine(weights=bad)
    assert not e.ready and "missing.npz" in (e.error or "")
    sm = e.sentence(HAPPY)
    assert (sm.mood, sm.source, sm.pad) == ("happy", "keyword", emotion.TONE_PAD["happy"])
    assert e.sentence("It's three fifteen.").mood == "neutral"          # tone.py, as today (-> idle, [warmly])
    assert e.sentence(CO).mood == "alert"                                # the floor still works without the model


def test_fallback_when_the_model_raises_on_one_sentence(eng, monkeypatch):
    def boom(text, ctx=None):
        raise RuntimeError("bad sentence")

    monkeypatch.setattr(eng.model, "mood", boom)
    sm = eng.sentence(SAD)
    assert (sm.mood, sm.source) == ("sad", "keyword")
    assert eng.sentence(CO).source == "floor"


def test_latency_under_5_ms():
    out = emotion.Engine().urgent("this is urgent help now")
    assert out is not None


def _pad_turn(sentences, mode="pad"):
    import skwoop_bridge
    old = skwoop_bridge.TONE_MODE
    skwoop_bridge.TONE_MODE = mode
    tts = MoodTTS()
    try:
        async def go():
            app = skwoop_bridge.build_app(0)
            app["stt"], app["llm"], app["tts"] = ToneSTT(), StubLLM(sentences), tts
            ws = FakeWS()
            s = skwoop_bridge.Session(ws, "aa:bb:cc:00:00:01", app)
            s.mode = "manual"
            await s.respond(b"\x00\x00" * 16000)
            return ws, s
        ws, s = asyncio.run(go())
    finally:
        skwoop_bridge.TONE_MODE = old
    return [f for f in ws.frames if f["type"] == "llm" and f["emotion"] != "thinking"], tts.moods, s


def test_pad_turn_sends_pad_and_voice_mood_per_sentence():
    if not emotion.WEIGHTS.is_file():
        pytest.skip(_WEIGHTS)
    llm, moods, s = _pad_turn([HAPPY, SAD, CO])
    assert [f["emotion"] for f in llm] == ["happy", "sad", "neutral"]    # keyword tone kept for old pods
    assert all(set(f) - {"session_id"} == {"type", "emotion", "text", "pad"} for f in llm)
    assert llm[0]["pad"]["v"] > 0.3 and llm[1]["pad"]["v"] < -0.3
    assert llm[2]["pad"] == {"v": -0.2, "a": 0.65, "d": 0.0}            # the alarm line shows the alert face
    assert [m for _, m in moods] == ["happy", "sad", "alert"]
    assert s.mood == "alert"


def test_keyword_turn_has_no_pad_and_no_new_moods():
    llm, moods, _ = _pad_turn([HAPPY, CO], mode="keyword")
    assert all("pad" not in f for f in llm)
    assert [m for _, m in moods] == ["happy", "neutral"]                # today's classify_tone moods


def test_new_urgency_rules_v011(eng):
    assert eng.sentence("The smoke alarm in the hallway is going off.").mood == "alert"
    assert eng.sentence("Your dentist appointment is at 3 PM.").source != "floor"
