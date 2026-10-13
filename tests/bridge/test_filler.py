"""Thinking filler: the catalogue's voice rules, the manifest boundary, picking, and the player's timeline."""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import wave
from itertools import pairwise
from pathlib import Path

from array import array
import pytest

import filler
from filler import CATALOG, FillerClip, FillerPlayer, FillerSet, FillerTiming


# ------------------------------------------------------------------ the catalogue
def test_no_hums_moans_or_breaths_in_the_text():
    for line in CATALOG:
        first = line.text.split()[0].strip(",.!?").lower()
        assert not re.match(r"^m+(h*m+)*$", first), line          # "Mm", "Mmhm", "Mmm"
        assert not re.search(r"\bh+m{2,}\b|\bhm{3,}", line.text.lower()), line   # long "hmmm"
        assert not re.search(r"\b(sigh|ah+h|oo+h|uh{3,}|um+)\b", line.text.lower()), line
        assert "…" not in line.text and "..." not in line.text, line             # no trailing-off delivery


def test_every_stage_and_mood_can_avoid_a_repeat():
    for stage in filler.STAGES:
        for mood in filler.FILLER_MOODS:
            assert len([c for c in CATALOG if c.stage == stage and c.mood == mood]) >= 2, (stage, mood)

    assert len({c.id for c in CATALOG}) == len(CATALOG)


def test_pod_mood_picks_the_variant():
    assert [filler.filler_mood(m) for m in ("happy", "sleep", "sad", "alert", None)] == [
        "happy", "calm", "calm", "idle", "idle"]


# ------------------------------------------------------------------ the manifest boundary
def _write_set(root: Path, lines: list[tuple[str, filler.Stage, filler.FillerMood, str]], seconds: float = 0.3) -> None:
    (root / "clips").mkdir(parents=True)
    clips = []
    for cid, stage, mood, text in lines:
        with wave.open(str(root / "clips" / f"{cid}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(filler.RATE)
            w.writeframes(array("h", [3000] * int(filler.RATE * seconds)).tobytes())

        clips.append({"id": cid, "stage": stage, "mood": mood, "text": text, "file": f"clips/{cid}.wav",
                      "seconds": seconds})

    (root / "manifest.json").write_text(json.dumps({"clips": clips}))


SMALL = [("s1", "start", "idle", "Hm, looking into this."), ("s2", "start", "idle", "One sec."),
         ("t1", "still", "idle", "Still on it."), ("t2", "still", "idle", "Bear with me."),
         ("l1", "long", "idle", "Thinking very hard. Don't rush me."), ("l2", "long", "idle", "This one's a slow one."),
         ("g1", "landing", "idle", "Got it."), ("g2", "landing", "idle", "Oh, here!"),
         ("h1", "start", "happy", "One sec!")]


def test_manifest_is_parsed_at_the_boundary(tmp_path):
    _write_set(tmp_path, SMALL)
    fs = FillerSet.load(tmp_path)
    assert fs.ready and len(fs.clips) == len(SMALL) and fs.clips[0] == FillerClip(
        "s1", "start", "idle", "Hm, looking into this.", "clips/s1.wav", 0.3)
    assert len(fs.pcm(fs.clips[0])) == int(filler.RATE * 0.3) * 2
    for bad in ({}, {"clips": [{"id": "x"}]}, {"clips": [{"id": "x", "stage": "nope", "mood": "idle", "text": "t",
                                                          "file": "f", "seconds": 1}]},
                {"clips": [{"id": "x", "stage": "start", "mood": "idle", "text": "t", "file": "f", "seconds": True}]}):
        with pytest.raises(filler.FillerManifestError):
            filler.parse_filler_manifest(bad)


def test_missing_set_means_no_fillers(tmp_path):
    assert not FillerSet.load(tmp_path / "nothing-here").ready


def test_pick_prefers_the_mood_and_never_repeats(tmp_path):
    _write_set(tmp_path, SMALL)
    fs = FillerSet.load(tmp_path)
    fs.rng = random.Random(3)
    assert fs.pick("start", "happy").id == "h1"
    assert fs.pick("still", "happy").mood == "idle"                  # no happy "still" lines: idle stands in
    assert {fs.pick("start", "idle", avoid="s1").id for _ in range(20)} == {"s2"}


def test_fade_out_ends_silent():
    faded = array("h", filler.fade_out(array("h", [10000] * 1440).tobytes()))
    assert faded[0] == 10000 and faded[-1] == 0 and all(b <= a for a, b in zip(faded, faded[1:], strict=False))
    odd = array("h", filler.fade_out(array("h", [-32768, 32767, 12345, -7, 1]).tobytes()))
    assert list(odd) == [-32768, 24575, 6172, -1, 0]           # float32 ramp, truncated: numpy's numbers


def test_stages_escalate():
    t = FillerTiming(long_after_s=9)
    clip = {s: FillerClip(s, s, "idle", s, s, 1) for s in filler.STAGES}
    assert filler.next_stage([], 1.8, t) == "start"
    assert filler.next_stage([clip["start"]], 4, t) == "still"
    assert filler.next_stage([clip["start"], clip["still"]], 9.5, t) == "long"
    assert filler.next_stage([clip["still"], clip["long"]], 13, t) == "still"   # wit only every other line


# ------------------------------------------------------------------ the player
class RecordingOut:
    """FillerOut that plays at 10x speed and records what was said and when."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, float]] = []
        self.t0 = time.monotonic()

    async def filler_begin(self, clip: FillerClip) -> None:
        self.events.append(("begin", clip.id, time.monotonic() - self.t0))

    async def filler_audio(self, pcm: bytes, stop: asyncio.Event) -> bool:
        frames = len(pcm) // 2880
        for _ in range(max(1, frames)):
            if stop.is_set():
                self.events.append(("cut", "", time.monotonic() - self.t0))

                return True

            await asyncio.sleep(0.006)

        return False


FAST = FillerTiming(first_s=0.12, gap_min_s=0.05, gap_max_s=0.08, long_after_s=0.45, landing_after_s=0.3)


def _player(tmp_path: Path, out: RecordingOut) -> FillerPlayer:
    _write_set(tmp_path, SMALL)
    fs = FillerSet.load(tmp_path)
    fs.rng = random.Random(5)

    return FillerPlayer(fs, out, "idle", out.t0, timing=FAST)


def test_slow_reply_hears_escalating_fillers_then_a_landing(tmp_path):
    out = RecordingOut()

    async def go() -> FillerClip | None:
        p = _player(tmp_path, out)
        p.start()
        await asyncio.sleep(0.9)

        return await p.finish()

    landing = asyncio.run(go())
    begins = [e for e in out.events if e[0] == "begin"]
    ids = [e[1] for e in begins]
    assert begins[0][2] >= FAST.first_s - 0.01                                 # nothing before ~1.8 s (scaled)
    assert ids[0] in ("s1", "s2") and landing is not None and ids[-1] == landing.id and landing.stage == "landing"
    assert all(a != b for a, b in pairwise(ids))           # never the same clip twice in a row
    assert any(i.startswith("l") for i in ids) and any(i.startswith("t") for i in ids)


def test_fast_reply_hears_nothing(tmp_path):
    out = RecordingOut()

    async def go() -> FillerClip | None:
        p = _player(tmp_path, out)
        p.start()
        await asyncio.sleep(0.05)                                            # the reply was ready before first_s

        return await p.finish()

    assert asyncio.run(go()) is None and out.events == []


def test_reply_soon_after_the_first_filler_cuts_it_and_skips_the_landing(tmp_path):
    out = RecordingOut()

    async def go() -> FillerClip | None:
        p = _player(tmp_path, out)
        p.start()
        await asyncio.sleep(FAST.first_s + 0.02)                             # inside the first clip
        return await p.finish()

    assert asyncio.run(go()) is None
    assert [e[0] for e in out.events] == ["begin", "cut"]                   # straight into the answer


def test_reply_after_a_long_wait_cuts_the_filler_then_lands(tmp_path):
    out = RecordingOut()

    async def go() -> FillerClip | None:
        p = _player(tmp_path, out)
        p.start()
        await asyncio.sleep(FAST.first_s + FAST.landing_after_s + 0.05)
        return await p.finish()

    landing = asyncio.run(go())
    assert landing is not None and landing.stage == "landing"
    assert out.events[-1][:2] == ("begin", landing.id)


def test_barge_in_cancels_without_a_landing(tmp_path):
    out = RecordingOut()

    async def go() -> None:
        p = _player(tmp_path, out)
        p.start()
        await asyncio.sleep(FAST.first_s + 0.02)
        p.cancel()
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert [e[0] for e in out.events] == ["begin"]


def test_default_first_filler_waits_past_a_fast_answer():
    assert FillerTiming().first_s == 1.8       # fast-tier answers land at 1.4-1.7 s after the end of speech


# ------------------------------------------------------------------ the landing line (Oct 3: short, cut on cancel)
def _landing_set(*clips: tuple[str, filler.FillerMood, float]) -> FillerSet:
    landings = tuple(FillerClip(cid, "landing", mood, cid, f"clips/{cid}.wav", secs) for cid, mood, secs in clips)

    return FillerSet(landings, Path(), random.Random(1))


def test_landing_keeps_to_the_short_clips():
    fs = _landing_set(("got", "idle", 0.77), ("found", "idle", 0.85), ("here-we-go", "idle", 1.89))
    assert {fs.pick_landing("idle", 0.9).id for _ in range(30)} == {"got", "found"}
    assert {fs.pick_landing("idle", 0.9, avoid="got").id for _ in range(10)} == {"found"}


def test_landing_falls_back_to_the_shortest_then_to_idle():
    fs = _landing_set(("okay-got-it", "calm", 1.41), ("here-we-go", "calm", 0.93), ("got", "idle", 0.77))
    assert fs.pick_landing("calm", 0.9).id == "here-we-go"           # nothing that short in calm: its shortest
    assert fs.pick_landing("happy", 0.9).id == "got"                 # no happy landings: idle stands in
    assert _landing_set().pick_landing("idle", 0.9) is None


def test_default_landing_is_under_a_second():
    assert FillerTiming().landing_max_s == 0.9


def test_cancel_cuts_a_landing_line_already_playing(tmp_path):
    out = RecordingOut()
    _write_set(tmp_path, SMALL, seconds=3.0)
    fs = FillerSet.load(tmp_path)
    p = FillerPlayer(fs, out, "idle", out.t0, timing=FAST)

    async def go() -> None:
        p.start()
        await asyncio.sleep(FAST.first_s + FAST.landing_after_s + 0.05)
        landing = asyncio.create_task(p.finish())
        await asyncio.sleep(0.1)
        p.cancel()
        await asyncio.wait_for(landing, 1.0)

    asyncio.run(go())
    landed = [e for e in out.events if e[0] == "begin" and e[1] in ("g1", "g2")]
    assert landed and out.events[-1][0] == "cut" and out.events[-1][2] > landed[-1][2]
