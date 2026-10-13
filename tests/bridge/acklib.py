"""Writes a fake ack/stitch clip library for tests: one chirp WAV per recording plus manifest.json, in the layout
acks.AckLibrary reads. Free and offline; the real libraries were rendered with ElevenLabs."""
import json
import math
import struct
from pathlib import Path

import acks
from providers import TTS_RATE, pcm_to_wav

VOICE = "fake"


def fake_pcm(text: str) -> bytes:
    """A soft two-note chirp, about as long as the line would be spoken."""
    n = int(TTS_RATE * max(0.4, 0.06 * len(text)))
    return b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * (523 if i < n // 2 else 659) * i / TTS_RATE)))
                    for i in range(n))


def render(library: Path, out: Path) -> int:
    """Every recording the library asks for -> out/<kind>.<mood>.<text id>.<take>.wav. Returns the count."""
    out.mkdir(parents=True, exist_ok=True)
    clips = {}
    for kind, mood, text, take, tag in acks.recordings(acks.load_library(library)):
        name = f"{kind}.{mood}.{acks.text_id(text)}.{take}.wav"
        pcm = fake_pcm(text)
        (out / name).write_bytes(pcm_to_wav(pcm, TTS_RATE))
        clips[acks.clip_key(kind, mood, text, take)] = {
            "text": text, "direction": tag, "hash": acks.clip_hash(text, tag, VOICE), "file": name,
            "duration_s": round(len(pcm) / 2 / TTS_RATE, 2), "level": None}
    (out / "manifest.json").write_text(json.dumps({"voice": VOICE, "rate": TTS_RATE, "clips": clips}, indent=1))
    return len(clips)
