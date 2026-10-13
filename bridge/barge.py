"""Barge-in: is Alex talking over Skwoop?

Per mic frame: the mic level against the noise floor and against what Skwoop is playing (the reference). The echo
path (mic dB minus reference dB while only Skwoop is heard) is learned during the first WARMUP_MS of each reply
and on every frame that isn't voice; a voice frame must beat it by ECHO_DB. VOICE_MS of voice (gaps cost half)
fires. Same rules as webpod/static/app.js BargeIn, so the web pod and the bridge behave alike.
"""
from __future__ import annotations

import math
from collections import deque

from dsp import rms16, samples

SILENT_DB = -55.0      # reference below this: Skwoop is not making sound right now
FLOOR_DB = 12.0        # voice is at least this far above the room's noise floor
ECHO_DB = 10.0         # ... and this far above the learned echo of Skwoop's own voice
VOICE_MS = 240.0
WARMUP_MS = 300.0      # per reply: learn the echo path before anything may fire
REF_WINDOW_S = 0.8     # reference = loudest output in this window (device buffer + speaker delay)


def rms_db(pcm: bytes) -> float:
    a = samples(pcm)
    if not a:
        return -120.0
    return 20 * math.log10(rms16(a) / 32768.0 + 1e-9)


class Reference:
    """Levels of the audio sent to the pod, by send time."""

    def __init__(self, window_s: float = REF_WINDOW_S) -> None:
        self.window_s = window_s
        self.levels: deque[tuple[float, float]] = deque()

    def sent(self, t: float, pcm: bytes) -> None:
        self.levels.append((t, rms_db(pcm)))
        while self.levels and self.levels[0][0] < t - 5.0:
            self.levels.popleft()

    def db(self, t: float) -> float:
        return max((d for at, d in self.levels if t - self.window_s <= at <= t), default=-120.0)

    def active(self, t: float) -> bool:
        return bool(self.levels) and self.levels[-1][0] >= t - self.window_s


class BargeInDetector:
    def __init__(self) -> None:
        self.floor = -60.0
        self.reset()

    def reset(self) -> None:
        """A reply starts: re-learn the echo path."""
        self.echo = -20.0
        self.elapsed_ms = 0.0
        self.acc_ms = 0.0

    def push(self, mic_db: float, ref_db: float, frame_ms: float) -> bool:
        self.elapsed_ms += frame_ms
        playing = ref_db > SILENT_DB
        rel = mic_db - ref_db
        if self.elapsed_ms <= WARMUP_MS:
            if playing:
                self.echo = rel if self.elapsed_ms <= frame_ms else max(self.echo, 0.7 * self.echo + 0.3 * rel)
            return False
        voice = mic_db > self.floor + FLOOR_DB and (not playing or rel > self.echo + ECHO_DB)
        if not voice:
            if playing:
                self.echo += 0.05 * (rel - self.echo)
            else:
                self.floor = mic_db if mic_db < self.floor else self.floor + 0.02 * (mic_db - self.floor)
        self.acc_ms = self.acc_ms + frame_ms if voice else max(0.0, self.acc_ms - frame_ms / 2)
        if self.acc_ms >= VOICE_MS:
            self.acc_ms = 0.0
            return True
        return False
