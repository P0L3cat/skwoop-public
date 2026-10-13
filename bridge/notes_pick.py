"""The notes search a deep turn would run first, done on the pod before that turn starts.

For "what's in my notes about the dishwasher", the voice prompt would otherwise read a wiki index under
~/.skwoop/notes (SKWOOP_NOTES_DIR) and any extra files in SKWOOP_NOTES_LOOSE. The scope is a few dozen small text
files, so a keyword search here is enough. Its result goes out as "Already looked up on the pod" facts
(skwoop_turn.facts_blob), which the prompt already tells the model to use instead of fetching again.

Reads only. A write ("add a note about ...") gets no pre-pick: Deep follows the wiki's SCHEMA.md for those.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("skwoop.notes")

HOME = Path.home()
DEFAULT_WIKI = HOME / ".skwoop" / "notes"
DEFAULT_LOOSE: tuple[Path, ...] = ()
MAX_FILE_BYTES = 200_000
MAX_FACTS = 6               # skwoop_turn.facts_blob keeps 6 facts of 400 characters
FACT_CHARS = 400
MIN_NEAR = 4                # a note word this long that starts the asked word is a near match ("dish" ~ "dishwasher")

_WRITE = re.compile(r"^(please )?(write|add|save|put|make|create|note|jot|update|edit|change|delete|remove|"
                    r"append|log|record)\b|\b(make|take|write|add|save) (a |another )?note\b|\bnote (that|down)\b")
_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*")
_STOP = frozenset("""
a about above after again all also am an and any anything are as at be been being both but by can could did do does
doing down during each few for from had has have having he her here hers him his how i i'd i'm if in into is it it's
its just me might more most much my myself no nor not now of off on once only or other our ours out over own please
really same she should so some something stuff tell than that that's the their them then there these they this those
through to too under until up us very was we were what what's whats when where which while who whom why will with
would you your yours anything everything thing things say says said mention mentions mentioned written wrote write
know got get find look see check show give read
note notes noted file files folder folders document documents doc docs wiki page pages pdf pdfs spreadsheet
spreadsheets list lists asap grocery
""".split())


@dataclass(frozen=True)
class NoteFile:
    label: str              # how the answer names it: "family wiki: Appliances (home/appliances.md)", "~/ASAP.txt"
    text: str


@dataclass(frozen=True)
class Hit:
    label: str
    lines: tuple[str, ...]
    exact: bool


def is_read_ask(text: str) -> bool:
    return _WRITE.search(_norm(text)) is None


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def search_terms(text: str) -> tuple[str, ...]:
    """The words of the ask that name what to look for: "what's in my notes about the dishwasher" -> ("dishwasher",)."""
    words = (w.strip("'-") for w in _WORD.findall(_norm(text)))
    picked = [w.removesuffix("'s") for w in words if len(w) > 2 and w not in _STOP]

    return tuple(dict.fromkeys(w for w in picked if w not in _STOP))


def _forms(term: str) -> tuple[str, ...]:
    """The term and its plural/singular, as whole words."""
    forms = {term, term + "s", term + "es"}
    if term.endswith("ies"):
        forms.add(term[:-3] + "y")
    elif term.endswith("es"):
        forms.add(term[:-2])
    elif term.endswith("s"):
        forms.add(term[:-1])

    return tuple(sorted(forms))


def _exact_rx(terms: tuple[str, ...]) -> re.Pattern[str]:
    alts = "|".join(re.escape(f) for t in terms for f in _forms(t))

    return re.compile(rf"\b({alts})\b", re.IGNORECASE)


def _near(line: str, terms: tuple[str, ...]) -> bool:
    """A word in the line that is the start of an asked word: "Dish disposal pods" for "dishwasher"."""
    words = {w for w in _WORD.findall(line.lower()) if len(w) >= MIN_NEAR}

    return any(t != w and t.startswith(w) for t in terms for w in words)


def read_scope(wiki: Path, loose: Iterable[Path]) -> tuple[NoteFile, ...]:
    files: list[NoteFile] = []
    for path in sorted(wiki.rglob("*.md")):
        text = _read(path)
        if text is not None:
            title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
            files.append(NoteFile(f"family wiki: {title} ({path.relative_to(wiki)})", text))

    for path in loose:
        text = _read(path)
        if text is not None:
            files.append(NoteFile(f"~/{path.name}" if path.parent == HOME else str(path), text))

    return tuple(files)


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None

        return path.read_text(errors="replace")
    except OSError as e:
        log.warning("notes pre-pick: %s unreadable (%s)", path, type(e).__name__)

        return None


def search(files: tuple[NoteFile, ...], terms: tuple[str, ...]) -> tuple[Hit, ...]:
    """Files with an asked word (exact, most matching lines first), then files with only a near match."""
    exact_rx = _exact_rx(terms)
    exact: list[Hit] = []
    near: list[Hit] = []
    for f in files:
        lines = [" ".join(ln.split()) for ln in f.text.splitlines() if ln.strip()]
        hits = [ln for ln in lines if exact_rx.search(ln)]
        if hits:
            exact.append(Hit(f.label, tuple(hits), True))
            continue

        close = [ln for ln in lines if _near(ln, terms)]
        if close:
            near.append(Hit(f.label, tuple(close), False))

    exact.sort(key=lambda h: -len(h.lines))

    return (*exact, *near)


def facts(text: str, files: tuple[NoteFile, ...]) -> list[str] | None:
    """The facts for Deep, or None when this is not a notes read with something to look for."""
    if not is_read_ask(text):
        return None

    terms = search_terms(text)
    if not terms or not files:
        return None

    hits = search(files, terms)
    exact = [h for h in hits if h.exact]
    near = [h for h in hits if not h.exact]
    wiki_pages = sum(1 for f in files if f.label.startswith("family wiki:"))
    loose = ", ".join(f.label for f in files if not f.label.startswith("family wiki:"))
    head = (f"Notes search, already done on the pod over the whole notes scope ({wiki_pages} family wiki pages, "
            f"{loose}) for: {', '.join(terms)}. ")
    if exact:
        head += f"Matches in: {'; '.join(h.label for h in exact)}."
    else:
        head += "No note mentions it."

    if near:
        head += f" Close but not the same word in: {'; '.join(h.label for h in near)}."

    out = [head[:FACT_CHARS]]
    for h in hits[:MAX_FACTS - 1]:
        kind = "" if h.exact else "close match only, "
        out.append(f"{h.label} ({kind}lines): " + " / ".join(h.lines)[:FACT_CHARS])

    return out


def facts_for(text: str) -> list[str] | None:
    """facts() over the real scope (SKWOOP_NOTES_WIKI, SKWOOP_NOTES_LOOSE); blocking file reads, run it off the loop."""
    wiki = Path(os.environ.get("SKWOOP_NOTES_WIKI") or DEFAULT_WIKI).expanduser()
    loose_env = os.environ.get("SKWOOP_NOTES_LOOSE")
    loose = tuple(Path(p).expanduser() for p in loose_env.split(",")) if loose_env else DEFAULT_LOOSE

    return facts(text, read_scope(wiki, loose))
