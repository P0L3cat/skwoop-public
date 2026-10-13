"""Text -> sparse hashed features (word 1-2 grams with negation scope, char 3-5 grams, punctuation cues).

Hashing is crc32 (stable across runs and machines, unlike Python's salted hash()). The output is a list of
(bucket, value) pairs, L2-normalised, with repeated buckets summed.
"""

from __future__ import annotations

import itertools
import math
import re
import zlib
from dataclasses import dataclass
from typing import Final

NEGATORS: Final[frozenset[str]] = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "zero",
        "nothing",
        "none",
        "nobody",
        "neither",
        "nor",
        "cannot",
    }
)
_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|[!?.,;:]")
_CLAUSE_END: Final[frozenset[str]] = frozenset({".", ",", ";", ":", "!", "?"})
_DIGITS = re.compile(r"[0-9]")


@dataclass(frozen=True)
class FeatureConfig:
    hash_bits: int
    char_min: int = 3
    char_max: int = 5
    use_chars: bool = True
    use_bigrams: bool = True

    @property
    def n_buckets(self) -> int:
        return 1 << self.hash_bits


def tokenize(text: str) -> list[str]:
    low = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    low = _DIGITS.sub("0", low)

    return _TOKEN.findall(low)


def _is_negator(tok: str) -> bool:
    return tok in NEGATORS or tok.endswith("n't")


def mark_negation(tokens: list[str]) -> list[str]:
    """Prefix 'NEG_' to word tokens after a negator, up to the next clause punctuation."""
    out: list[str] = []
    negating = False
    for tok in tokens:
        if tok in _CLAUSE_END:
            negating = False
            out.append(tok)
            continue

        out.append(f"NEG_{tok}" if negating else tok)
        if _is_negator(tok):
            negating = True

    return out


def _bucket(key: str, n_buckets: int) -> int:
    return zlib.crc32(key.encode("utf-8")) & (n_buckets - 1)


def raw_feature_keys(text: str, cfg: FeatureConfig) -> list[str]:
    tokens = tokenize(text)
    marked = mark_negation(tokens)
    words = [t for t in tokens if t not in _CLAUSE_END]
    keys: list[str] = ["b:bias"]
    keys.extend(f"w:{t}" for t in marked)
    if cfg.use_bigrams:
        seq = ["<s>", *marked, "</s>"]
        keys.extend(f"b:{x} {y}" for x, y in itertools.pairwise(seq))

    if cfg.use_chars:
        for w in words:
            padded = f"<{w}>"
            for n in range(cfg.char_min, cfg.char_max + 1):
                keys.extend(f"c:{padded[i : i + n]}" for i in range(len(padded) - n + 1))

    if "!" in text:
        keys.append("p:exclaim")

    if "?" in text:
        keys.append("p:question")

    if any(len(w) > 1 and w.isupper() and w.isalpha() for w in text.split()):
        keys.append("p:allcaps")

    keys.append(f"len:{min(len(words), 40) // 5}")

    return keys


def featurize(text: str, cfg: FeatureConfig) -> tuple[list[int], list[float]]:
    """Sparse feature vector as parallel (indices, values), sublinear counts, L2-normalised."""
    counts: dict[int, float] = {}
    for key in raw_feature_keys(text, cfg):
        b = _bucket(key, cfg.n_buckets)
        counts[b] = counts.get(b, 0.0) + 1.0

    idx = sorted(counts)
    vals = [1.0 + math.log(counts[i]) for i in idx]
    norm = math.sqrt(sum(x * x for x in vals))

    return idx, [x / norm for x in vals]
