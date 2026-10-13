"""Synthetic clip audio for tests. The repo keeps align JSON only; wavs are generated here."""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import wave
from array import array
from pathlib import Path

_RATE = 24000
_CACHE: dict[str, Path] = {}


def _write_wav(path: Path, samples: list[float]) -> None:
    pcm = array("h", (int(max(-1.0, min(1.0, v)) * 32767) for v in samples))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_RATE)
        handle.writeframes(pcm.tobytes())


def _sine(n: int, amp: float, period: int) -> list[float]:
    # One pitch period across the whole clip so an overlap xcorr can phase-align.
    step = 2 * math.pi / period if period else 2 * math.pi / 114
    return [amp * math.sin(step * i) for i in range(n)]


def _sample_count(align: Path) -> int:
    raw = json.loads(align.read_text())
    analysis = raw.get("analysis")
    if isinstance(analysis, dict) and isinstance(analysis.get("n"), int):
        return analysis["n"]

    return _RATE


def library_root() -> Path:
    """A temp library: fixture manifest and align JSON, plus a sine wav per clip."""
    cached = _CACHE.get("lib")
    if cached is not None:
        return cached

    src = Path(__file__).resolve().parent / "fixtures" / "sample-lib"
    dest = Path(tempfile.mkdtemp(prefix="sample-lib-"))
    shutil.copy2(src / "manifest.json", dest / "manifest.json")
    clips = dest / "clips"
    clips.mkdir()
    for align in sorted((src / "clips").glob("*.align.json")):
        shutil.copy2(align, clips / align.name)
        stem = align.name[: -len(".align.json")]
        _write_wav(clips / f"{stem}.wav", _sine(_sample_count(align), 0.2, 114))

    manifest = json.loads((dest / "manifest.json").read_text())
    listed = manifest.get("clips")
    if isinstance(listed, dict):
        for clip in listed.values():
            if not isinstance(clip, dict):
                continue
            rel = clip.get("file")
            if isinstance(rel, str) and not (dest / rel).is_file():
                _write_wav(dest / rel, _sine(_RATE, 0.2, 114))

    _CACHE["lib"] = dest
    return dest


def overlap_root() -> Path:
    """Temp copy of the overlap-pair sidecars plus phase-locked sines (B quieter, same period)."""
    cached = _CACHE.get("ov")
    if cached is not None:
        return cached

    src = Path(__file__).resolve().parent / "fixtures" / "overlap-pair"
    dest = Path(tempfile.mkdtemp(prefix="skwoop-overlap-"))
    for name, amp in (("a", 0.25), ("b", 0.175)):
        align = src / f"{name}.align.json"
        shutil.copy2(align, dest / align.name)
        _write_wav(dest / f"{name}.wav", _sine(_sample_count(align), amp, 114))

    _CACHE["ov"] = dest
    return dest
