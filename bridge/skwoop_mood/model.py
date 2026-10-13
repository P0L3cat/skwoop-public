"""Weights file parsing and inference without numpy: a sparse linear model (the dot products run in dsp.c).

pad = clip(b + x_text . W, -1, 1) with x_text the hashed text features (features.py). The bridge loads only the
format 4 (v1.4) file: it carries its own 6 x 3 prototype table (same mood order as mapping.PROTOTYPES) and a calm
gate, a linear score over the same text features. When the nearest prototype is one of the gate's source moods
(calm and the low-key moods next to it), mood() returns calm if the score is positive; a calm-nearest point the
gate rejects falls back to the nearest non-calm prototype. The context head stored in the file ("h") is not
read: the bridge never passes context, and without context the head adds exactly zero.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple

from dsp import sparse_dot
from skwoop_mood import npz
from skwoop_mood.features import FeatureConfig, featurize
from skwoop_mood.mapping import PROTOTYPES, MoodName, Prototype, mood_tag, nearest_mood

WEIGHTS_FORMAT_CALM_GATE: Final = 4
_KEYS: Final = frozenset({"format", "hash_bits", "use_chars", "use_bigrams", "char_min", "char_max", "w", "b",
                          "head_bits", "head_cross_words", "head_cross_chars", "h", "prototypes", "calm_w", "calm_b",
                          "calm_from"})


class Pad(NamedTuple):
    v: float
    a: float
    d: float


class MoodResult(NamedTuple):
    name: MoodName
    tag: str
    v: float
    a: float
    d: float


@dataclass(frozen=True)
class CalmGate:
    w: array[float]  # (n_buckets,) float32: calm score per text feature
    b: float
    from_moods: frozenset[MoodName]  # nearest moods the gate may turn into calm; always includes calm

    def score(self, idx: list[int], vals: list[float]) -> float:
        return self.b + sparse_dot(self.w, 1, 0, array("q", idx), array("d", vals))


@dataclass(frozen=True)
class MoodWeights:
    cfg: FeatureConfig
    w: array[float]  # (n_buckets * 3,) float32, row-major: V, A, D per bucket
    b: tuple[float, float, float]
    prototypes: tuple[Prototype, ...]
    calm_gate: CalmGate


def _finite(a: array[float], name: str) -> array[float]:
    if not all(math.isfinite(x) for x in a):
        raise ValueError(f"weights: {name} has non-finite values")
    return a


def _floats(z: dict[str, npz.Npy], name: str, shape: tuple[int, ...]) -> array[float]:
    arr = z[name]
    data = arr.data
    if not npz.is_f32(data) or arr.shape != shape:
        raise ValueError(f"weights: {name} must be float32 {shape}, got {arr.shape}")
    return _finite(data, name)


def _int(z: dict[str, npz.Npy], name: str) -> int:
    v = z[name].scalar()
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"weights: {name} must be an integer scalar")
    return v


def _bool(z: dict[str, npz.Npy], name: str) -> bool:
    v = z[name].scalar()
    if not isinstance(v, bool):
        raise ValueError(f"weights: {name} must be a bool scalar")
    return v


def load_weights(path: Path) -> MoodWeights:
    """Parse a format 4 .npz; reject anything with the wrong keys, shapes or dtypes."""
    z = npz.load(path)
    if frozenset(z) != _KEYS:
        raise ValueError(f"weights: keys {sorted(z)} != {sorted(_KEYS)}")
    if _int(z, "format") != WEIGHTS_FORMAT_CALM_GATE:
        raise ValueError(f"weights: format {_int(z, 'format')}, expected 4")

    cfg = FeatureConfig(hash_bits=_int(z, "hash_bits"), char_min=_int(z, "char_min"), char_max=_int(z, "char_max"),
                        use_chars=_bool(z, "use_chars"), use_bigrams=_bool(z, "use_bigrams"))
    if not 4 <= cfg.hash_bits <= 22:
        raise ValueError(f"weights: hash_bits {cfg.hash_bits} out of range")

    table = _floats(z, "prototypes", (len(PROTOTYPES), 3))
    if max(abs(x) for x in table) > 1.0:
        raise ValueError("weights: prototypes must lie in [-1, 1]")
    # Stored as float32; rounding to 1e-6 gives back the decimals that were written (0.15, not 0.15000000596).
    protos = tuple(Prototype(p.name, round(table[3 * i], 6), round(table[3 * i + 1], 6), round(table[3 * i + 2], 6))
                   for i, p in enumerate(PROTOTYPES))

    src = z["calm_from"]
    if src.shape != (len(PROTOTYPES),) or not all(isinstance(x, bool) for x in src.data):
        raise ValueError(f"weights: calm_from must be bool ({len(PROTOTYPES)},)")
    from_moods = frozenset(p.name for p, on in zip(PROTOTYPES, src.data, strict=True) if on)
    if "calm" not in from_moods:
        raise ValueError("weights: calm_from must include calm")
    calm_b = _floats(z, "calm_b", ())
    gate = CalmGate(_floats(z, "calm_w", (cfg.n_buckets,)), calm_b[0], from_moods)

    b = _floats(z, "b", (3,))
    return MoodWeights(cfg, _floats(z, "w", (cfg.n_buckets, 3)), (b[0], b[1], b[2]), protos, gate)


class MoodModel:
    def __init__(self, weights: MoodWeights) -> None:
        self._weights = weights

    @property
    def weights(self) -> MoodWeights:
        return self._weights

    def predict_raw(self, text: str) -> tuple[float, float, float]:
        """Unclipped (V, A, D)."""
        wt = self._weights
        idx, vals = featurize(text, wt.cfg)
        ia, va = array("q", idx), array("d", vals)
        v, a, d = (b + sparse_dot(wt.w, 3, col, ia, va) for col, b in enumerate(wt.b))
        return v, a, d

    def predict(self, text: str) -> Pad:
        v, a, d = (min(1.0, max(-1.0, x)) for x in self.predict_raw(text))
        return Pad(v, a, d)

    def mood(self, text: str) -> MoodResult:
        p = self.predict(text)
        name = self._table_mood(text, p.v, p.a, p.d)
        return MoodResult(name, mood_tag(name), p.v, p.a, p.d)

    def _table_mood(self, text: str, v: float, a: float, d: float) -> MoodName:
        wt = self._weights
        name = nearest_mood(wt.prototypes, v, a, d)
        gate = wt.calm_gate
        if name not in gate.from_moods:
            return name

        idx, vals = featurize(text, wt.cfg)
        if gate.score(idx, vals) > 0.0:
            return "calm"

        if name != "calm":
            return name

        return nearest_mood(tuple(q for q in wt.prototypes if q.name != "calm"), v, a, d)
