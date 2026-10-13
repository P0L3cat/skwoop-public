"""PAD -> Skwoop mood name + ElevenLabs v4 delivery tag.

Mood names and tags are the bridge's own (providers.ELEVEN_MOOD_TAGS), plus "plain" -> [warmly] (Alex, 2026-10-04).
The rule is nearest prototype in PAD space with axis weights (1, 1, 0.5): dominance is the weakest-predicted axis, so it
counts half. Ties go to the earlier prototype in PROTOTYPES. Frozen in LOCK.md before training.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

MoodName = Literal["happy", "idle", "sad", "alert", "calm", "plain"]


@dataclass(frozen=True)
class Prototype:
    name: MoodName
    v: float
    a: float
    d: float


# Bridge tags, verbatim from providers.ELEVEN_MOOD_TAGS, plus "plain" -> [warmly]: Alex's decision 2026-10-04 10:28 CT
# (tag map only; model outputs unchanged). The bridge's own map has no "plain" entry yet; that edit is a follow-up.
MOOD_TAGS: Final[dict[MoodName, str]] = {
    "happy": "[cheerfully]",
    "idle": "[warmly]",
    "sad": "[apologetically]",
    "alert": "[crisply]",
    "calm": "[softly]",
    "plain": "[warmly]",
}

PROTOTYPES: Final[tuple[Prototype, ...]] = (
    Prototype("idle", 0.15, 0.00, 0.15),
    Prototype("plain", -0.15, 0.05, 0.00),
    Prototype("happy", 0.60, 0.45, 0.30),
    Prototype("calm", 0.30, -0.50, 0.10),
    Prototype("sad", -0.50, 0.00, -0.30),
    Prototype("alert", -0.20, 0.65, 0.00),
)

AXIS_WEIGHTS: Final[tuple[float, float, float]] = (1.0, 1.0, 0.5)


def _distance(p: Prototype, v: float, a: float, d: float) -> float:
    wv, wa, wd = AXIS_WEIGHTS

    return wv * (v - p.v) ** 2 + wa * (a - p.a) ** 2 + wd * (d - p.d) ** 2


def nearest_mood(prototypes: tuple[Prototype, ...], v: float, a: float, d: float) -> MoodName:
    """Nearest prototype (weighted squared distance); earlier prototype wins a tie."""
    best = prototypes[0]
    best_dist = _distance(best, v, a, d)
    for p in prototypes[1:]:
        dist = _distance(p, v, a, d)
        if dist < best_dist:
            best, best_dist = p, dist

    return best.name


def mood_tag(name: MoodName) -> str:
    return MOOD_TAGS[name]
