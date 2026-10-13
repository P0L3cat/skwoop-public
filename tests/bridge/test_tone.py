"""Reply tone: tone.classify_tone + the per-sentence "llm" emotion frames skwoop_bridge sends.

No network, no keys, stub STT/LLM/TTS. Run:  python -m pytest -q tests  (or: python tests/test_tone.py)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bridge")
sys.path.insert(0, BRIDGE)
_tmp = tempfile.mkdtemp()
os.environ.setdefault("STATUS_FILE", os.path.join(_tmp, "status.json"))
os.environ.setdefault("INBOX_DIR", os.path.join(_tmp, "inbox"))
os.environ.setdefault("STATUS_WRITE_KEY", "test-only-key")

import tone  # noqa: E402
from stubs import StubSTT  # noqa: E402
from tone import classify_tone  # noqa: E402

HAPPY_LINE = "Deploy finished and all tests passed. Nice work."
BAD_LINE = "The deploy failed and the database is down."


# ----------------------------------------------------------------------------- classifier
def test_happy_line():
    assert classify_tone(HAPPY_LINE) == "happy"
    assert classify_tone("The build is green.") == "happy"
    assert classify_tone("All set, the backup is saved.") == "happy"


def test_bad_news_line():
    assert classify_tone(BAD_LINE) == "sad"
    assert classify_tone("Sorry, I couldn't reach the Deep line.") == "sad"
    assert classify_tone("The job timed out.") == "sad"
    assert classify_tone("CI is red again.") == "sad"


def test_sad_beats_happy():
    assert classify_tone("The build passed but the deploy failed.") == "sad"


def test_neutral():
    assert classify_tone("It's three fifteen.") == "neutral"
    assert classify_tone("Skwoop is deploying the bridge.") == "neutral"
    assert classify_tone("") == "neutral"
    assert classify_tone("   ") == "neutral"


def test_word_start_match():
    assert classify_tone("Your package was delivered.") == "happy"   # "red" is not a word start here
    assert classify_tone("Three tests failed.") == "sad"             # "fail" matches "failed"
    assert classify_tone("Congratulations on the launch.") == "happy"


def test_negation_sad_word():
    assert classify_tone("No errors this time, it went through.") == "neutral"   # was sad (RESULTS.md)
    assert classify_tone("No errors, and all tests passed.") == "happy"
    assert classify_tone("Nothing crashed overnight.") == "neutral"
    assert classify_tone("Zero errors and no warnings.") == "neutral"
    assert classify_tone("It finished without a single error.") == "happy"
    assert classify_tone("The site never went down.") == "neutral"
    assert classify_tone("Not a problem, done.") == "happy"


def test_negation_happy_word():
    assert classify_tone("That's not good.") == "neutral"
    assert classify_tone("The database isn't back up yet.") == "neutral"
    assert classify_tone("The migration is not finished.") == "neutral"


def test_negation_does_not_hide_bad_news():
    assert classify_tone("No, the deploy failed.") == "sad"                      # comma ends the clause
    assert classify_tone("No errors in the build. The deploy failed.") == "sad"  # one un-negated hit is enough
    assert classify_tone("Not sure why, but the database is down.") == "sad"
    assert classify_tone("The login is not working.") == "sad"                   # "not working" is a sad keyword
    assert classify_tone("I didn't get a reply.") == "sad"                       # "didn't" is a sad keyword
    assert classify_tone("Not great: the sync is stuck.") == "sad"
    assert classify_tone("Unfortunately it's not done.") == "sad"


def test_negation_window_is_three_words():
    assert classify_tone("There were no more failures.") == "neutral"            # "no" 2 words before
    assert classify_tone("There were not a single failure.") == "neutral"        # "not" 3 words before
    assert classify_tone("There were no more than two failures.") == "sad"       # "no" 4 words before
    assert classify_tone("No one told me the upload was lost.") == "sad"         # "no" 6 words before


def test_curly_apostrophe():
    assert classify_tone("Sorry, I couldn\u2019t reach it.") == "sad"
    assert classify_tone("It isn\u2019t ready.") == "neutral"


def test_deterministic():
    lines = [HAPPY_LINE, BAD_LINE, "No errors this time.", "It's noon."]
    first = [classify_tone(s) for s in lines]
    for _ in range(50):
        assert [classify_tone(s) for s in lines] == first


def _decider_table(text):
    """skwoop-decider/src/control.py predict_one, verbatim, as the reference."""
    import re
    t = " " + re.sub(r"[^a-z' ]+", " ", text.lower()) + " "
    if any((" " + k) in t for k in tone.SAD_WORDS):
        return "sad"
    if any((" " + k) in t for k in tone.HAPPY_WORDS):
        return "happy"
    return "neutral"


def test_same_as_decider_table_without_negators():
    assert len(tone.SAD_WORDS) + len(tone.HAPPY_WORDS) == 47
    lines = [HAPPY_LINE, BAD_LINE, "The timed-out job is back-up now.", "All-set!", "Build is green; deploy crashed.",
             "Great, it's done.", "Apologies, the Wi-Fi is offline.", "Your 3 PM moved.", "Working again, finally."]
    for s in lines:
        assert classify_tone(s) == _decider_table(s), s
    data = os.environ.get("SKWOOP_TONE_DATA", "")
    if data and os.path.isdir(data):
        n = 0
        for name in sorted(os.listdir(data)):
            if name.endswith(".jsonl"):
                for line in open(os.path.join(data, name)):
                    text = json.loads(line)["text"]
                    words = set(text.lower().replace("\u2019", "'").replace(",", " ").replace(".", " ").split())
                    if not words & tone.NEGATORS:
                        assert classify_tone(text) == _decider_table(text), text
                        n += 1
        assert n > 1000


# ----------------------------------------------------------------------------- bridge wiring
class FakeWS:
    closed = False

    def __init__(self):
        self.frames = []
        self.audio_frames = 0

    async def send_str(self, s):
        self.frames.append(json.loads(s))

    async def send_bytes(self, b):
        self.audio_frames += 1


class ToneSTT(StubSTT):
    def __init__(self, fail=False):
        self.fail = fail

    async def transcribe(self, pcm, rate):
        if self.fail:
            raise RuntimeError("stt down")
        return "how did the deploy go"


class StubLLM:
    mode = "stub"

    def __init__(self, sentences):
        self.sentences = sentences

    async def stream_sentences(self, messages, fast=False):
        for s in self.sentences:
            yield s


class StubTTS:
    provider = "stub"

    library = None        # the providers.TTS / house.AckSpeaker surface: no SkwoopOS clips here
    last_backend = ""

    async def synth(self, text, mood=None, previous_text=None, next_text=None):
        return b"\x00\x01" * 1440  # one 60 ms frame at 24 kHz


def _turn(sentences, mode="keyword", stt_fail=False):
    import skwoop_bridge
    old = skwoop_bridge.TONE_MODE
    skwoop_bridge.TONE_MODE = mode
    try:
        async def go():
            app = skwoop_bridge.build_app(0)
            app["stt"], app["llm"], app["tts"] = ToneSTT(stt_fail), StubLLM(sentences), StubTTS()
            ws = FakeWS()
            s = skwoop_bridge.Session(ws, "aa:bb:cc:00:00:01", app)
            s.mode = "manual"
            await s.respond(b"\x00\x00" * 16000)
            return ws
        ws = asyncio.run(go())
    finally:
        skwoop_bridge.TONE_MODE = old
    return [{k: v for k, v in f.items() if k != "session_id"} for f in ws.frames], ws


def _emotions_before_sentences(frames):
    """[(emotion, sentence)] pairing each reply sentence with the llm emotion sent just before it."""
    out, last = [], None
    for f in frames:
        if f["type"] == "llm" and f["emotion"] != "thinking":
            last = f["emotion"]
        if f["type"] == "tts" and f.get("state") == "sentence_start":
            out.append((last, f["text"]))
    return out


def test_bridge_sends_real_tone_per_sentence():
    frames, ws = _turn(["Deploy finished and all tests passed.", "Nice work.", "It's three fifteen.",
                        "The deploy failed and the database is down."])
    assert _emotions_before_sentences(frames) == [
        ("happy", "Deploy finished and all tests passed."), ("happy", "Nice work."),
        ("neutral", "It's three fifteen."), ("sad", "The deploy failed and the database is down.")]
    kinds = [(f["type"], f.get("state") or f.get("emotion")) for f in frames]
    assert kinds[:4] == [("llm", "thinking"), ("stt", None), ("tts", "start"), ("llm", "happy")]
    assert kinds[-1] == ("tts", "stop") and kinds.count(("tts", "start")) == 1
    for f in frames:  # same shape as the old frame: {"type":"llm","emotion":...,"text":""}
        if f["type"] == "llm":
            assert set(f) == {"type", "emotion", "text"} and f["text"] == ""
    assert ws.audio_frames == 4


def test_bridge_bad_news_reply_is_sad():
    frames, _ = _turn([BAD_LINE])
    assert _emotions_before_sentences(frames) == [("sad", BAD_LINE)]
    assert not any(f["type"] == "llm" and f["emotion"] == "happy" for f in frames)


def test_bridge_happy_mode_is_old_behaviour():
    frames, _ = _turn([BAD_LINE, "Nice work."], mode="happy")
    llm = [f["emotion"] for f in frames if f["type"] == "llm"]
    assert llm == ["thinking", "happy"]  # exactly one "happy" after tts start, like before the fix
    kinds = [(f["type"], f.get("state") or f.get("emotion")) for f in frames]
    assert kinds[2:4] == [("tts", "start"), ("llm", "happy")]


def test_error_path_stays_sad():
    frames, _ = _turn(["unused"], stt_fail=True)
    alerts = [f for f in frames if f["type"] == "alert"]
    assert alerts == [{"type": "alert", "status": "Error", "message": "Voice line hiccup - try again",
                       "emotion": "sad"}]
    assert not any(f["type"] == "tts" for f in frames)


def test_skwoop_tone_env():
    def mode(value):
        env = {**os.environ, "SKWOOP_TONE": value} if value is not None else {
            k: v for k, v in os.environ.items() if k != "SKWOOP_TONE"}
        out = subprocess.run([sys.executable, "-c", "import skwoop_bridge; print(skwoop_bridge.TONE_MODE)"],
                             cwd=BRIDGE, env=env, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    assert mode(None) == "keyword"
    assert mode("happy") == "happy"
    assert mode(" HAPPY ") == "happy"
    assert mode("keyword") == "keyword"
    assert mode("bogus") == "keyword"


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"TONE OK ({n} tests)")
