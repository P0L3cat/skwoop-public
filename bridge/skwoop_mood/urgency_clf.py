"""Tiny urgency classifier: hashed word uni/bigrams and character 4/5-grams into logistic regression.
Plain Python (no numpy).

urgency_probability(text) -> float in [0, 1]. skwoop_mood.urgency.urgency() combines it with the rules:
a floor rule fires on its own, any other rule needs p >= threshold_weak, and p >= threshold fires alone.
Weights: weights/urgency_clf_v024.npz (train_urgency_v024.py), which also store the floor rule names and both
thresholds. v0.2.3 was urgency_clf_v023.npz, v0.2.2 urgency_clf_v022.npz, v0.2.1 urgency_clf_v021.npz,
v1.4 round 2 urgency_clf_v14r2.npz. Round 1 (weights/urgency_clf_v14.npz, train_urgency_v14.py) used every rule
as floor.
"""

from __future__ import annotations

import math
import re
import zlib
from array import array
from dataclasses import dataclass
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Final

from dsp import sparse_dot
from skwoop_mood import npz

CLF_WEIGHTS: Final = Path(__file__).resolve().parent.parent / "weights" / "urgency_clf_v024.npz"
N_BUCKETS: Final = 1 << 17
CHAR_SIZES: Final = (4, 5)
CHAR_WEIGHT: Final = 0.3  # character n-grams help with typos but generalise worse than words (dev)

_QUOTES: Final = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u2032": "'"})
_APOSTROPHE: Final = re.compile(r"'")
_NON_WORD: Final = re.compile(r"[^a-z0-9!?]+")


@dataclass(frozen=True)
class ClfWeights:
    w: array[float]  # (N_BUCKETS,) float32
    bias: float
    threshold: float  # p at or above this fires with no rule
    threshold_weak: float  # a non-floor rule fires only when p is at or above this
    floor_rules: frozenset[str]  # rule names that fire on their own
    version: str


def normalise(text: str) -> str:
    """Lower case, straight quotes, apostrophes dropped ("can't" == "cant"), other punctuation except ! ? removed."""
    t = _APOSTROPHE.sub("", text.translate(_QUOTES).lower())

    return " ".join(_NON_WORD.sub(" ", t).split())


# crc32(gram, crc32(b"k|")) == crc32(b"k|" + gram): the kind prefix is hashed once.
_PREFIX_CRC: Final = {kind: zlib.crc32(f"{kind}|".encode()) for kind in ("w", "b", "c")}


def features(text: str) -> tuple[list[int], list[float]]:
    """Hashed bucket ids (sorted) and values: log(1 + weighted count), L2-normalised. Empty text gives empty lists.

    Grams: words, word bigrams and character 4/5-grams of " text " (ASCII after normalise()). Each gram goes to
    bucket crc32("kind|gram") mod N_BUCKETS; character grams count CHAR_WEIGHT, word grams 1.
    """
    t = normalise(text)
    words = t.encode().split()
    padded = f" {t} ".encode()
    crc = zlib.crc32
    word_hashes = [crc(w, _PREFIX_CRC["w"]) for w in words]
    word_hashes += [crc(a + b" " + b, _PREFIX_CRC["b"]) for a, b in pairwise(words)]
    pc = _PREFIX_CRC["c"]
    char_hashes = [crc(padded[i : i + n], pc) for n in CHAR_SIZES for i in range(len(padded) - n + 1)]

    mask = N_BUCKETS - 1
    counts: dict[int, float] = {}
    for h in word_hashes:
        counts[h & mask] = counts.get(h & mask, 0.0) + 1.0
    for h in char_hashes:
        counts[h & mask] = counts.get(h & mask, 0.0) + CHAR_WEIGHT
    idx = sorted(counts)
    val = [math.log1p(counts[i]) for i in idx]
    norm = math.sqrt(sum(v * v for v in val))

    return idx, [v / norm for v in val] if norm > 0 else val


def load_clf(path: Path = CLF_WEIGHTS) -> ClfWeights:
    z = npz.load(path)
    w = z["w"]
    weights = w.data
    if not npz.is_f32(weights) or w.shape != (N_BUCKETS,):
        raise ValueError(f"{path}: w has shape {w.shape}, expected float32 ({N_BUCKETS},)")

    return ClfWeights(
        weights,
        float(z["bias"].scalar()),
        float(z["threshold"].scalar()),
        float(z["threshold_weak"].scalar()),
        frozenset(str(name) for name in z["floor_rules"].data),
        str(z["version"].scalar()),
    )


@cache
def default_clf() -> ClfWeights:
    return load_clf()


def urgency_probability(text: str | None, clf: ClfWeights | None = None) -> float:
    """Classifier probability that the line is urgent. None or blank text gives 0.0."""
    if not isinstance(text, str) or not text.strip():
        return 0.0

    weights = clf if clf is not None else default_clf()
    idx, val = features(text)
    z = sparse_dot(weights.w, 1, 0, array("q", idx), array("d", val)) + weights.bias

    return 1.0 / (1.0 + math.exp(-z)) if z > -700.0 else 0.0   # exp overflows past ~709
