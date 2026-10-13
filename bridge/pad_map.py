"""PAD -> robot-face expression weights and ElevenLabs delivery tag, for the bridge side.

Vendored into the bridge from mood-model c7903f7 (emotion-engine-v0.1.1)
face-preview/tools/pad_map.py (Oct 4 2026, emotion engine v0.1).
Same rules and numbers as webpod/static/pad-face.js (both read webpod/static/expressions.json); tests/test_pad_map.py
runs the JS under Node and checks the two agree. FACE-MAP.md documents the rules.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

EXPRESSIONS_JSON: Final = Path(__file__).resolve().parent / "webpod" / "static" / "expressions.json"  # bridge copy


@dataclass(frozen=True)
class Pad:
    v: float
    a: float
    d: float

    @staticmethod
    def parse(v: float, a: float, d: float) -> Pad:
        """Clamp each axis to -1..1; NaN or infinity becomes 0 (as in pad-face.js)."""

        def unit(x: float) -> float:
            return max(-1.0, min(1.0, x)) if math.isfinite(x) else 0.0

        return Pad(unit(v), unit(a), unit(d))

    def dot(self, other: Pad) -> float:
        return self.v * other.v + self.a * other.a + self.d * other.d

    def norm(self) -> float:
        return math.sqrt(self.dot(self))


@dataclass(frozen=True)
class Expression:
    name: str
    pad: Pad
    tag: str


@dataclass(frozen=True)
class BlendRule:
    min_radius: float
    full_radius: float
    cos_lo: float
    cos_hi: float
    idw_power: float


@dataclass(frozen=True)
class VoiceRule:
    min_intensity: float
    default_tag: str
    approved_tags: frozenset[str]
    proposed_fallback: dict[str, str]


@dataclass(frozen=True)
class FaceMap:
    expressions: tuple[Expression, ...]
    blend: BlendRule
    voice: VoiceRule
    va_gain: float  # display-side gain on valence and arousal before the blend (face only)


def _num(raw: object, what: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ValueError(f"{what} must be a number")

    return float(raw)


def _str(raw: object, what: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{what} must be a string")

    return raw


def _obj(raw: object, what: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be an object")

    return {_str(k, f"{what} key"): v for k, v in raw.items()}


def _list(raw: object, what: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{what} must be a list")

    return raw


def load(path: Path = EXPRESSIONS_JSON) -> FaceMap:
    """Parse expressions.json (the boundary) into typed rules."""
    doc = _obj(json.loads(path.read_text()), "expressions.json")
    exprs: list[Expression] = []
    for i, raw in enumerate(_list(doc["expressions"], "expressions")):
        e = _obj(raw, f"expressions[{i}]")
        p = _list(e["pad"], f"expressions[{i}].pad")
        if len(p) != 3:
            raise ValueError(f"expressions[{i}].pad must have 3 numbers")

        pad = Pad(*(_num(x, f"expressions[{i}].pad") for x in p))
        exprs.append(Expression(_str(e["name"], "name"), pad, _str(e["tag"], "tag")))

    b = _obj(doc["pad_blend"], "pad_blend")
    blend = BlendRule(
        *(_num(b[k], f"pad_blend.{k}") for k in ("min_radius", "full_radius", "cos_lo", "cos_hi", "idw_power"))
    )
    vo = _obj(doc["voice"], "voice")
    voice = VoiceRule(
        min_intensity=_num(vo["min_intensity"], "voice.min_intensity"),
        default_tag=_str(vo["default_tag"], "voice.default_tag"),
        approved_tags=frozenset(_str(t, "approved tag") for t in _list(vo["approved_tags"], "approved_tags")),
        proposed_fallback={k: _str(v, "fallback tag") for k, v in _obj(vo["proposed_tags"], "proposed_tags").items()},
    )

    va_gain = _num(_obj(doc["display"], "display")["va_gain"], "display.va_gain")

    return FaceMap(tuple(exprs), blend, voice, va_gain)


def _smoothstep(lo: float, hi: float, x: float) -> float:
    t = max(0.0, min(1.0, (x - lo) / (hi - lo)))

    return t * t * (3 - 2 * t)


def expression_weights(fm: FaceMap, raw: Pad, va_gain: float | None = None) -> list[float]:
    """Direction picks the expressions (inverse-angle weights within the cosine gate), distance sets intensity.

    The weights sum to the intensity k (0..1): a point at 30% of an anchor gives that anchor 0.3. Valence and arousal
    are first multiplied by the display gain (va_gain, default from expressions.json) and clamped; dominance is not.
    """
    g = fm.va_gain if va_gain is None else va_gain
    pad = Pad.parse(g * raw.v, g * raw.a, raw.d)
    r = pad.norm()
    n = len(fm.expressions)
    if r < fm.blend.min_radius:
        return [0.0] * n

    scores: list[float] = []
    best, best_cos = 0, -2.0
    for i, e in enumerate(fm.expressions):
        c = pad.dot(e.pad) / (r * e.pad.norm())
        if c > best_cos:
            best, best_cos = i, c

        gate = _smoothstep(fm.blend.cos_lo, fm.blend.cos_hi, c)
        theta = max(math.acos(max(-1.0, min(1.0, c))), 1e-4)
        scores.append(gate / theta**fm.blend.idw_power if gate > 0 else 0.0)

    total = sum(scores)
    if total == 0:
        scores[best], total = 1.0, 1.0

    w = [x / total for x in scores]
    mean = Pad(
        sum(wi * e.pad.v for wi, e in zip(w, fm.expressions, strict=True)),
        sum(wi * e.pad.a for wi, e in zip(w, fm.expressions, strict=True)),
        sum(wi * e.pad.d for wi, e in zip(w, fm.expressions, strict=True)),
    )
    m2 = mean.dot(mean)
    fade = _smoothstep(fm.blend.min_radius, fm.blend.full_radius, r)  # dead zone around neutral
    k = fade * max(0.0, min(1.0, pad.dot(mean) / m2)) if m2 > 0 else 0.0

    return [x * k for x in w]


def voice_tag(fm: FaceMap, weights: list[float], allow_proposed: bool = False) -> str:
    """Strongest expression's tag at intensity >= min_intensity; unapproved tags fall back until cleared."""
    if sum(weights) < fm.voice.min_intensity:
        return fm.voice.default_tag

    top = max(range(len(weights)), key=lambda i: weights[i])
    tag = fm.expressions[top].tag
    if tag in fm.voice.approved_tags or allow_proposed:
        return tag

    return fm.voice.proposed_fallback.get(tag, fm.voice.default_tag)
