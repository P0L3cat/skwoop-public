"""SkwoopOS clip library: the Oct 1-2 priority library in the try-8 voice (ElevenLabs ,
eleven_v4_turbo). On disk: manifest.json + clips/<id>.wav + clips/<id>.align.json sidecars (priority-plan/manifest.py).

Lookup order for one sentence (SkwoopLibrary.render):
  1. whole  - a pre-rendered whole sentence or line clip with exactly these words (case, punctuation, digits agnostic)
  2. b2     - a b2 template (room_pct, room_color, weather_now) assembled from carrier + slot pieces with
              stitch.join(profile="overlap"); pieces without sidecars fall back to the v3 join inside stitch
  3. None   - the caller speaks it with live TTS (providers.TTS: credit-guarded ElevenLabs, then xAI)

SKWOOP_LIBRARY_DIR (default ~/.skwoop/skwoop-lib) holds manifest.json and clips/. SKWOOP_LIBRARY=0 turns it off.
Moods: sleep/sleepy -> calm, neutral/thinking -> idle. Non-verbal lines (hums, moans) are never picked.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict, cast

import stitch
from clip_index import ClipIndex, IndexCorrupt, IndexMissing, IndexStale
from dsp import from_floats_trunc

log = logging.getLogger("skwoop.skwoop_lib")

DEFAULT_DIR = Path.home() / ".skwoop" / "skwoop-lib"
MOOD_ALIASES = {"sleep": "calm", "sleepy": "calm", "neutral": "idle", "thinking": "idle", "listening": "idle",
                "speaking": "idle", "": "idle"}
MOOD_FALLBACK = ("idle", "happy", "calm", "alert", "sad")
NONVERBAL = re.compile(r"^\s*(?:(?:m+|hm+|mm+|m+-?hm+|uh+-?huh|ah+|oo+h*|zz+|hum+|moan\w*|yawn\w*)[\s,.!?~…-]*)+$",
                       re.I)
PCM_CACHE = 48                        # decoded clips kept in memory; true LRU
PATH_CACHE = 4096                     # paths outlive the PCM LRU; not one Path per library clip

TemplateMode = Literal["whole", "b2"]


class ManifestError(ValueError):
    """manifest.json is missing a field or has the wrong type."""


@dataclass(frozen=True, slots=True)
class Clip:
    file: str
    text: str


@dataclass(frozen=True, slots=True)
class Template:
    id: str
    sentence: str
    moods: tuple[str, ...]
    mode: TemplateMode


@dataclass(frozen=True, slots=True)
class LineClip:
    text: str
    id: str


@dataclass(frozen=True, slots=True)
class Manifest:
    voice: str
    model: str
    templates: dict[str, Template]
    whole: dict[str, str]                    # "tid|mood|{json values}" -> clip id
    slots: dict[str, str]                    # "kind|left word|right word|mood|value" -> clip id
    carriers: dict[str, str]                 # "tid|part index|mood" -> clip id
    lines: dict[str, tuple[LineClip, ...]]   # "kind|mood" -> line clips
    clips: dict[str, Clip]


# ------------------------------------------------------------------ parsing (the only place that sees raw JSON)
def _str(raw: object, where: str) -> str:
    if not isinstance(raw, str):
        raise ManifestError(f"{where}: expected a string")

    return raw


def _mood(raw: object, where: str) -> str:
    return sys.intern(_str(raw, where))


def _clip_file(raw: object, where: str) -> str:
    """Keep the one JSON path string. Interning directory parts shares the repeated prefix."""
    file = _str(raw, where)
    for part in file.split("/")[:-1]:
        sys.intern(part)

    return file


def _obj(raw: object, where: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: expected an object")

    return {_str(k, where): v for k, v in raw.items()}


def _str_map(raw: object, where: str) -> dict[str, str]:
    return {k: _str(v, f"{where}.{k}") for k, v in _obj(raw, where).items()}


def parse_manifest(raw: object) -> Manifest:
    top = _obj(raw, "manifest")
    templates: dict[str, Template] = {}
    for tid, t in _obj(top.get("templates"), "templates").items():
        tt = _obj(t, f"templates.{tid}")
        moods = tt.get("moods")
        if not isinstance(moods, list):
            raise ManifestError(f"templates.{tid}.moods: expected a list")

        mode = tt.get("mode")
        templates[tid] = Template(tid, _str(tt.get("sentence"), f"templates.{tid}.sentence"),
                                  tuple(_mood(m, f"templates.{tid}.moods") for m in moods),
                                  "b2" if mode == "b2" else "whole")

    lines: dict[str, tuple[LineClip, ...]] = {}
    for key, items in _obj(top.get("lines"), "lines").items():
        if not isinstance(items, list):
            raise ManifestError(f"lines.{key}: expected a list")

        lines[key] = tuple(LineClip(_str(_obj(i, key).get("text"), key), _str(_obj(i, key).get("id"), key))
                           for i in items)

    clips: dict[str, Clip] = {}
    for cid, c in _obj(top.get("clips"), "clips").items():
        cc = _obj(c, f"clips.{cid}")
        _mood(cc.get("mood"), f"clips.{cid}.mood")   # not stored; index keys carry mood
        clips[cid] = Clip(_clip_file(cc.get("file"), f"clips.{cid}.file"), _str(cc.get("text"), f"clips.{cid}.text"))

    return Manifest(voice=_str(top.get("voice", ""), "voice"), model=_str(top.get("model", ""), "model"),
                    templates=templates, whole=_str_map(top.get("whole"), "whole"),
                    slots=_str_map(top.get("slots"), "slots"), carriers=_str_map(top.get("carriers"), "carriers"),
                    lines=lines, clips=clips)


def load_manifest(root: Path) -> Manifest:
    """OSError: no manifest. ManifestError / ValueError: a broken one."""
    return parse_manifest(json.loads((root / "manifest.json").read_text()))


# ------------------------------------------------------------------ results
@dataclass(frozen=True)
class Found:
    how: Literal["whole", "b2"]
    mood: str
    clip_id: str | None = None                      # how == "whole"
    template: str | None = None                     # how == "b2"
    values: dict[str, str] = field(default_factory=dict)
    pieces: tuple[str, ...] = ()                    # how == "b2": clip ids in sentence order


@dataclass(frozen=True)
class Rendered:
    pcm: bytes                                      # s16le mono @ 24 kHz
    found: Found
    ms: float
    v3_fallback: bool                               # b2 only: a piece lacked its sidecar, joined as v3


@dataclass(frozen=True)
class PickedLine:
    kind: str
    mood: str
    text: str
    id: str
    pcm: bytes


class LibraryStatus(TypedDict):
    enabled: bool
    ready: bool
    dir: str
    voice: str | None
    model: str | None
    clips: int
    sentences: int
    b2_templates: list[str]
    join: str
    hits: dict[str, int]
    error: str | None


def speak_mood(mood: str | None) -> str:
    m = (mood or "").strip().lower()

    return MOOD_ALIASES.get(m, m)


# ------------------------------------------------------------------ text normalisation
def norm(text: str) -> str:
    """'It's 72°F and sunny!' -> "it's seventy two degrees and sunny" (the key both sides are compared on)."""
    t = str(text).replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"(-?\d+)\s*%", lambda m: stitch.pct_words(int(m.group(1))), t)
    t = re.sub(r"(-?\d+)\s*(?:°\s*[fF]?|degrees?\b)", lambda m: stitch.temp_words(int(m.group(1))), t)
    t = re.sub(r"(?<![\d:])-?\d+(?![\d:])", lambda m: stitch.num_words(int(m.group(0))), t)

    return stitch._words_key(t)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text)


@dataclass(frozen=True)
class B2Pattern:
    template: str
    rx: re.Pattern[str]
    slots: tuple[str, ...]


@dataclass
class ManifestTables:
    """Builder-only tables. The speak path does not construct this."""

    manifest: Manifest
    whole: dict[str, tuple[tuple[str, str], ...]]
    slot_values: dict[str, dict[str, str]]
    titles: dict[str, str]
    b2: list[B2Pattern]


def _put_whole(whole: dict[str, tuple[tuple[str, str], ...]], key: str, mood: str, cid: str, *,
               overwrite: bool) -> None:
    """One tuple per sentence, not a dict: most sentences have two moods and a dict is mostly header."""
    mood = sys.intern(mood)
    cur = whole.get(key)
    pair = (mood, cid)
    if cur is None:
        whole[key] = (pair,)
        return

    for i, (have, old) in enumerate(cur):
        if have == mood:
            if overwrite and old != cid:
                whole[key] = (*cur[:i], pair, *cur[i + 1:])
            return

    whole[key] = (*cur, pair)


def _b2_pattern(tpl: Template) -> B2Pattern:
    rx: list[str] = []
    slots: list[str] = []
    for p in re.split(r"(\{\w+\})", tpl.sentence):
        if p.startswith("{"):
            slots.append(p[1:-1])
            rx.append(f"(?P<{p[1:-1]}>.+?)")
        elif norm(p):
            rx.append(re.escape(norm(p)))

    return B2Pattern(tpl.id, re.compile("^" + " ".join(rx) + "$"), tuple(slots))


def build_manifest_tables(root: Path) -> ManifestTables:
    """The tables tools/build_clip_index.py writes. Not called on the speak path."""
    m = load_manifest(root)
    whole: dict[str, tuple[tuple[str, str], ...]] = {}
    slot_values: dict[str, dict[str, str]] = {}
    titles: dict[str, str] = {}
    b2: list[B2Pattern] = []
    for key, cid in m.whole.items():
        tid, mood, vals = key.split("|", 2)
        tpl = m.templates.get(tid)
        if tpl is None or cid not in m.clips:
            continue

        values = _str_map(json.loads(vals), f"whole.{key}")
        _put_whole(whole, norm(tpl.sentence.format(**values)), mood, cid, overwrite=True)
        if tid == "tv_title":
            titles[norm(values["title"])] = values["title"]

    for key, items in m.lines.items():
        mood = key.split("|", 1)[1]
        for it in items:
            if it.id in m.clips and not NONVERBAL.match(it.text):
                _put_whole(whole, norm(it.text), mood, it.id, overwrite=False)

    for key in m.slots:
        kind, _, _, _, value = key.split("|", 4)
        slot_values.setdefault(kind, {})[norm(value)] = value

    for tpl in m.templates.values():
        if tpl.mode == "b2":
            b2.append(_b2_pattern(tpl))

    m.whole.clear()
    return ManifestTables(m, whole, slot_values, titles, b2)


def _quoted_field(text: str, key: str) -> str | None:
    """One JSON string near the start of manifest.json. The speak path does not parse the clip table."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if match is None:
        return None

    try:
        loaded = json.loads('"' + match.group(1) + '"')
    except json.JSONDecodeError:
        return None

    # SAFETY: json.loads is typed as Any; a JSON string is str.
    value = cast(object, loaded)
    if not isinstance(value, str):
        return None

    return value


def _voice_model(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("rb") as fh:
            blob = fh.read(4096)
    except OSError:
        return None, None

    text = blob.decode("utf-8", "replace")
    return _quoted_field(text, "voice"), _quoted_field(text, "model")


class SkwoopLibrary:
    def __init__(self, root: Path | str | None = None, enabled: bool | None = None):
        self.root = Path(root or os.environ.get("SKWOOP_LIBRARY_DIR") or DEFAULT_DIR).expanduser()
        if enabled is None:
            enabled = os.environ.get("SKWOOP_LIBRARY", "1").lower() not in ("0", "false", "no", "off")

        self.enabled = enabled
        self.idx: ClipIndex | None = None
        self.b2: list[B2Pattern] = []
        self.voice: str | None = None
        self.model: str | None = None
        self._n_clips = 0
        self._n_sentences = 0
        self._texts: dict[str, str] | None = None
        self._paths: OrderedDict[str, Path] = OrderedDict()
        self.error = ""
        self.stats = {"whole": 0, "b2": 0, "miss": 0}
        self._pcm: OrderedDict[str, bytes] = OrderedDict()
        self._last: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        if self.enabled:
            self.load()

    # ---------------------------------------------------------- loading
    def load(self) -> None:
        t0 = time.monotonic()
        previous = self.idx
        self.idx = None
        self.b2 = []
        self.voice = None
        self.model = None
        self._n_clips = 0
        self._n_sentences = 0
        self._texts = None
        self._paths = OrderedDict()
        if previous is not None:
            previous.close()

        try:
            idx = ClipIndex.open(self.root / "clips.idx", self.root / "manifest.json")
        except (IndexMissing, IndexStale, IndexCorrupt) as exc:
            self.error = type(exc).__name__
            log.error("SkwoopOS library %s not ready (%s): tools/build_clip_index.py %s", self.root, self.error,
                      self.root)

            return

        self.idx, self.error = idx, ""
        self.b2 = [B2Pattern(row.template, re.compile(row.pattern), row.slots) for row in idx.b2]
        self.voice, self.model = _voice_model(self.root / "manifest.json")
        self._n_clips = idx.n_clips
        self._n_sentences = idx.n_sentences
        log.info("SkwoopOS library: %d clips, %d whole sentences, %d b2 templates (%s, %.2fs)", self._n_clips,
                 self._n_sentences, len(self.b2), self.voice, time.monotonic() - t0)

    @property
    def ready(self) -> bool:
        return self.enabled and self.idx is not None

    def describe(self) -> LibraryStatus:
        return {"enabled": self.enabled, "ready": self.ready, "dir": str(self.root),
                "voice": self.voice, "model": self.model, "clips": self._n_clips, "sentences": self._n_sentences,
                "b2_templates": [p.template for p in self.b2], "join": stitch.JOIN, "hits": dict(self.stats),
                "error": self.error or None}

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _pick_mood(want: str | None, have: list[str]) -> str | None:
        if not have:
            return None

        for m in (speak_mood(want), *MOOD_FALLBACK):
            if m in have:
                return m

        return have[0]

    def _index(self) -> ClipIndex:
        if self.idx is None:
            raise LookupError("SkwoopOS library not loaded")

        return self.idx

    def _path(self, cid: str) -> Path:
        # PCM LRU is 48, so a replayed clip resolves its path again. Paths stay capped.
        hit = self._paths.get(cid)
        if hit is not None:
            self._paths.move_to_end(cid)
            return hit

        info = self._index().clip(cid)
        if info is None:
            raise KeyError(cid)

        path = self.root / info[0]
        self._paths[cid] = path
        self._paths.move_to_end(cid)
        if len(self._paths) > PATH_CACHE:
            self._paths.popitem(last=False)

        return path

    def _clip_texts(self) -> dict[str, str]:
        """Transcripts for a b2 join. Kind 11 has no text column; read them when a sentence is played."""
        cached = self._texts
        if cached is not None:
            return cached

        out: dict[str, str] = {}
        # Prefer the slim clip-texts.json (id -> text) written by build_clip_index; fall back to
        # manifest.json only when that file is missing (do not keep a failed empty cache).
        text_path = self.root / "clip-texts.json"
        man_path = self.root / "manifest.json"
        try:
            raw_path = text_path if text_path.is_file() else man_path
            loaded = json.loads(raw_path.read_text())
        except (OSError, ValueError) as e:
            raise LookupError(f"clip texts unreadable: {type(e).__name__}") from e

        # SAFETY: json.loads is typed as Any; the isinstance checks narrow it.
        raw = cast(object, loaded)
        if not isinstance(raw, dict):
            raise LookupError("clip texts not an object")
        # clip-texts.json is {id: text}; manifest.json is {clips: {id: {text, ...}}}.
        clips = raw.get("clips") if "clips" in raw else raw
        if isinstance(clips, dict):
            for cid, row in clips.items():
                if not isinstance(cid, str):
                    continue
                if isinstance(row, str):
                    out[cid] = row
                elif isinstance(row, dict):
                    text = row.get("text")
                    if isinstance(text, str):
                        out[cid] = text
        if not out:
            raise LookupError("clip texts empty")
        self._texts = out
        return out

    def clip_pcm(self, cid: str) -> bytes:
        with self._lock:
            hit = self._pcm.get(cid)
            if hit is not None:
                self._pcm.move_to_end(cid)

                return hit

        x = stitch._fade(stitch._trim(stitch._load(self._path(cid))))
        pcm = from_floats_trunc(x, 32767.0)
        with self._lock:
            self._pcm[cid] = pcm
            self._pcm.move_to_end(cid)
            if len(self._pcm) > PCM_CACHE:
                self._pcm.popitem(last=False)

        return pcm

    # ---------------------------------------------------------- lookup
    def find(self, text: str, mood: str | None = None) -> Found | None:
        """Lookup order: whole -> b2. None = say it live."""
        idx = self.idx
        if idx is None or not self.enabled or not text or NONVERBAL.match(text):
            return None

        key = norm(text)
        if not key:
            return None

        hits = idx.whole(key)
        if hits:
            pick = self._pick_mood(mood, [m for m, _cid in hits])
            if pick is not None:
                cid = next(c for m, c in hits if m == pick)
                return Found("whole", pick, clip_id=cid)

        for pat in self.b2:
            found = self._find_b2(pat, key, mood)
            if found is not None:
                return found

        return None

    def _find_b2(self, pat: B2Pattern, key: str, mood: str | None) -> Found | None:
        idx = self._index()
        hit = pat.rx.match(key)
        if not hit:
            return None

        values: dict[str, str] = {}
        for slot in pat.slots:
            value = idx.slot_value(slot, hit.group(slot))
            if value is None:
                return None

            values[slot] = value

        tpl = idx.template(pat.template)
        if tpl is None:
            return None

        tpl_moods = list(tpl.moods)
        first = self._pick_mood(mood, tpl_moods)
        for md in dict.fromkeys((first, *tpl_moods)):
            if md is None:
                continue

            pieces = self._b2_pieces(pat.template, md, values)
            if pieces:
                return Found("b2", md, template=pat.template, values=values, pieces=pieces)

        return None

    def _b2_pieces(self, tid: str, mood: str, values: dict[str, str]) -> tuple[str, ...] | None:
        idx = self._index()
        tpl = idx.template(tid)
        if tpl is None:
            return None

        parts = re.split(r"(\{\w+\})", tpl.sentence)
        out: list[str] = []
        for i, p in enumerate(parts):
            if p.startswith("{"):
                k = p[1:-1]
                left, right = _words(parts[i - 1]), _words(parts[i + 1])
                cid = idx.slot(f"{k}|{left[-1] if left else None}|{right[0] if right else None}|{mood}|{values[k]}")
            else:
                if not _words(p) or (0 < i < len(parts) - 1 and len(_words(p)) == 1):
                    continue        # one-word links ride on the overlap of their neighbours

                cid = idx.carrier(f"{tid}|{i // 2}|{mood}")
            if cid is None or idx.clip(cid) is None:
                return None

            out.append(cid)

        return tuple(out)

    def render(self, text: str, mood: str | None = None, report: list[dict[str, object]] | None = None
               ) -> Rendered | None:
        """Lookup order whole -> b2 -> None (speak it live)."""
        t0 = time.perf_counter()
        found = self.find(text, mood)
        if found is None or self.idx is None:
            self.stats["miss"] += 1

            return None

        fallback = False
        try:
            if found.clip_id is not None:
                pcm = self.clip_pcm(found.clip_id)
            else:
                rep: list[dict[str, object]] = report if report is not None else []
                texts = self._clip_texts()
                clips: list[tuple[str, Path, dict[str, float]]] = [
                    (texts[c], self._path(c), {}) for c in found.pieces]
                pcm = stitch.join(clips, report=rep, profile="overlap")
                fallback = any("fallback" in j for j in rep)
        except (OSError, ValueError, KeyError) as e:
            # Fail closed: a library hit that cannot be played must NOT fall through to live ElevenLabs
            #. The caller sees None and TTS skips paid synth when the library is required.
            log.error("library %s for %r failed (%s): no live TTS fallback", found.how, text[:60], type(e).__name__)
            self.stats["miss"] += 1
            self.error = f"render:{type(e).__name__}"
            raise LookupError(f"library render failed: {type(e).__name__}") from e

        self.stats[found.how] += 1

        return Rendered(pcm, found, round((time.perf_counter() - t0) * 1000, 1), fallback)

    def has_lines(self, kind: str) -> bool:
        return self.idx is not None and self.idx.has_lines(kind)

    def pick_line(self, kind: str, mood: str | None = None) -> PickedLine | None:
        """A random line clip of `kind` ("ack", "tv_ok", ...) in the closest mood; never a non-verbal one."""
        idx = self.idx
        if idx is None:
            return None

        have: list[str] = []
        asked = speak_mood(mood)
        if idx.lines(f"{kind}|{asked}") is not None:
            have.append(asked)

        for candidate in MOOD_FALLBACK:
            if candidate not in have and idx.lines(f"{kind}|{candidate}") is not None:
                have.append(candidate)

        md = self._pick_mood(mood, have)
        if md is None:
            return None

        group = idx.lines(f"{kind}|{md}")
        if group is None:
            return None

        items = [pair for pair in group if not NONVERBAL.match(pair[0])]
        if not items:
            return None

        last = self._last.get((kind, md))
        it = random.choice([pair for pair in items if pair[0] != last] or items)
        self._last[(kind, md)] = it[0]

        return PickedLine(kind, md, it[0], it[1], self.clip_pcm(it[1]))

    def display_title(self, title: str) -> str:
        """The library's casing for a spoken title ("the blacklist" -> "The Blacklist"), else title case."""
        idx = self.idx
        hit = idx.title(norm(title)) if idx is not None else None
        if hit:
            return hit

        return title if any(c.isupper() for c in title) else title.title()
