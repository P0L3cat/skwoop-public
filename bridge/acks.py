"""Pre-rendered action acknowledgements: POST /ack {"kind":"light_up", "mood"?} -> "Okay, I turned it up!".

The library (acks.json, or ACK_LIBRARY) has one bucket of lines per (kind, mood), worded for that mood
(sleepy lines read cute-tired and yawny, alert lines are short). Each line is recorded `takes` times, each take with a
different delivery tag (an Eleven v4 audio tag) from the bucket, chosen from the line's text so adding a line
never reshuffles the others. render_acks.py writes one 24 kHz 16-bit mono WAV per recording into ACK_DIR
(default ~/.skwoop/acks) and records it in ACK_DIR/manifest.json:
  {"voice": "<provider:voice:model>", "clips": {
      "<kind>/<mood>/<text id>/<take>": {"text", "direction", "hash", "file", "duration_s"}}}

Moods are the approved Skwoop mood loops (skwoop-2d/loops) that can speak; "neutral" (the tone classifier's
name) is the idle loop. Picking: a random recording from the (kind, mood) bucket, never the line played last
from that bucket; if the bucket has no recordings, the kind's idle bucket, then its happy bucket; if none has
any, live TTS of a line from the first of those buckets that has lines. Both files are re-read when they
change, so a re-render needs no restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import zlib
from pathlib import Path

from providers import TTS_RATE, env, resample, wav_to_pcm

log = logging.getLogger("skwoop.acks")

DEFAULT_LIBRARY = Path(__file__).resolve().parent / "acks.json"
MOODS = ("idle", "happy", "sad", "sleep", "thinking", "alert")   # skwoop-2d loops a reply can be spoken in
ALIASES = {"neutral": "idle"}                                     # tone.py says neutral; the loop is idle
FALLBACK = ("idle", "happy")
# Moods that are no longer spoken (Alex, Oct 1: the sleepy voice came out breathy and has no real use). Their clips
# stay on disk and in the libraries, but a request for one (explicit, pod mood, kind default) plays the mapped mood
# instead: low battery / night -> the calm idle voice. ACK_RETIRED_MOODS="" brings sleep back; "sleep:idle" = default.
def _retired(spec: str) -> dict:
    out = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        old, _, new = item.partition(":")
        out[old.strip().lower()] = (new.strip().lower() or "idle")
    return out


RETIRED_MOODS = _retired(os.environ.get("ACK_RETIRED_MOODS", "sleep:idle"))
PCM_CACHE = 64                                                    # decoded clips kept in memory
# emotion frame per mood (xiaozhi names; the pod face reacts to happy and thinking today)
EMOTION = {"idle": "neutral", "happy": "happy", "sad": "sad", "sleep": "sleepy", "thinking": "thinking",
           "alert": "surprised"}


def library_path() -> Path:
    return Path(os.path.expanduser(env("ACK_LIBRARY") or str(DEFAULT_LIBRARY)))


def clip_dir() -> Path:
    return Path(os.path.expanduser(env("ACK_DIR") or "~/.skwoop/acks"))


def norm_mood(mood: str) -> str:
    m = ALIASES.get(str(mood).strip().lower(), str(mood).strip().lower())
    if m not in MOODS:
        raise ValueError(f"unknown mood {mood!r} (moods: {', '.join(MOODS)}, or neutral)")
    return m


def speak_mood(mood: str) -> str:
    """norm_mood + retired moods mapped to the one we speak instead ("sleep" -> "idle")."""
    m = norm_mood(mood)
    return RETIRED_MOODS.get(m, m)


def text_id(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def take_tag(text: str, take: int, tags: list[str]) -> str:
    """The tag for one take of a line: stable per text, a different tag per take."""
    return tags[(zlib.crc32(text.encode()) + take) % len(tags)] if tags else ""


def load_library(path: Path) -> dict[str, dict]:
    """{kind: {"default": mood, "moods": {mood: {"tags": [...], "takes": n, "lines": [...]}}}}; ValueError if bad."""
    data = json.loads(Path(path).read_text())
    tags0 = {norm_mood(m): list(t) for m, t in (data.get("tags") or {}).items()}
    takes0 = int(data.get("takes", 1))
    kinds = {}
    for kind, spec in (data.get("kinds") or {}).items():
        buckets = {}
        for m, b in (spec.get("moods") or {}).items():
            m = norm_mood(m)
            b = {"lines": b} if isinstance(b, list) else dict(b)
            lines = [str(t).strip() for t in b.get("lines", []) if str(t).strip()]
            tags = list(b.get("tags") or tags0.get(m) or [])
            takes = max(1, min(int(b.get("takes", takes0)), max(1, len(tags))))
            if not lines or len(set(lines)) != len(lines):
                raise ValueError(f"ack bucket {kind}/{m}: needs lines, with no duplicates")
            buckets[m] = {"tags": tags, "takes": takes, "lines": lines}
        default = norm_mood(spec.get("default", "idle"))
        if default not in buckets:
            raise ValueError(f"ack kind {kind!r}: no bucket for its default mood {default!r}")
        kinds[kind] = {"default": default, "moods": buckets}
    return kinds


def recordings(kinds: dict):
    """Every (kind, mood, text, take, tag) the library asks for."""
    for kind, spec in kinds.items():
        for mood, b in spec["moods"].items():
            for text in b["lines"]:
                for take in range(b["takes"]):
                    yield kind, mood, text, take, take_tag(text, take, b["tags"])


def clip_key(kind: str, mood: str, text: str, take: int) -> str:
    return f"{kind}/{mood}/{text_id(text)}/{take}"


def clip_hash(text: str, direction: str, voice: str) -> str:
    return hashlib.sha256(f"{voice}\n{direction}\n{text}".encode()).hexdigest()[:16]


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


class AckLibrary:
    def __init__(self, library: Path | str | None = None, clips: Path | str | None = None):
        self.library_path = Path(library) if library else library_path()
        self.dir = Path(clips) if clips else clip_dir()
        self.kinds: dict[str, dict] = {}
        self.manifest: dict[str, dict] = {}
        self.voice = ""                      # what the clips were spoken with (providers.voice_key)
        self.error = ""
        self._seen = None
        self._last: dict[tuple, str] = {}   # (kind, mood) -> line played last from that bucket
        self._pcm: dict[str, bytes] = {}

    def refresh(self) -> None:
        seen = (_mtime(self.library_path), _mtime(self.dir / "manifest.json"))
        if seen == self._seen:
            return
        self._seen, self._pcm = seen, {}
        try:
            self.kinds, self.error = load_library(self.library_path), ""
        except (OSError, ValueError, AttributeError, TypeError) as e:
            self.kinds, self.error = {}, f"{type(e).__name__}: {e}"
            log.warning("ack library %s unusable: %s", self.library_path, self.error)
        try:
            m = json.loads((self.dir / "manifest.json").read_text())
            self.manifest, self.voice = m.get("clips") or {}, m.get("voice") or ""
        except (OSError, ValueError, AttributeError):
            self.manifest, self.voice = {}, ""

    def clips(self, kind: str, mood: str) -> list[tuple[str, Path]]:
        """Rendered (text, file) for every take in the (kind, mood) bucket; empty if the bucket doesn't exist."""
        b = self.kinds[kind]["moods"].get(mood)
        out = []
        for text in (b["lines"] if b else []):
            for take in range(b["takes"]):
                entry = self.manifest.get(clip_key(kind, mood, text, take))
                if (entry and entry.get("text") == text and not entry.get("bad")
                        and (self.dir / entry["file"]).is_file()):
                    out.append((text, self.dir / entry["file"]))
        return out

    def resolve_mood(self, kind: str, mood: str | None = None, pod_mood: str | None = None) -> str:
        """Requested mood; else the pod's current mood if this kind has a bucket for it; else the kind's default.
        KeyError: unknown kind. ValueError: unknown mood."""
        self.refresh()
        spec = self.kinds[kind]
        if mood:
            return speak_mood(mood)
        pm = ALIASES.get(pod_mood, pod_mood) if pod_mood else None
        pm = RETIRED_MOODS.get(pm, pm)
        if pm and pm in spec["moods"]:
            return pm
        return RETIRED_MOODS.get(spec["default"], spec["default"])

    def _choose(self, kind: str, mood: str, items: list, text_of=lambda x: x):
        last = self._last.get((kind, mood))
        item = random.choice([x for x in items if text_of(x) != last] or items)
        self._last[(kind, mood)] = text_of(item)
        return item

    def pick(self, kind: str, mood: str | None = None, pod_mood: str | None = None) -> dict:
        """pcm=None means: say `text` with live TTS."""
        want = self.resolve_mood(kind, mood, pod_mood)
        order = list(dict.fromkeys((want, *FALLBACK)))
        for m in order:
            clips = self.clips(kind, m)
            while clips:
                text, path = self._choose(kind, m, clips, text_of=lambda c: c[0])
                try:
                    return {"kind": kind, "text": text, "requested": want, "mood": m, "emotion": EMOTION[m],
                            "pcm": self._load(path), "source": "clip", "file": path.name}
                except Exception as e:  # noqa: BLE001 - a bad file: try another recording
                    log.warning("ack clip %s unreadable (%s)", path.name, type(e).__name__)
                    clips = [c for c in clips if c[1] != path]
        buckets = self.kinds[kind]["moods"]
        spoken = [b for b in buckets if b not in RETIRED_MOODS] or list(buckets)
        m = next(x for x in (*order, *spoken) if x in buckets)
        return {"kind": kind, "text": self._choose(kind, m, buckets[m]["lines"]), "requested": want, "mood": m,
                "emotion": EMOTION[m], "pcm": None, "source": "tts", "file": None}

    def _load(self, path: Path) -> bytes:
        key = str(path)
        if key not in self._pcm:
            pcm, rate = wav_to_pcm(path.read_bytes())
            if len(self._pcm) >= PCM_CACHE:          # ~2,000 clips is ~150 MB: keep only recent ones in memory
                self._pcm.pop(next(iter(self._pcm)))
            self._pcm[key] = resample(pcm, rate, TTS_RATE)
        return self._pcm[key]

    def describe(self) -> dict:
        self.refresh()
        buckets = [(k, m) for k, s in self.kinds.items() for m in s["moods"]]
        d = {"kinds": len(self.kinds), "buckets": len(buckets),
             "lines": sum(len(self.kinds[k]["moods"][m]["lines"]) for k, m in buckets),
             "recordings": sum(1 for _ in recordings(self.kinds)),
             "clips": sum(len(self.clips(k, m)) for k, m in buckets)}
        d["missing"] = d["recordings"] - d["clips"]
        d["voice"] = self.voice or None
        if self.error:
            d["error"] = self.error
        return d
