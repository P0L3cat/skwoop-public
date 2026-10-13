"""Emotion engine v0.3 (SKWOOP_TONE=pad): one mood per reply sentence for the voice tag and the web pod face.

Source: mood-model commit 753e21c (tag emotion-engine-v0.3), vendored as skwoop_mood/ (runtime files only) with
weights/skwoop_mood_v14.npz (mood model v1.4: v1.1 plus a calm gate) and weights/urgency_clf_v024.npz (urgency v0.2.4:
hashed n-gram logistic classifier plus a trimmed rules floor; sha256 of both below), and pad_map.py +
webpod/static/expressions.json (face side, gain 1.0). FACE-MAP.md section 5 is the plan this follows.
All weight files are CC-BY-SA 4.0 (trained in part on EmoBank): see weights/NOTICE.md before any public push.

Per sentence, sentence(text) -> SentenceMood:
  * urgency floor first: skwoop_mood.urgency(text) fires -> mood "alert" ([crisply]) and the alert PAD on the face,
    whatever the model says. Never lowered by anything below. If the classifier weights fail to load or their
    sha256 is wrong, the floor falls back to the v0.1.2 rules alone (urgency_rules: every rule fires on its own),
    so there is always a floor. Logged, and /healthz shows it.
    When the classifier fires with no rule behind it (p >= 0.83), one INFO line logs p, a 10-hex sha256 prefix of
    the sentence and its length, never the text, so false alerts on live replies can be counted (v0.2.4: the top
    real-reply score was 0.7802).
  * else the model's mood() name (happy, idle, sad, alert, calm, plain) is the voice mood; plain -> [warmly] and
    calm -> [softly]. v1.4's calm gate can turn idle, plain, happy or calm into calm; it only runs here, after the
    floor, so it never acts on a sentence urgency() flags. (MoodModel urgency_override stays off: the floor above
    already does that job, and the override would run urgency() a second time.) (providers.ELEVEN_MOOD_TAGS). A
    positive mood (happy, idle, plain) whose face reads playful or
    curious as its strongest expression (intensity >= expressions.json voice.min_intensity) becomes "playful"
    ([playfully]) or "curious" ([curious]), Alex's approved tags (Oct 4).
  * the face gets the model's PAD; "emotion" stays the keyword tone (tone.classify_tone) for old pods and firmware.
  * the model won't load, or raises on a sentence: keyword tone for that sentence (today's behaviour), TONE_PAD on the
    face. Logged once.

floor_mood(text, mood) is the TTS-side floor for every spoken path (replies, instant replies, inbox renders, previews):
in pad mode an urgent text is spoken as "alert", and an explicit "alert" mood (alerts, alarms, timers) is never changed.
SKWOOP_TONE=keyword (or happy) makes every function here a no-op passthrough: no model calls at all.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tone import classify_tone

log = logging.getLogger("skwoop.emotion")

HERE = Path(__file__).resolve().parent.parent  # bridge/, not this package directory
WEIGHTS = HERE / "weights" / "skwoop_mood_v14.npz"
WEIGHTS_SHA256 = "90b53809c581e84bde9a9ce754e388e1f079118d203f3ce2945ee2b9f8ad949e"
URGENCY_WEIGHTS = HERE / "weights" / "urgency_clf_v024.npz"
URGENCY_SHA256 = "ce7ad638f3e9cf49db9423b7aacfbdcfcadbd21fbf6633dfc7acf1fc1800bc9a"
EXPRESSIONS = HERE / "webpod" / "static" / "expressions.json"
VERSION = "emotion-engine v0.3 (mood-model 753e21c)"

Pad3 = tuple[float, float, float]
TONE_PAD: dict[str, Pad3] = {"neutral": (0.15, 0.15, 0.10), "happy": (0.55, 0.40, 0.25), "sad": (-0.45, -0.20, -0.20)}
ALERT_PAD: Pad3 = (-0.20, 0.65, 0.00)          # mapping.PROTOTYPES "alert": the face reads surprised + worried
POSITIVE = frozenset({"happy", "idle", "plain"})
FACE_VOICE = {"playful": "playful", "curious": "curious"}   # face expression -> voice mood (ELEVEN_MOOD_TAGS key)


def pad_mode() -> bool:
    return os.environ.get("SKWOOP_TONE", "keyword").strip().lower() == "pad"


@dataclass(frozen=True)
class SentenceMood:
    tone: str                  # keyword tone: "emotion" for old pods, the status mood
    mood: str                  # voice mood for tts.synth (ELEVEN_MOOD_TAGS key, or the keyword tone on fallback)
    pad: Pad3                  # face point
    source: str                # "model" | "floor" | "keyword"
    ms: float = 0.0            # time spent in sentence()

    def pad_json(self) -> dict[str, float]:
        v, a, d = self.pad
        return {"v": round(v, 2), "a": round(a, 2), "d": round(d, 2)}

    def status_mood(self) -> str:
        return "alert" if self.source == "floor" else self.tone


class Engine:
    """Loads the vendored model once. Never raises after construction; .error says why it is on the keyword path."""

    def __init__(self, weights: Path = WEIGHTS, expressions: Path = EXPRESSIONS) -> None:
        self.error: str | None = None
        self.model: Any = None
        self.fm: Any = None
        self._urgency: Any = None
        self.floor_kind = "none"     # "classifier+rules" (v0.2.4 urgency), "rules-only" (fallback) or "none"
        self._warned = False
        self._mod: Any = None              # skwoop_mood.urgency, for urgency_reasons() and the classifier p
        self._last_alone: tuple[str, float] = ("", 0.0)
        self._load_urgency()
        try:
            from skwoop_mood.model import MoodModel, load_weights
            import pad_map
            path = Path(weights)
            if not path.is_file():
                raise FileNotFoundError(
                    f"{path} is missing. Mood weights ship separately (CC-BY-SA); see bridge/docs/mood-weights.md")
            got = hashlib.sha256(path.read_bytes()).hexdigest()
            if path == WEIGHTS and got != WEIGHTS_SHA256:
                raise ValueError(f"weights sha256 {got[:12]}... is not the v1.4 file")
            model = MoodModel(load_weights(Path(weights)))
            model.mood("Warm-up.")
            self.fm = pad_map.load(Path(expressions))
            self.model = model
        except Exception as e:  # noqa: BLE001 - any load failure -> keyword tone
            self.model, self.fm = None, None
            msg = f"{type(e).__name__}: {e}"
            # Keep an earlier urgency-load error; the rules floor still names the missing file.
            self.error = f"{self.error}; {msg}" if self.error else msg
            log.warning("emotion engine unavailable (%s): keyword tone", self.error)
        else:
            log.info("emotion engine ready: %s", VERSION)

    @property
    def ready(self) -> bool:
        return self.model is not None

    def _load_urgency(self) -> None:
        try:
            import importlib
            u = importlib.import_module("skwoop_mood.urgency")   # the module; the package re-exports urgency()
        except Exception as e:  # noqa: BLE001
            self.error = f"urgency: {type(e).__name__}: {e}"
            log.error("emotion engine: urgency() unavailable (%s): NO urgency floor", self.error)
            return
        try:
            if not URGENCY_WEIGHTS.is_file():
                raise FileNotFoundError(
                    f"{URGENCY_WEIGHTS} is missing. Mood weights ship separately (CC-BY-SA); "
                    "see bridge/docs/mood-weights.md")
            got = hashlib.sha256(URGENCY_WEIGHTS.read_bytes()).hexdigest()
            if got != URGENCY_SHA256:
                raise ValueError(f"urgency weights sha256 {got[:12]}... is not the v0.2.4 file")
            u.urgency_reasons("Warm-up.")               # loads and caches the classifier
            self._mod = u
            self._urgency, self.floor_kind = self._classifier_floor, "classifier+rules"
        except Exception as e:  # noqa: BLE001 - keep a floor: the rules need no weights
            self._urgency, self.floor_kind = u.urgency_rules, "rules-only"
            self.error = f"urgency classifier: {type(e).__name__}: {e}"
            log.error("emotion engine: urgency classifier unavailable (%s): rules-only floor", self.error)

    def _classifier_floor(self, text: str) -> bool:
        reasons = self._mod.urgency_reasons(text)
        if reasons == ("classifier",):
            self._log_classifier_alone(text)
        return bool(reasons)

    def _log_classifier_alone(self, text: str) -> None:
        """One line per sentence that only the classifier flagged: p, a short hash and the length, never the text."""
        try:
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
            now = time.monotonic()
            if self._last_alone[0] == h and now - self._last_alone[1] < 10.0:
                return                                  # the TTS-side floor re-checks the same sentence
            self._last_alone = (h, now)
            clf = self._mod.default_clf()
            p = self._mod.urgency_probability(text, clf)
            log.info("urgency floor from the classifier alone: p=%.4f (threshold %.2f) text sha256:%s len=%d",
                     p, clf.threshold, h, len(text))
        except Exception as e:  # noqa: BLE001 - logging never changes the floor
            log.warning("urgency classifier-alone log failed (%s)", type(e).__name__)

    def describe(self) -> dict[str, Any]:
        return {"ready": self.ready, "floor": self._urgency is not None, "floor_kind": self.floor_kind,
                "version": VERSION, "error": self.error}

    def urgent(self, text: str) -> bool:
        if self._urgency is None:
            return False
        try:
            return bool(self._urgency(text))
        except Exception as e:  # noqa: BLE001
            log.warning("urgency() failed on one sentence (%s)", type(e).__name__)
            return False

    def sentence(self, text: str) -> SentenceMood:
        t0 = time.perf_counter()
        tone = classify_tone(text)
        if self.urgent(text):
            return SentenceMood(tone, "alert", ALERT_PAD, "floor", (time.perf_counter() - t0) * 1000)

        if self.model is not None:
            try:
                r = self.model.mood(text)
                pad: Pad3 = (float(r.v), float(r.a), float(r.d))
                name = str(r.name)
                if name in POSITIVE:
                    name = self._face_voice(pad) or name
                return SentenceMood(tone, name, pad, "model", (time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001 - this sentence only
                if not self._warned:
                    self._warned = True
                    log.warning("mood model failed on a sentence (%s): keyword tone for it", type(e).__name__)

        return SentenceMood(tone, tone, TONE_PAD.get(tone, TONE_PAD["neutral"]), "keyword",
                            (time.perf_counter() - t0) * 1000)

    def _face_voice(self, pad: Pad3) -> str | None:
        import pad_map
        w = pad_map.expression_weights(self.fm, pad_map.Pad(*pad))
        if sum(w) < self.fm.voice.min_intensity:
            return None

        top = self.fm.expressions[max(range(len(w)), key=w.__getitem__)].name
        return FACE_VOICE.get(top)


_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


def floor_mood(text: str, mood: str | None) -> str | None:
    """TTS-side urgency floor (pad mode only): urgent text -> "alert"; an explicit "alert" is never lowered."""
    if not pad_mode() or mood == "alert" or not text:
        return mood

    return "alert" if engine().urgent(text) else mood
