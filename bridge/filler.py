"""Thinking filler: short pre-rendered SkwoopOS clips that cover a slow reply ("Hm, looking into this.", "Still on
it.", then "Got it." right before the answer).

Timeline of a slow turn (FillerTiming): nothing for the first ~1.8 s; then a "start" line; then every 3-5 s a
"still" line, with a dry "long" line mixed in after ~9 s; never the same clip twice in a row. The moment the first
reply audio is ready the clip playing is cut with a short fade and, if a filler was heard, one short "landing" line
plays: at most landing_max_s, about 0.9 s ("Got it.", "Found it."), since it delays the answer by its own length.
The landing line stops on cancel() like the other fillers, and barge-in cancels it like any other reply audio.
Instant replies (house scripts, quick intents, library clips) never start a filler.

Voice rule: no hums, moans or breaths. No line starts with "Mm"/"Mmhm"; "Hm" is written as a short word and every
take is checked (render_fillers.py) and dropped if its "hm"/"uh" runs long or sounds hummed or breathy.

On disk (FILLER_DIR, default ~/.skwoop/fillers): manifest.json + clips/<id>.wav (24 kHz mono 16-bit).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import dsp

log = logging.getLogger("skwoop.filler")

Stage = Literal["start", "still", "long", "landing"]
FillerMood = Literal["idle", "happy", "calm"]
STAGES: tuple[Stage, ...] = ("start", "still", "long", "landing")
FILLER_MOODS: tuple[FillerMood, ...] = ("idle", "happy", "calm")
RATE = 24000
DEFAULT_DIR = Path.home() / ".skwoop" / "fillers"


@dataclass(frozen=True)
class FillerLine:
    id: str
    stage: Stage
    mood: FillerMood
    text: str


def _lines(stage: Stage, mood: FillerMood, *texts: str) -> tuple[FillerLine, ...]:
    return tuple(FillerLine(f"{stage}.{mood}.{i}", stage, mood, t) for i, t in enumerate(texts, 1))


CATALOG: tuple[FillerLine, ...] = (
    *_lines("start", "idle", "Hm, looking into this.", "Uh, one sec.", "Okay, digging.", "One sec.",
            "Let me check.", "Hm. Checking.", "On it. One moment."),
    *_lines("still", "idle", "Still on it.", "Bear with me.", "Working on it.", "Almost there.", "Still digging.",
            "Hang on. Nearly."),
    *_lines("long", "idle", "Thinking very hard. Don't rush me.", "This one's a slow one.",
            "Still going. Big question.", "Good question. Annoyingly good.", "Any second now. Probably.",
            "I'm fast, I promise. Usually."),
    *_lines("landing", "idle", "Oh, here!", "Got it.", "Okay, here we go.", "Found it.", "Right, here it is."),
    *_lines("start", "happy", "Oh, let me look!", "One sec!", "Okay, digging in!", "Hm, good one. Checking!",
            "Let me find out!"),
    *_lines("still", "happy", "Still on it!", "Almost there!", "Working on it!", "Nearly got it!"),
    *_lines("long", "happy", "Thinking really hard over here!", "Big question. I love it.",
            "Still digging. Worth the wait, promise!"),
    *_lines("landing", "happy", "Oh, here!", "Got it!", "Here it is!", "Found it!"),
    *_lines("start", "calm", "Hm, let me look.", "One moment.", "Let me check on that.", "Okay, looking."),
    *_lines("still", "calm", "Still looking.", "Just a moment more.", "Nearly there."),
    *_lines("long", "calm", "Taking my time on this one.", "Slow and steady. Still on it."),
    *_lines("landing", "calm", "Here we go.", "Okay, got it.", "Here it is."),
)


def filler_mood(pod_mood: str | None) -> FillerMood:
    """The pod's current mood -> the filler variant: happy stays happy, sleepy/sad/calm go calm, the rest idle."""
    if pod_mood in ("happy", "laughing", "funny", "loving"):
        return "happy"

    if pod_mood in ("calm", "sleep", "sleepy", "sad"):
        return "calm"

    return "idle"


# ------------------------------------------------------------------ manifest (the only place that sees raw JSON)
class FillerManifestError(ValueError):
    """fillers/manifest.json is missing a field or has the wrong type."""


@dataclass(frozen=True)
class FillerClip:
    id: str
    stage: Stage
    mood: FillerMood
    text: str
    file: str
    seconds: float


def _field_str(raw: dict[str, object], key: str, where: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v:
        raise FillerManifestError(f"{where}.{key}: expected a non-empty string")

    return v


def _stage(v: str, where: str) -> Stage:
    for s in STAGES:
        if s == v:
            return s

    raise FillerManifestError(f"{where}.stage: {v!r} is not one of {STAGES}")


def _mood(v: str, where: str) -> FillerMood:
    for m in FILLER_MOODS:
        if m == v:
            return m

    raise FillerManifestError(f"{where}.mood: {v!r} is not one of {FILLER_MOODS}")


def parse_filler_manifest(raw: object) -> tuple[FillerClip, ...]:
    if not isinstance(raw, dict) or not isinstance(raw.get("clips"), list):
        raise FillerManifestError("manifest: expected {\"clips\": [...]}")

    out: list[FillerClip] = []
    for i, item in enumerate(raw["clips"]):
        where = f"clips[{i}]"
        if not isinstance(item, dict):
            raise FillerManifestError(f"{where}: expected an object")

        secs = item.get("seconds")
        if isinstance(secs, bool) or not isinstance(secs, (int, float)) or secs <= 0:
            raise FillerManifestError(f"{where}.seconds: expected a positive number")

        out.append(FillerClip(id=_field_str(item, "id", where), stage=_stage(_field_str(item, "stage", where), where),
                              mood=_mood(_field_str(item, "mood", where), where),
                              text=_field_str(item, "text", where), file=_field_str(item, "file", where),
                              seconds=float(secs)))

    return tuple(out)


def read_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise FillerManifestError(f"{path.name}: expected {RATE} Hz mono 16-bit")

        frames: bytes = w.readframes(w.getnframes())

        return frames


# ------------------------------------------------------------------ the set
class FillerSet:
    def __init__(self, clips: tuple[FillerClip, ...], root: Path, rng: random.Random | None = None):
        self.clips = clips
        self.root = root
        self.rng = rng or random.Random()
        self._pcm: dict[str, bytes] = {}

    @classmethod
    def load(cls, root: Path | None = None) -> FillerSet:
        """An empty set (no fillers) when the directory or manifest is missing or broken: the bridge still works."""
        root = (root or Path(os.environ.get("FILLER_DIR") or DEFAULT_DIR)).expanduser()
        try:
            clips = parse_filler_manifest(json.loads((root / "manifest.json").read_text()))
        except (OSError, ValueError) as e:
            log.warning("thinking fillers not loaded from %s (%s): slow replies stay silent", root, type(e).__name__)

            return cls((), root)

        log.info("thinking fillers: %d clips from %s", len(clips), root)

        return cls(clips, root)

    @property
    def ready(self) -> bool:
        return bool(self.clips)

    def pick(self, stage: Stage, mood: FillerMood, avoid: str | None = None) -> FillerClip | None:
        """A clip for this stage in this mood (idle as the fallback mood), never `avoid` when there is a choice."""
        for m in dict.fromkeys((mood, "idle")):
            pool = [c for c in self.clips if c.stage == stage and c.mood == m]
            fresh = [c for c in pool if c.id != avoid] or pool
            if fresh:
                return self.rng.choice(fresh)

        return None

    def pick_landing(self, mood: FillerMood, max_s: float, avoid: str | None = None) -> FillerClip | None:
        """A landing clip no longer than max_s in this mood (idle as the fallback), else the mood's shortest."""
        for m in dict.fromkeys((mood, "idle")):
            pool = [c for c in self.clips if c.stage == "landing" and c.mood == m]
            if not pool:
                continue

            short = [c for c in pool if c.seconds <= max_s]
            if not short:
                return min(pool, key=lambda c: c.seconds)

            fresh = [c for c in short if c.id != avoid] or short

            return self.rng.choice(fresh)

        return None

    def pcm(self, clip: FillerClip) -> bytes:
        hit = self._pcm.get(clip.id)
        if hit is None:
            hit = self._pcm[clip.id] = read_wav_pcm(self.root / clip.file)

        return hit


def fade_out(chunk: bytes) -> bytes:
    """A 16-bit PCM chunk ramped down to silence: the clean end of a filler cut short."""
    return dsp.fade_out(chunk)


# ------------------------------------------------------------------ the player
@dataclass(frozen=True)
class FillerTiming:
    first_s: float = 1.8        # a reply ready before this never hears a filler (fast-tier answers land at 1.4-1.7 s)
    gap_min_s: float = 3.0      # silence between filler lines
    gap_max_s: float = 5.0
    long_after_s: float = 9.0   # dry "long" lines only after this, and never two in a row
    landing_after_s: float = 2.5  # a landing line only once the fillers have held the floor this long;
                                  # a shorter wait fades into the answer (a 1 s "Got it." would cost more than it hid)
    landing_max_s: float = 0.9    # the longest landing clip used ("Okay, here we go." 1.9 s is skipped); none that
                                  # short in the mood: the shortest one


def timing_from_env() -> FillerTiming:
    d = FillerTiming()

    return FillerTiming(first_s=float(os.environ.get("FILLER_FIRST_S") or d.first_s),
                        gap_min_s=float(os.environ.get("FILLER_GAP_MIN_S") or d.gap_min_s),
                        gap_max_s=float(os.environ.get("FILLER_GAP_MAX_S") or d.gap_max_s),
                        long_after_s=float(os.environ.get("FILLER_LONG_AFTER_S") or d.long_after_s),
                        landing_after_s=float(os.environ.get("FILLER_LANDING_AFTER_S") or d.landing_after_s),
                        landing_max_s=float(os.environ.get("FILLER_LANDING_MAX_S") or d.landing_max_s))


def next_stage(played: list[FillerClip], elapsed_s: float, timing: FillerTiming) -> Stage:
    if not played:
        return "start"

    if elapsed_s >= timing.long_after_s and played[-1].stage != "long":
        return "long"

    return "still"


class FillerOut(Protocol):
    """Where filler audio goes (the pod session; a recorder in tests)."""

    async def filler_begin(self, clip: FillerClip) -> None: ...

    async def filler_audio(self, pcm: bytes, stop: asyncio.Event) -> bool:
        """Play pcm at real time; when `stop` is set, fade out within a frame and return True (cut short)."""
        ...


@dataclass
class FillerPlayer:
    fillers: FillerSet
    out: FillerOut
    mood: FillerMood
    t0: float
    timing: FillerTiming = field(default_factory=FillerTiming)
    clock: Callable[[], float] = time.monotonic
    played: list[FillerClip] = field(default_factory=list)
    first_at: float | None = None       # clock() when the first filler started
    cut: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    landing_stop: asyncio.Event = field(default_factory=asyncio.Event)    # cancel() cuts the landing line too
    task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.fillers.ready:
            self.task = asyncio.create_task(self._run())

    async def _pause(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), max(0.0, seconds))
        except TimeoutError:
            pass

    async def _run(self) -> None:
        await self._pause(self.t0 + self.timing.first_s - self.clock())
        while not self.stop.is_set():
            stage = next_stage(self.played, self.clock() - self.t0, self.timing)
            clip = self.fillers.pick(stage, self.mood, avoid=self.played[-1].id if self.played else None)
            if clip is None:
                return

            if self.first_at is None:
                self.first_at = self.clock()

            await self.out.filler_begin(clip)
            self.played.append(clip)
            self.cut = await self.out.filler_audio(self.fillers.pcm(clip), self.stop)
            gap = self.fillers.rng.uniform(self.timing.gap_min_s, self.timing.gap_max_s)
            await self._pause(gap)

    async def finish(self) -> FillerClip | None:
        """The reply is ready: cut the filler (short fade) and, after a long enough wait, say a landing line."""
        self.stop.set()
        if self.task is not None:
            await self.task

        if not self.played or self.first_at is None:
            return None

        if self.clock() - self.first_at < self.timing.landing_after_s:
            return None

        clip = self.fillers.pick_landing(self.mood, self.timing.landing_max_s, avoid=self.played[-1].id)
        if clip is None or self.landing_stop.is_set():
            return None

        await self.out.filler_begin(clip)
        self.played.append(clip)
        await self.out.filler_audio(self.fillers.pcm(clip), self.landing_stop)

        return clip

    def cancel(self) -> None:
        self.stop.set()
        self.landing_stop.set()
        if self.task is not None:
            self.task.cancel()
