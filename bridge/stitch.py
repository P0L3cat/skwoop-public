"""Stitched speech: a template like light_set {pct} becomes a short run of pre-rendered clips.

  "light_set", {"pct": 50}  ->  "Okay, setting it to..." + "fifty percent."

The piece library (stitch.json, or ACK_STITCH_LIBRARY) uses the acks.json format, so render_acks.py renders it:
  render_acks.py --library stitch.json --out ~/.skwoop/stitch --yes
Kinds are piece groups: "pct.final", "time.mid", "c.lead" (carrier phrases), "line.joke" (whole lines) ...
Each line is one piece in one prosody position: mid ("fifty percent..."), final ("fifty percent.") or question
("fifty percent?"). Pieces are recorded once per mood (happy, idle, sleep, alert; sad for errors) with a single
tag per mood, so the pieces of one sentence match. "templates" in the same file list each template's parts.

Joining: each piece's silence is trimmed to a few ms, edges get short fades, pieces that run on are crossfaded
and the rest get a short pause. Pieces are already leveled (render_acks.py) and get a small gain trim from their
manifest level so they match. A sentence never mixes moods: if a piece is missing in the asked mood, the whole
sentence uses idle (then happy); if one is still missing, the sentence is read live by the normal TTS.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import random
import re
import statistics
from array import array
from operator import add
from pathlib import Path
from typing import NotRequired, TypedDict, TypeGuard

from acks import EMOTION, RETIRED_MOODS, AckLibrary, clip_key, norm_mood
from dsp import (
    correlate,
    dot,
    dtw_path,
    floats,
    frame_peaks,
    from_floats,
    mul,
    resample,
    scale,
    to_floats,
    xcorr_norm,
)
from providers import TTS_RATE

Audio = array[float]
Level = dict[str, float | int]
Take = tuple[str, Path, Level, str]  # text, path, manifest level, direction
Clip = tuple[str, Path, Level]
SlotValue = str | int | float | dt.date | dt.time | dt.datetime
LineHit = tuple[str, str, str]  # kind, mood, text
JoinInfo = dict[str, object]

HERE = Path(__file__).resolve().parent
DEFAULT_STITCH = HERE / "stitch.json"

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORD_ONES = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
            "twelve": "twelfth"}
MONTHS = "January February March April May June July August September October November December".split()
DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()


def _num(v: SlotValue) -> float:
    """float(v) for the slot values the templates pass; dates and times raise the same TypeError float() would."""
    if isinstance(v, str):
        return float(v)
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    raise TypeError(f"float() argument must be a string or a real number, not '{type(v).__name__}'")


def _whole_int(v: SlotValue) -> int:
    """int(v) for a number or numeric string; dates and times raise the same TypeError int() would."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        return int(v)
    raise TypeError(
        f"int() argument must be a string, a bytes-like object or a real number, not '{type(v).__name__}'")


def num_words(n: int) -> str:
    n = int(n)
    if n < 0:
        return "minus " + num_words(-n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else "-" + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + num_words(n % 100))
    if n < 1_000_000:
        return num_words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + num_words(n % 1000))
    raise ValueError("number too big")


def ordinal_words(n: int) -> str:
    w = num_words(n)
    head, sep, last = w.rpartition("-")
    if last in ORD_ONES:
        last = ORD_ONES[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + sep + last


def pct_words(p: SlotValue) -> str:
    return num_words(round(_num(p))) + " percent"


def temp_words(t: SlotValue) -> str:
    n = round(_num(t))
    return num_words(n) + (" degree" if abs(n) == 1 else " degrees")


def dur_words(seconds: SlotValue) -> list[str]:
    """Duration as one or two phrases: [phrase] or [hours phrase, minutes phrase] (joined with "and")."""
    s = int(round(_num(seconds)))
    if s <= 0:
        raise ValueError("duration must be positive")
    special = {1800: "half an hour", 5400: "an hour and a half", 90: "ninety seconds"}
    if s in special:
        return [special[s]]
    if s < 60:
        return [num_words(s) + (" second" if s == 1 else " seconds")]
    if s % 3600 == 0 and (s // 3600 <= 12 or s == 86400):
        h = s // 3600
        return ["one hour" if h == 1 else num_words(h) + " hours"]
    m = s // 60
    if s % 60 == 0 and (m <= 60 or m in (90, 120)):
        return ["one minute" if m == 1 else num_words(m) + " minutes"]
    if s % 60 == 0 and m < 12 * 60:
        h, mm = divmod(m, 60)
        hours = "one hour" if h == 1 else num_words(h) + " hours"
        minutes = "one minute" if mm == 1 else num_words(mm) + " minutes"
        return [hours, minutes]
    # anything else (e.g. 1 h 2 min 5 s, 30 hours): spelled out; these have no pieces, so they're read live
    h, rest = divmod(s, 3600)
    mm, ss = divmod(rest, 60)
    out = [f"{num_words(n)} {u}{'' if n == 1 else 's'}" for n, u in ((h, "hour"), (mm, "minute"), (ss, "second")) if n]
    return [", ".join(out[:-1]) + (" and " if len(out) > 1 else "") + out[-1]]


def time_words(value: SlotValue) -> tuple[str, str]:
    """'15:45', '3:45 pm', 'noon', datetime.time -> ("three forty-five", "in the afternoon")."""
    if isinstance(value, (dt.time, dt.datetime)):
        h, m = value.hour, value.minute
    else:
        v = str(value).strip().lower()
        if v in ("noon", "midday"):
            return "noon", ""
        if v == "midnight":
            return "midnight", ""
        mt = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", v)
        if not mt:
            raise ValueError(f"bad time {value!r}")
        h, m = int(mt.group(1)), int(mt.group(2) or 0)
        if mt.group(3):
            h = h % 12 + (12 if mt.group(3).startswith("p") else 0)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {value!r}")
    if m == 0 and h == 12:
        return "noon", ""
    if m == 0 and h == 0:
        return "midnight", ""
    h12 = h % 12 or 12
    words = f"{num_words(h12)} o'clock" if m == 0 else f"{num_words(h12)} oh {num_words(m)}" if m < 10 \
        else f"{num_words(h12)} {num_words(m)}"
    period = "in the morning" if 5 <= h < 12 else "in the afternoon" if 12 <= h < 17 else \
        "in the evening" if 17 <= h < 21 else "at night"
    return words, period


def date_words(value: SlotValue) -> tuple[str, str]:
    """'2026-10-12', '10-12', datetime.date -> ("October", "twelfth")."""
    if isinstance(value, (dt.date, dt.datetime)):
        mo, d = value.month, value.day
    else:
        parts = [int(x) for x in re.findall(r"\d+", str(value))]
        if len(parts) == 3:
            parts = parts[1:]
        if len(parts) != 2:
            raise ValueError(f"bad date {value!r}")
        mo, d = parts
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        raise ValueError(f"bad date {value!r}")
    return MONTHS[mo - 1], ordinal_words(d)


# Slot values -> the spoken words of each slot type. Rooms, devices, states and weather come from the library.
POSITIONS = {"mid": "...", "final": ".", "q": "?", "end": "."}


def piece_text(words: str, pos: str, plural: bool = False, prep: str = "in") -> str:
    """The line recorded for a slot word in a prosody position (the text the clip says).
    mid "fifty percent..." | final "fifty percent." | q "fifty percent?" | and "and sunny." (weather)
    | in "In the kitchen..." / in_end "in the kitchen." (rooms; prep "on" for the porch, "" for upstairs)
    | subj "The lights are..." (devices)"""
    if pos == "mid":
        return words + "..."
    if pos == "q":
        return words + "?"
    if pos == "and":
        return f"and {words}."
    if pos == "plus":
        return f"{words} and..."
    if pos in ("in", "in_end"):
        head = f"{prep} {words}" if prep else words
        return head[:1].upper() + head[1:] + "..." if pos == "in" else head + "."
    if pos == "subj":
        return f"{words[:1].upper()}{words[1:]} {'are' if plural else 'is'}..."
    return words + "."


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


class Template(TypedDict):
    parts: list[str]
    moods: NotRequired[list[str]]
    default: NotRequired[str]


# A vocab row is either spoken text or a dict of say/prep/plural/carrier forms.
VocabEntry = dict[str, str | bool] | str


class JoinProfile(TypedDict):
    pause_run: float
    pause_comma: float
    pause_sent: float
    tail_cut_db: float | None
    tail_keep_s: float
    tail_tempo: float
    tail_tempo_s: float
    tail_lift_db: float
    tail_lift_s: float
    onset_min_db: float
    onset_fade_s: float
    fallback: NotRequired[str]


class Edge(TypedDict):
    a0: int
    n: int
    mfcc: list[list[float]]
    c0: list[float]
    period: list[float]
    voicing: list[float]
    word: NotRequired[str]
    span: NotRequired[list[float]]
    level_db: NotRequired[float]


# "or" is a keyword, so these overlap records stay functional TypedDicts.
Analysis = TypedDict("Analysis", {
    "v": int,
    "rate": int,
    "n": int,
    "ol": Edge | None,
    "or": Edge | None,
})
Piece = TypedDict("Piece", {
    "x": Audio,
    "ana": Analysis,
    "ol": str | None,
    "or": str | None,
})
Sidecar = TypedDict("Sidecar", {
    "alignment": dict[str, object],
    "ol": str | None,
    "or": str | None,
    "analysis": Analysis,
}, total=False)
class Cut(TypedDict):
    voiced: float
    mfcc_d: float
    period: int | None
    xcorr: float | None
    cut: str
    ia: int
    ib: int


def _is_sidecar(value: object) -> TypeGuard[Sidecar]:
    return isinstance(value, dict)


def _is_level(value: object) -> TypeGuard[Level]:
    return isinstance(value, dict)


def _manifest_take(entry: object, text: str, folder: Path) -> Take | None:
    """One usable manifest row as (text, path, level, direction), or None when the take is missing or bad."""
    if not isinstance(entry, dict) or entry.get("text") != text or entry.get("bad"):
        return None
    file = entry.get("file")
    if not isinstance(file, str) or not (folder / file).is_file():
        return None
    level_raw = entry.get("level") or {}
    level: Level = level_raw if _is_level(level_raw) else {}
    direction = entry.get("direction") or ""
    if not isinstance(direction, str):
        direction = ""
    return text, folder / file, level, direction


class StitchLibrary:
    """Stitch templates over a rendered piece library (acks.AckLibrary underneath)."""

    def __init__(self, library: Path | str | None = None, clips: Path | str | None = None,
                 whole_lines: list[AckLibrary] | None = None) -> None:
        """whole_lines: more AckLibrary objects (e.g. the bridge's ack library) whose recorded lines are played
        instead of a stitched sentence when one says exactly the same words."""
        lib = library or os.environ.get("ACK_STITCH_LIBRARY") or DEFAULT_STITCH
        out = clips or os.environ.get("ACK_STITCH_DIR") or Path.home() / ".skwoop" / "stitch"
        self.lib = AckLibrary(lib, out)
        self.whole_libs = list(whole_lines or [])
        self._feat: dict[str, float | None] = {}   # per clip file: speech level, for take matching
        # id(library) -> (refresh stamp, {normalized words: [(kind, mood, text)]})
        self._index: dict[int, tuple[object, dict[str, list[LineHit]]]] = {}
        self.path = Path(lib)
        self.templates: dict[str, Template] = {}
        self.vocab: dict[str, dict[str, VocabEntry]] = {}
        self._mtime = -1.0

    def refresh(self) -> None:
        self.lib.refresh()
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self._mtime:
            import json
            d = json.loads(self.path.read_text())
            self.templates = d.get("templates", {})
            self.vocab = d.get("vocab", {})
            self._mtime = m

    # ---------------- words for one slot
    def slot_words(self, slot: str, value: SlotValue) -> list[tuple[str, str]]:
        """[(slot type, words)], usually one; a duration like 1 h 30 min gives two phrases."""
        if slot in ("pct", "battery", "volume", "brightness"):
            return [("pct", pct_words(value))]
        if slot == "num":
            return [("num", num_words(_whole_int(value)))]
        if slot in ("temp", "high", "low"):
            return [("temp", temp_words(value))]
        if slot == "dur":
            return [("dur", w) for w in dur_words(value)]
        if slot == "time":
            return [("time", time_words(value)[0])]
        if slot == "period":
            return [("period", time_words(value)[1])]
        if slot == "date":
            return [("date", "{} {}".format(*date_words(value)))]
        if slot == "month":
            return [("month", date_words(value)[0] if not str(value).isalpha() else str(value).capitalize())]
        if slot == "ordinal":
            return [("ord", ordinal_words(_whole_int(value)))]
        if slot == "day":
            v = str(value).strip().lower()
            if v in ("today", "tonight", "tomorrow", "yesterday"):
                return [("day", v)]
            try:
                return [("day", DAYS[dt.date.fromisoformat(v).weekday()])]
            except ValueError:
                return [("day", v.capitalize() if v.capitalize() in DAYS else v)]
        if slot in ("room", "device", "state", "wx"):
            table = self.vocab.get(slot, {})
            key = str(value).strip().lower().replace("_", " ")
            if key not in table:
                raise ValueError(f"unknown {slot} {value!r}")
            item = table[key]
            if isinstance(item, dict):
                spoken = item["say"]
                if not isinstance(spoken, str):
                    raise TypeError(f"vocab {slot} {key!r} say is {type(spoken).__name__}")
                return [(slot, spoken)]
            return [(slot, item)]
        raise ValueError(f"unknown slot {slot!r}")

    def plural(self, device: SlotValue) -> bool:
        e = self.vocab.get("device", {}).get(str(device).strip().lower().replace("_", " "))
        return bool(isinstance(e, dict) and e.get("plural"))

    # ---------------- template -> list of (kind, text)
    def expand(self, template: str, values: dict[str, SlotValue]) -> list[tuple[str, str]]:
        if template not in self.templates:
            raise KeyError(template)
        out: list[tuple[str, str]] = []
        for part in self.templates[template]["parts"]:
            kind, _, arg = part.partition(":")
            if kind in ("lead", "mid", "end"):                     # a carrier phrase: lead:its_about
                out.append((f"c.{kind}", self.carrier(kind, arg)))
            elif kind == "line":                                   # a whole line, picked at random later
                out.append((f"line.{arg}", "*"))
            else:                                                  # a slot: pct.final, dur.mid, room.q ...
                slot, pos = kind, arg or "final"
                if slot == "period" and slot not in values and "time" in values:
                    values = {**values, "period": values["time"]}           # the period comes from the time
                if slot not in values:
                    raise ValueError(f"template {template} needs {slot!r}")
                words = self.slot_words(slot, values[slot])
                entry = self.vocab.get(slot, {}).get(str(values[slot]).strip().lower().replace("_", " "))
                entry = entry if isinstance(entry, dict) else {}
                if words == [("period", "")]:                       # noon, midnight: no period; end on the time
                    if out and out[-1][0].endswith(".mid") and out[-1][1].endswith("..."):
                        k0, t0 = out.pop()
                        out.append((k0[:-4] + "." + pos, piece_text(t0[:-3], pos)))
                    continue
                for i, (stype, w) in enumerate(words):             # two-part durations: "one hour and..." "..."
                    p = "plus" if i < len(words) - 1 else pos
                    prep = entry.get("prep", "in")
                    prep_s = prep if isinstance(prep, str) else "in"
                    out.append((f"{stype}.{p}", piece_text(w, p, bool(entry.get("plural")), prep_s)))
        return out

    def carrier(self, form: str, cid: str) -> str:
        c = self.vocab.get("carrier", {}).get(cid)
        if not isinstance(c, dict) or form not in c:
            raise ValueError(f"no {form} form for carrier {cid!r}")
        word = c[form]
        if not isinstance(word, str):
            raise ValueError(f"no {form} form for carrier {cid!r}")
        return word

    # ---------------- audio
    def _cands(self, kind: str, mood: str, text: str) -> list[Take]:
        """[(text, path, level entry, direction)] for every usable take of a piece in this mood ([] = not rendered).
        text "*" (a whole-line kind): one line with rendered takes, picked at random, so lines keep their variety.
        Takes marked "bad" in the manifest (STT heard a repeat or the wrong word twice) are never used."""
        def takes(t: str) -> list[Take]:
            out: list[Take] = []
            for k in (kind, kind + "+"):                  # "<kind>+": extra takes added by a later batch
                b = self.lib.kinds.get(k, {}).get("moods", {}).get(mood)
                if not b or t not in b["lines"]:
                    continue
                n_takes = b["takes"]
                if not isinstance(n_takes, int):
                    continue
                for take in range(n_takes):
                    e = self.lib.manifest.get(clip_key(k, mood, t, take))
                    got = _manifest_take(e, t, self.lib.dir)
                    if got is not None:
                        out.append(got)
            return out
        if text != "*":
            return takes(text)
        lines = list(dict.fromkeys(t for k in (kind, kind + "+")
                                   for t in (self.lib.kinds.get(k, {}).get("moods", {}).get(mood) or {}).get(
                                       "lines", [])))
        random.shuffle(lines)
        for t in lines:
            if c := takes(t):
                return c
        return []

    def _clip(self, kind: str, mood: str, text: str) -> Clip | None:
        """(text, path, level entry) of one random usable take; None if not rendered."""
        c = self._cands(kind, mood, text)
        return random.choice(c)[:3] if c else None

    def _energy(self, path: Path, lv: Level) -> float | None:
        """A take's delivery energy: the manifest's pre-leveling loudness, else its speech level (cached)."""
        loud = lv.get("lufs_in")
        if isinstance(loud, (int, float)) and not isinstance(loud, bool):
            return float(loud)
        key = str(path)
        if key not in self._feat:
            self._feat[key] = active_db(_trim(_load(path)))
            if len(self._feat) > 4096:
                self._feat.pop(next(iter(self._feat)))
        return self._feat[key]

    def _pick_takes(self, parts: list[tuple[str, str]], cands: list[list[Take]], calm: bool = False) -> list[Take]:
        """One take per piece. The carrier phrase (first "c." piece, else the first piece) is picked at random (calm
        takes first when the asked mood was retired); every other piece takes the take whose delivery tag and energy
        are closest to the carrier's."""
        ci = next((i for i, (k, _) in enumerate(parts) if k.startswith("c.")), 0)
        pool = cands[ci]
        if calm:
            pool = [c for c in pool if "calm" in c[3]] or pool
        car = random.choice(pool)
        ec = self._energy(car[1], car[2])

        def score(c: Take) -> float:
            s = 0.0 if c[3] == car[3] else 0.5                                # same mood tag as the carrier
            e = self._energy(c[1], c[2])
            if ec is not None and e is not None:
                s += abs(e - ec) / 3                                          # dB of delivery energy
            return s + random.random() * 0.05                                 # near-ties stay varied
        return [car if i == ci else (cs[0] if len(cs) == 1 else min(cs, key=score)) for i, cs in enumerate(cands)]

    def _lines_index(self, lib: AckLibrary, stitch_lib: bool) -> dict[str, list[LineHit]]:
        lib.refresh()
        stamp = lib._seen
        hit = self._index.get(id(lib))
        if hit and hit[0] == stamp:
            return hit[1]
        idx: dict[str, list[LineHit]] = {}
        for kind, spec in lib.kinds.items():
            if stitch_lib and not kind.startswith("line."):
                continue
            for m, b in spec["moods"].items():
                for t in b["lines"]:
                    idx.setdefault(_words_key(t), []).append((kind, m, t))
        self._index[id(lib)] = (stamp, idx)
        return idx

    def _whole(self, text: str, mood: str, calm: bool = False) -> Take | None:
        """A recorded whole line that says `text` in `mood`: (text, path, level, direction) or None."""
        key = _words_key(text)
        for lib in (self.lib, *self.whole_libs):
            for kind, m, t in self._lines_index(lib, lib is self.lib).get(key, []):
                if m != mood:
                    continue
                b = lib.kinds[kind]["moods"][m]
                found: list[Take] = []
                n_takes = b["takes"]
                if not isinstance(n_takes, int):
                    continue
                for take in range(n_takes):
                    e = lib.manifest.get(clip_key(kind, m, t, take))
                    got = _manifest_take(e, t, lib.dir)
                    if got is not None:
                        found.append(got)
                if found:
                    if calm:
                        found = [c for c in found if "calm" in c[3]] or found
                    return random.choice(found)
        return None

    def say(self, template: str, values: dict[str, SlotValue] | None = None,
            mood: str | None = None) -> dict[str, object]:
        """-> text, pcm (None = read live), mood, emotion, source line|stitch|tts, pieces, missing.
        A retired mood (sleep) is spoken as the mood it maps to (idle, calm takes first). A recorded whole line with
        the same words beats stitching; within a mood, stitching beats falling back to another mood."""
        self.refresh()
        values = values or {}
        parts = self.expand(template, values)
        asked = norm_mood(mood) if mood else self.templates[template].get("default", "idle")
        want = RETIRED_MOODS.get(asked, asked)
        calm = want != asked
        allowed = [m for m in (self.templates[template].get("moods") or []) if m not in RETIRED_MOODS] or None
        order = [m for m in dict.fromkeys([want, "idle", "happy"]) if not allowed or m in allowed]
        fixed = None if any(t == "*" for _, t in parts) else _cap(" ".join(t for _, t in parts))
        missing: list[str] = []
        for m in order:
            if fixed and len(parts) > 1 and (w := self._whole(fixed, m, calm)):
                return {"template": template, "text": w[0], "pcm": join([w[:3]]), "mood": m, "requested": asked,
                        "emotion": EMOTION[m], "source": "line", "pieces": 1, "missing": [], "kind": template,
                        "file": w[1].name}
            cands = [self._cands(k, m, t) for k, t in parts]
            if all(cands):
                clips = self._pick_takes(parts, cands, calm)
                text = _cap(" ".join(c[0] for c in clips))
                ci = next((i for i, (k, _) in enumerate(parts) if k.startswith("c.")), 0)
                joins: list[JoinInfo] = []
                pcm = join([c[:3] for c in clips], ref=ci, report=joins)
                return {"template": template, "text": text, "pcm": pcm, "mood": m,
                        "requested": asked, "emotion": EMOTION[m], "source": "stitch", "pieces": len(clips),
                        "missing": [], "kind": template, "file": None, "takes": [c[1].name for c in clips],
                        "joins": joins}
            if m == want:
                missing = [f"{k}/{m}: {t}" for (k, t), c in zip(parts, cands, strict=True) if not c]
        text = _cap(" ".join(t if t != "*" else self._any_line(k, want) for k, t in parts))
        return {"template": template, "text": text, "pcm": None, "mood": want, "requested": asked,
                "emotion": EMOTION[want], "source": "tts", "pieces": len(parts), "missing": missing,
                "kind": template, "file": None}

    def _any_line(self, kind: str, mood: str) -> str:
        b = self.lib.kinds.get(kind, {}).get("moods", {})
        lines = (b.get(mood) or b.get("idle") or next(iter(b.values()), {"lines": [""]}))["lines"]
        picked = random.choice(lines)
        return picked if isinstance(picked, str) else str(picked)

    def describe(self) -> dict[str, object]:
        self.refresh()
        raw = self.lib.describe()
        d: dict[str, object] = {}
        if isinstance(raw, dict):
            for key, val in raw.items():
                if isinstance(key, str):
                    d[key] = val
        d["templates"] = len(self.templates)
        d["retired_moods"] = dict(RETIRED_MOODS)
        d["join"] = JOIN
        return d


def _words_key(text: str) -> str:
    """Words only: "Okay, setting it to... fifty percent!" -> "okay setting it to fifty percent"."""
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("-", " ")).split())


# ---------------------------------------------------------------- joining
# Audio is array('d') in [-1, 1) at TTS_RATE; the long loops (conversion, gain, correlation, DTW) run in dsp.c.
# Edges: anything below -45 dBFS or 36 dB under the piece's peak (breath, room tone) is trimmed, 10 ms kept.
# Joins: an equal-power crossfade of XFADE_S at every join; the next piece starts PAUSE after the last one ends,
# minus the crossfade (so "runs straight on" overlaps by XFADE_S). Pauses follow the punctuation the piece ends on.
TRIM_DB = -45.0
TRIM_REL_DB = -36.0
KEEP_S = 0.010
EDGE_FADE_S = 0.008   # outer edges of the utterance
XFADE_S = 0.030
LEVEL_MAX_DB = 4.0    # each piece's speech level -> the carrier's, at most this much
EDGE_S, EDGE_MAX_DB, EDGE_RAMP_S = 0.20, 1.5, 0.30   # per-join: half the edge level step, ramped out
# Join profiles. v3 makes the sentence run on: the drawn-out, fading last syllable of a "..." piece is cut back to
# its body and its last ~220 ms sped up (WSOLA, no pitch change), mid-sentence pauses ~0, the tail lifted a little
# and the next onset eased in.
_V3: JoinProfile = {
    "pause_run": 0.015, "pause_comma": 0.060, "pause_sent": 0.100,
    "tail_cut_db": -9.0,      # end the "..." piece where it falls 9 dB under its speech level ...
    "tail_keep_s": 0.035,     # ... plus 35 ms of the decay, faded out
    "tail_tempo": 1.45, "tail_tempo_s": 0.22,   # the last 220 ms of what's left, 1.45x faster
    "tail_lift_db": 2.0, "tail_lift_s": 0.15,   # ramp the end up 2 dB (it trails off quieter)
    "onset_min_db": -4.0,     # the next piece may start up to 4 dB under (half the edge step), ramped out
    "onset_fade_s": 0.020,    # and fades in over 20 ms on top of the crossfade
}
# overlap (default since Oct 2): pieces rendered with a shared overlap word from their neighbour, with the analysis
# stored at render time in a "<clip>.align.json" sidecar, are joined INSIDE that word (overlap_join). Pieces without
# a usable sidecar join as v3. Same numbers as v3, plus the fallback name.
_OVERLAP: JoinProfile = {
    "pause_run": _V3["pause_run"], "pause_comma": _V3["pause_comma"], "pause_sent": _V3["pause_sent"],
    "tail_cut_db": _V3["tail_cut_db"], "tail_keep_s": _V3["tail_keep_s"],
    "tail_tempo": _V3["tail_tempo"], "tail_tempo_s": _V3["tail_tempo_s"],
    "tail_lift_db": _V3["tail_lift_db"], "tail_lift_s": _V3["tail_lift_s"],
    "onset_min_db": _V3["onset_min_db"], "onset_fade_s": _V3["onset_fade_s"],
    "fallback": "v3",
}
JOIN_PROFILES: dict[str, JoinProfile] = {"v3": _V3, "overlap": _OVERLAP}
_join_name = os.environ.get("STITCH_JOIN", "overlap")
JOIN: str = _join_name if _join_name in JOIN_PROFILES else "overlap"


def _linspace(a: float, b: float, n: int) -> list[float]:
    """numpy.linspace(a, b, n): n evenly spaced points, both ends included."""
    if n <= 1:
        return [a] * n
    step = (b - a) / (n - 1)
    return [a + k * step for k in range(n - 1)] + [b]


def _peak(x: Audio) -> float:
    return max(max(x), -min(x)) if x else 0.0


def _db_frames(x: Audio, n: int) -> list[float]:
    """Mean power of each whole n-sample frame."""
    return [dot(f, f) / n for f in (x[i:i + n] for i in range(0, len(x) // n * n, n))]


def pause_after(text: str, profile: str | None = None) -> float:
    pr = JOIN_PROFILES[profile or JOIN]
    t = text.rstrip()
    if t.endswith("..."):
        return pr["pause_run"]
    if t[-1:] in ".!?":
        return pr["pause_sent"]
    if t[-1:] in ",;:":
        return pr["pause_comma"]
    return 0.0


def wsola(x: Audio, rate: float, win_s: float = 0.025, tol_s: float = 0.008) -> Audio:
    """Time-compress (rate > 1) or stretch speech without changing pitch: waveform-similarity overlap-add.
    The first frame lines up exactly with the input, so a tail can be spliced back on."""
    n = int(win_s * TTS_RATE) // 2 * 2
    hs, tol = n // 2, int(tol_s * TTS_RATE)
    if rate == 1.0 or len(x) < 2 * n + tol:
        return floats(x)
    ha = hs * rate
    w = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n + 1)) for i in range(1, n + 1)]   # numpy.hanning(n + 2)[1:-1]
    frames = int((len(x) - n - tol) / ha)
    out = [0.0] * (frames * hs + n)
    norm = [0.0] * len(out)
    prev = 0
    for k in range(frames + 1):
        if k == 0:
            pos = 0
        else:
            want = int(round(k * ha))
            nat = prev + hs                                   # where the last frame naturally continues
            lo, hi = max(0, want - tol), min(len(x) - n, want + tol)
            if hi <= lo or nat + n > len(x):
                break
            tmpl = x[nat:nat + n]
            seg = x[lo:hi + n]
            c = correlate(seg, tmpl)
            pos = lo + max(range(len(c)), key=c.__getitem__)
        o = k * hs
        for i in range(n):
            out[o + i] += x[pos + i] * w[i]
            norm[o + i] += w[i]
        prev = pos
    last = max(i for i, v in enumerate(norm) if v > 1e-3)
    return floats([v / max(m, 0.5) for v, m in zip(out[:last + 1], norm[:last + 1], strict=True)])


_TIGHT: dict[tuple[str, str], Audio] = {}


def tighten_tail(x: Audio, profile: str | None = None, key: str | None = None) -> Audio:
    """v3: a "..." piece ends on a drawn-out syllable that fades for 300-500 ms. Cut it where it falls tail_cut_db
    under the piece's speech level (keep tail_keep_s, faded), speed up the last tail_tempo_s, lift the end."""
    pr = JOIN_PROFILES[profile or JOIN]
    if pr["tail_cut_db"] is None or len(x) < int(0.15 * TTS_RATE):
        return x
    if key:
        ck = (key, profile or JOIN)
        if ck in _TIGHT:
            return floats(_TIGHT[ck])
    lvl = active_db(x)
    if lvl is None:
        return x
    win = int(0.01 * TTS_RATE)
    env = [10 * math.log10(p + 1e-12) for p in _db_frames(x, win)]
    body = [i for i, e in enumerate(env) if e >= lvl + pr["tail_cut_db"]]
    if not body:
        return x
    cut = min(len(x), (body[-1] + 1) * win + int(pr["tail_keep_s"] * TTS_RATE))
    y = x[:max(cut, int(0.12 * TTS_RATE))]
    if pr["tail_tempo"] != 1.0:
        s0 = max(int(0.1 * TTS_RATE), len(y) - int(pr["tail_tempo_s"] * TTS_RATE))
        if len(y) - s0 > int(0.08 * TTS_RATE):
            fast = wsola(y[s0:], pr["tail_tempo"])
            xf = min(int(0.006 * TTS_RATE), len(fast) // 4)
            t = _linspace(0, math.pi / 2, xf)
            head = y[:s0 + xf]
            blended = [h * math.cos(a) + f * math.sin(a) for h, f, a in zip(head[s0:], fast[:xf], t, strict=True)]
            head[s0:] = floats(blended)
            y = head + fast[xf:]
    if pr["tail_lift_db"]:
        r = min(len(y), int(pr["tail_lift_s"] * TTS_RATE))
        lift = _linspace(0, 1, r)
        y[len(y) - r:] = mul(y[len(y) - r:], floats([10 ** (pr["tail_lift_db"] * g / 20) for g in lift]))
    n = min(int(pr["tail_keep_s"] * TTS_RATE), len(y) // 4)
    if n:
        fade = _linspace(0, math.pi / 2, n)
        y[len(y) - n:] = mul(y[len(y) - n:], floats(map(math.cos, fade)))
    if key:
        ck = (key, profile or JOIN)
        if len(_TIGHT) > 512:
            _TIGHT.pop(next(iter(_TIGHT)))
        _TIGHT[ck] = floats(y)
    return y


def _load(path: Path | str) -> Audio:
    from providers import wav_to_pcm
    pcm, rate = wav_to_pcm(Path(path).read_bytes())
    if rate != TTS_RATE:
        pcm = resample(pcm, rate, TTS_RATE)
    return to_floats(pcm)


def _trim(x: Audio) -> Audio:
    win = int(0.01 * TTS_RATE)
    if len(x) < win:
        return x
    env = frame_peaks(x, win)
    thr = max(10 ** (TRIM_DB / 20), max(env) * 10 ** (TRIM_REL_DB / 20))
    loud = [e > thr for e in env]
    pairs = [i for i in range(len(loud) - 1) if loud[i] and loud[i + 1]]   # 20 ms above: a lone tick isn't speech
    if pairs:
        first, last = pairs[0], pairs[-1] + 1
    else:
        on = [i for i, v in enumerate(loud) if v]
        if not on:
            return x[:0]
        first, last = on[0], on[-1]
    keep = int(KEEP_S * TTS_RATE)
    return x[max(0, first * win - keep): min(len(x), (last + 1) * win + keep)]


def _fade(x: Audio, n_s: float = EDGE_FADE_S) -> Audio:
    n = min(int(n_s * TTS_RATE), len(x) // 2)
    if n:
        r = floats(_linspace(0, 1, n))
        x[:n] = mul(x[:n], r)
        r.reverse()
        x[-n:] = mul(x[-n:], r)
    return x


def active_db(x: Audio) -> float | None:
    """Speech level: mean power of the 20 ms frames within 30 dB of the loudest frame, in dB."""
    n = int(0.02 * TTS_RATE)
    if len(x) < n:
        return None
    p = _db_frames(x, n)
    top = max(p)
    if top <= 1e-12:
        return None
    p = [v for v in p if v > top * 1e-3]
    return 10 * math.log10(sum(p) / len(p))


# ---------------------------------------------------------------------------------------------- overlap join
# Each piece is a render that also says the word it shares with its neighbour ("It's three forty-five in" +
# "four in the afternoon." share "in"). Render-time analysis (stored in the sidecar) gives each overlap word's
# MFCC frames, voicing and pitch periods; at runtime a DTW maps one word onto the other, the cut goes where the two
# renders sound most alike (voiced frames first), is refined by waveform cross-correlation to the same pitch phase,
# snapped to a rising zero crossing, and overlap-added over ~2 pitch periods with a ramped gain so both sides match.
# The analysis code (MFCC, pitch marks) lives with the renderer in git history
# (stitch.py before an earlier revision).
OV_MID = (0.05, 0.95)       # cut anywhere in the (padded, audible) word but its very edges
OV_PITCH_COST, OV_VOICED_BONUS, OV_MIXED_COST = 3.0, 3.0, 8.0   # cut scoring, in MFCC-distance units
OV_GAIN_MAX_DB, OV_GAIN_RAMP_S = 4.0, 0.25
OV_PITCH_MAX_ST = 1.0
OV_FADE_PERIODS, OV_FADE_MIN_S, OV_FADE_MAX_S = 2.0, 0.004, 0.015
OV_ANA_VERSION = 1


def _rising_zero(x: Audio, at: int, span: int) -> int:
    lo, hi = max(1, at - span), min(len(x) - 1, at + span)
    seg = x[lo - 1:hi]
    z = [i + lo for i in range(len(seg) - 1) if seg[i] <= 0 < seg[i + 1]]
    return min(z, key=lambda v: abs(v - at)) if z else at


def overlap_cut(a: Audio, ea: Edge, off_a: int, b: Audio, eb: Edge) -> Cut:
    """Runtime cut inside a shared word from the two clips' precomputed edge analysis: DTW on the stored MFCC
    frames, cut scoring from the stored voicing/periods, then a waveform cross-correlation only within ±1 pitch
    period around the chosen point (a few ms) and a rising-zero snap. a is the utterance so far (ea's sample
    indices are + off_a in it). -> {ia, ib (samples), period, voiced, xcorr, mfcc_d, cut}."""
    hop = int(0.005 * TTS_RATE)
    fa, ca, pa_, va_ = ea["mfcc"], ea["c0"], ea["period"], ea["voicing"]
    fb, cb, pb_, vb_ = eb["mfcc"], eb["c0"], eb["period"], eb["voicing"]
    a0, b0 = ea["a0"] + off_a, eb["a0"]
    path = dtw_path(fa, fb)
    lo, hi = int(OV_MID[0] * len(fa)), max(int(OV_MID[0] * len(fa)) + 1, int(OV_MID[1] * len(fa)))
    best: tuple[float, int, int, float, float, str, int | None] | None = None
    for i, j in path:
        if not lo <= i < hi:
            continue
        d = math.dist(fa[i], fb[j])
        de = abs(ca[i] - cb[j])
        va, vb, pa, pb = va_[i], vb_[j], pa_[i], pb_[j]
        if va > 0.6 and vb > 0.6 and pa and pb:                   # voiced on both sides: pitch must agree
            gap = abs(12 * math.log2(pb / pa))
            score, kind = d + 0.5 * de + OV_PITCH_COST * max(0.0, gap - OV_PITCH_MAX_ST) - OV_VOICED_BONUS, "voiced"
        elif va < 0.45 and vb < 0.45:                             # both unvoiced (closure, fricative): no pitch
            score, kind = d + 0.8 * de, "unvoiced"
        else:                                                     # voiced on one side only: worst place to cut
            score, kind = d + 0.5 * de + OV_MIXED_COST, "mixed"
        if best is None or score < best[0]:
            best = (score, a0 + i * hop, b0 + j * hop, min(va, vb), d, kind, int(pa) if pa else None)
    if best is None:                                              # word too short: its middle
        best = (0.0, a0 + ea["n"] // 2, b0 + eb["n"] // 2, 0.0, 0.0, "middle", None)
    _, ia, ib, voiced, d, kind, per = best
    out: Cut = {"voiced": round(float(voiced), 2), "mfcc_d": round(d, 2), "period": per, "xcorr": None,
                "cut": kind, "ia": 0, "ib": 0}
    if per and voiced > 0.5:                                      # same pitch phase on both sides
        w = int(1.5 * per)
        ref = a[max(0, ia - w): ia + w]
        lead = ia - max(0, ia - w)
        s0 = ib - per - lead
        n = len(ref)
        if s0 >= 0 and s0 + n + 2 * per <= len(b) and n > 8:
            seg = b[s0:s0 + n + 2 * per]
            c = xcorr_norm(seg, ref)
            k = max(range(len(c)), key=c.__getitem__)
            ib += k - per
            out["xcorr"] = round(c[k], 3)
        z = _rising_zero(a, ia, per // 2)
        ib += z - ia
        ia = z
    out["ia"] = int(ia)
    out["ib"] = int(max(0, min(len(b) - 1, ib)))
    return out


def _edge_level(edge: Edge) -> float | None:
    level = edge.get("level_db")
    if isinstance(level, (int, float)) and not isinstance(level, bool):
        return float(level)
    return None


def _bump_edge(edge: Edge | None, gain: float) -> Edge | None:
    """Copy of an overlap edge whose stored level includes the manifest trim. None and missing levels stay put."""
    if edge is None or not gain:
        return edge
    level = _edge_level(edge)
    if level is None:
        return edge
    bumped: Edge = {
        "a0": edge["a0"], "n": edge["n"], "mfcc": edge["mfcc"], "c0": edge["c0"],
        "period": edge["period"], "voicing": edge["voicing"], "level_db": level + gain,
    }
    word, span = edge.get("word"), edge.get("span")
    if isinstance(word, str):
        bumped["word"] = word
    if span is not None:
        bumped["span"] = span
    return bumped


def overlap_join(pieces: list[Piece], energy_match: bool = True, report: list[JoinInfo] | None = None) -> Audio:
    """pieces: [{"x": float audio at TTS_RATE, "ana": the sidecar's render-time analysis, "ol": word shared with the
    previous piece, "or": with the next}] -> one float utterance. Join k cuts inside the word pieces[k]["or"] ==
    pieces[k+1]["ol"]; everything of piece k after the cut (and of piece k+1 before it) is dropped. Runtime work
    per join: a DTW on the two stored MFCC edge sequences (~60 x 60 frames), a cross-correlation over ±1 pitch
    period, an overlap-add over ~2 periods and a gain ramp. ValueError if a piece has no usable analysis."""
    out = floats([])
    off = 0
    prev: Analysis | None = None
    prev_gain = 0.0
    for k, p in enumerate(pieces):
        x = floats(p["x"])
        ana = p["ana"]
        # Callers can pass ana=None; the TypedDict does not stop that at runtime.
        version = ana.get("v") if isinstance(ana, dict) else None
        if version != OV_ANA_VERSION:
            raise ValueError("piece has no overlap analysis (v1)")
        if k == 0:
            out, off, prev, prev_gain = x, 0, ana, 0.0
            continue
        if prev is None:
            raise ValueError("piece has no overlap analysis (v1)")
        word = p["ol"]
        ea, eb = prev.get("or"), ana.get("ol")
        info: JoinInfo = {"word": word}
        if not isinstance(ea, dict) or not isinstance(eb, dict):
            raise ValueError(f"overlap word {word!r} not found in both renders")
        cut = overlap_cut(out, ea, off, x, eb)
        ia, ib = cut["ia"], cut["ib"]
        g = 0.0
        la_db, lb_db = _edge_level(ea), _edge_level(eb)
        if energy_match and la_db is not None and lb_db is not None:
            la = la_db + prev_gain                                  # that word's level as it plays now
            g = max(-OV_GAIN_MAX_DB, min(OV_GAIN_MAX_DB, la - lb_db))
            r = min(len(x) - ib, int(OV_GAIN_RAMP_S * TTS_RATE))
            g0 = 10 ** (g / 20)
            ramp = floats([10 ** (g * (1 - t) / 20) for t in _linspace(0, 1, r)])
            x = scale(x[:ib], g0) + mul(x[ib:ib + r], ramp) + x[ib + r:]
            info["gain_db"] = round(g, 2)
        period = cut["period"]
        per = period if cut["voiced"] > 0.5 and period else None
        if per:
            fade = min(OV_FADE_MAX_S * TTS_RATE, max(OV_FADE_MIN_S * TTS_RATE, OV_FADE_PERIODS * per))
        else:
            fade = 0.010 * TTS_RATE
        length = int(fade)
        h = min(length // 2, ia, ib, len(out) - ia, len(x) - ib)
        w = [0.5 - 0.5 * math.cos(t) for t in _linspace(0, math.pi, 2 * h)]   # 0 -> 1, sums to 1 with 1 - w
        mixed = floats([o * (1 - q) + v * q for o, v, q in zip(
            out[ia - h:ia + h], x[ib - h:ib + h], w, strict=True)])
        out = out[:ia - h] + mixed + x[ib + h:]
        off = ia - ib
        or_edge = ana.get("or")                                    # gain still on this piece at its own "or" word
        span = or_edge.get("span") if isinstance(or_edge, dict) else None
        if span and g:
            t_rel = span[0] * TTS_RATE - ib
            prev_gain = g * max(0.0, 1 - t_rel / max(1, int(OV_GAIN_RAMP_S * TTS_RATE)))
        else:
            prev_gain = 0.0
        prev = ana
        info.update({k2: cut[k2] for k2 in ("voiced", "mfcc_d", "xcorr", "cut")}, at_s=round(ia / TTS_RATE, 3),
                    cut_a=int(ia), cut_b=int(ib), fade_ms=round(2 * h / TTS_RATE * 1000, 1),
                    period_ms=round(per / TTS_RATE * 1000, 2) if per else None)
        if report is not None:
            report.append(info)
    return out


def overlap_sidecar(path: Path | str) -> Sidecar | None:
    """'<clip>.align.json' next to a clip: {"alignment": {...}, "ol": word, "or": word, "analysis": {...}}."""
    sc = Path(str(path)).with_suffix(".align.json")
    try:
        loaded = json.loads(sc.read_text())
        if not (loaded.get("alignment") or loaded.get("analysis")):
            return None
        if _is_sidecar(loaded):
            return loaded
        return None
    except (OSError, ValueError):
        return None


def _finish(out: Audio) -> bytes:
    peak = _peak(out)
    if peak > 0.9:
        out = scale(out, 0.9 / peak)
    return from_floats(out)


def _sidecar_version(car: Sidecar | None) -> object:
    if not isinstance(car, dict):
        return None
    ana = car.get("analysis")
    if not isinstance(ana, dict):
        return None
    return ana.get("v")


def _side_word(car: Sidecar | None, side: str) -> str:
    if car is None:
        return ""
    word = car.get("ol") if side == "ol" else car.get("or")
    return word if isinstance(word, str) else ""


def _overlap_ok(cars: list[Sidecar | None]) -> TypeGuard[list[Sidecar]]:
    if len(cars) <= 1:
        return False
    if not all(_sidecar_version(c) == OV_ANA_VERSION for c in cars):
        return False
    return all(_side_word(cars[i], "or").lower() == _side_word(cars[i + 1], "ol").lower() != ""
               for i in range(len(cars) - 1))


def _gain_analysis(ana: Analysis, gain: float) -> Analysis:
    """Analysis whose ol/or levels include the manifest trim. A zero trim returns the same record."""
    if not gain:
        return ana
    return {
        "v": ana["v"], "rate": ana["rate"], "n": ana["n"],
        "ol": _bump_edge(ana.get("ol"), gain), "or": _bump_edge(ana.get("or"), gain),
    }


def _level_delta(lv: Level) -> float | None:
    """Manifest loudness trim in dB, or None when lufs/target are absent. Same clip as the old inline math."""
    measured, target = lv.get("lufs"), lv.get("target")
    if not isinstance(measured, (int, float)) or isinstance(measured, bool):
        return None
    if not isinstance(target, (int, float)) or isinstance(target, bool):
        return None
    return max(-3.0, min(3.0, float(target) - float(measured)))


def join(clips: list[Clip], report: list[JoinInfo] | None = None, ref: int = 0,
         profile: str | None = None) -> bytes:
    """clips: [(text, path, level entry)] -> one 16-bit PCM utterance at TTS_RATE. ref: the carrier piece.
    Each piece: manifest level trim (±3 dB), edge silence trimmed, speech level matched to the carrier's
    (±LEVEL_MAX_DB). Then per join half the edge level step (±EDGE_MAX_DB, ramped out) and an equal-power crossfade.
    profile: "overlap" (default, STITCH_JOIN) joins inside the shared word when every piece has a sidecar with its
    analysis, else "v3", which tightens "..." tails and eases onsets.
    report: if a list, gets one dict per join (pause, level)."""
    profile = profile or JOIN
    pr = JOIN_PROFILES[profile]
    if profile == "overlap":
        cars = [overlap_sidecar(path) for _, path, _ in clips]
        if _overlap_ok(cars):
            ps: list[Piece] = []
            for (_, path, lv), sc in zip(clips, cars, strict=True):
                x = _load(path)
                delta = _level_delta(lv)
                g = 0.0 if delta is None else delta
                if delta is not None:
                    x = scale(x, 10 ** (g / 20))
                ana = sc.get("analysis")
                if not isinstance(ana, dict):
                    raise ValueError("piece has no overlap analysis (v1)")
                # stored levels were measured before this trim
                ol_word, or_word = sc.get("ol"), sc.get("or")
                ps.append({"x": x, "ana": _gain_analysis(ana, g),
                           "ol": ol_word if isinstance(ol_word, str) else None,
                           "or": or_word if isinstance(or_word, str) else None})
            return _finish(_fade(_trim(overlap_join(ps, report=report))))
        fallback = pr.get("fallback")
        if not isinstance(fallback, str):
            raise KeyError("fallback")
        if report is not None:
            report.append({"fallback": fallback, "why": "pieces lack overlap sidecars"})
        profile = fallback
        pr = JOIN_PROFILES[profile]
    pieces: list[tuple[str, Audio, JoinInfo]] = []
    for text, path, lv in clips:
        x = _load(path)
        delta = _level_delta(lv)                                  # small loudness trim to the target
        if delta is not None:
            x = scale(x, 10 ** (delta / 20))
        x = _trim(x)
        if text.rstrip().endswith("...") and len(pieces) < len(clips) - 1:     # not the last piece
            x = tighten_tail(x, profile, key=f"{path}|{lv.get('lufs')}")
        pieces.append((text, x, {}))
    ref = ref if 0 <= ref < len(pieces) else 0
    if not pieces:
        return b""
    ref_db = active_db(pieces[ref][1])
    levels_in = [level for level in (active_db(p[1]) for p in pieces) if level is not None]
    for i, (_, x, info) in enumerate(pieces):
        if i == ref:
            continue
        heard = active_db(x)
        if ref_db is not None and heard is not None:              # speech level -> the carrier's
            g = max(-LEVEL_MAX_DB, min(LEVEL_MAX_DB, ref_db - heard))
            gl = 10 ** (g / 20)
            pieces[i] = (pieces[i][0], scale(x, gl), info)
            info["level_gain_db"] = round(g, 1)
    xf = int(XFADE_S * TTS_RATE)
    out = floats(pieces[0][1])
    for i in range(1, len(pieces)):
        text, x, info = pieces[i]
        x = floats(x)
        prev_text = pieces[i - 1][0]
        pause_s = pause_after(prev_text, profile)
        info.update({"after": prev_text, "next": text, "pause_s": pause_s})
        edge_n = int(EDGE_S * TTS_RATE)
        pa, pb = active_db(out[-edge_n:]), active_db(x[:edge_n])
        if pa is not None and pb is not None:                     # per-join edge level, ramped back to 0 dB
            g = max(pr["onset_min_db"], min(EDGE_MAX_DB, (pa - pb) / 2))
            r = min(len(x), int(EDGE_RAMP_S * TTS_RATE))
            x[:r] = mul(x[:r], floats([10 ** (g * (1 - t) / 20) for t in _linspace(0, 1, r)]))
            info["edge_step_db"], info["edge_gain_db"] = round(pa - pb, 1), round(g, 1)
        n = min(xf, len(out) // 2, len(x) // 2)
        ramp = _linspace(0, math.pi / 2, n)
        out[len(out) - n:] = mul(out[len(out) - n:], floats(map(math.cos, ramp)))   # equal power
        x[:n] = mul(x[:n], floats(map(math.sin, ramp)))
        fi = min(int(pr["onset_fade_s"] * TTS_RATE), len(x) // 4)
        if fi:                                                    # ease the onset in (on top of the crossfade)
            x[:fi] = mul(x[:fi], floats([q ** 0.5 for q in _linspace(0, 1, fi)]))
        start = len(out) + int(pause_s * TTS_RATE) - n
        info["at_s"] = round(start / TTS_RATE, 3)
        out += floats([0.0]) * max(0, start + len(x) - len(out))
        out[start:start + len(x)] = floats(map(add, out[start:start + len(x)], x))
        if report is not None:
            report.append(info)
    out = _fade(out)
    lv_out = active_db(out)                                      # keep the sentence as loud as its pieces were
    if levels_in and lv_out is not None and len(pieces) > 1:
        gl = 10 ** (max(-3.0, min(3.0, statistics.median(levels_in) - lv_out)) / 20)
        out = scale(out, gl)
    return _finish(out)
