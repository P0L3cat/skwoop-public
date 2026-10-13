"""Script-first house intents for the pod.

HA script + cached ack PCM. LLM is not used on a hit.
Secrets stay in env; never logged.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol
from zoneinfo import ZoneInfo

import json

from lean_http import ClientSession, ClientTimeout

if TYPE_CHECKING:
    from skwoop_lib import SkwoopLibrary

log = logging.getLogger("skwoop.house")

TZ = ZoneInfo("America/Chicago")
ACK_DIR = Path.home() / ".skwoop" / "acks"
ENDPOINT_PATH = Path.home() / ".skwoop" / "endpoint"
INTENTS_PATH = Path(__file__).resolve().parent.parent / "intents.json"
_PLAY_SHOW = re.compile(r"^\s*(play|watch)\s+(.+?)[\s.!?,]*$", re.I)   # STT ends with "." -- not part of the title
_NOT_SHOW = frozenset({"tv", "the tv", "apple tv", "the apple tv", "it"})
# Oct 4: "play X" is Spotify (router DEEP music) unless Alex says TV or show: "play X on TV", "play the show X".
# "watch X" is always the TV. Not a bare "show" first word: "play Show Me How" is a song.
_TV_FRAME = re.compile(r"^(?:the (?:tv )?(?:show|movie)|tv show) | on (?:the )?(?:living room )?(?:apple )?"
                       r"(?:tv|television)$", re.I)
_NOT_A_TITLE = re.compile(r"\b(music|songs?|playlists?|albums?|radio|podcasts?|spotify)\b", re.I)   # not a TV ask
# Oct 2: SkwoopOS library lines. "Putting on <title>." (tv_title, ~7k titles pre-rendered) for an Apple TV deep
# link or a Netflix title; Netflix then gets the follow-up (we can open the app, not drive it to the title).
NETFLIX_LINE = "Netflix is up. You'll have to pick it from there."
PUTTING_ON = "Putting on {title}."
_LOOKUP_DIR = Path.home() / ".skwoop" / "media-lookup"


ShowMode = Literal["APPLE", "NETFLIX", "SEARCH"]


@dataclass(frozen=True)
class ShowLookup:
    mode: ShowMode
    urls: tuple[str, ...]                # deep links (tv.apple.com/..., videos://search?...) or the Netflix app id


def parse_show_lookup(raw: object) -> ShowLookup:
    """atv_lookup.resolve() -> ShowLookup. Anything unexpected = a plain search."""
    if not isinstance(raw, tuple) or len(raw) != 2:
        return ShowLookup("SEARCH", ())

    mode, urls = raw
    if mode not in ("APPLE", "NETFLIX", "SEARCH") or not isinstance(urls, list):
        return ShowLookup("SEARCH", ())

    return ShowLookup(mode, tuple(u for u in urls if isinstance(u, str)))


Resolver = Callable[[str], ShowLookup]


def atv_resolver(lookup_dir: Path | None = None) -> Resolver:
    """atv_lookup.resolve under ATV_LOOKUP_DIR (default ~/.skwoop/media-lookup). A failure is a plain search."""
    where = lookup_dir or Path(_env("ATV_LOOKUP_DIR") or str(_LOOKUP_DIR))

    def resolve(title: str) -> ShowLookup:
        if not where.is_dir():
            return ShowLookup("SEARCH", ())
        if str(where) not in sys.path:
            sys.path.insert(0, str(where))

        try:
            from atv_lookup import resolve as _resolve

            return parse_show_lookup(_resolve(title))
        except Exception as e:  # noqa: BLE001 - the lookup is best effort
            log.warning("title lookup failed: %s", type(e).__name__)

            return ShowLookup("SEARCH", ())

    return resolve


def _plain_title(title: str) -> str:
    return title if any(c.isupper() for c in title) else title.title()


@dataclass(frozen=True)
class ShowContext:
    """What "play <title>" needs: a title resolver and the library's title casing. Tests pass their own."""
    resolve: Resolver
    display_title: Callable[[str], str] = _plain_title


def show_context(library: SkwoopLibrary | None = None, resolve: Resolver | None = None) -> ShowContext:
    return ShowContext(resolve or atv_resolver(), library.display_title if library is not None else _plain_title)


@dataclass(frozen=True)
class Hit:
    spoken: str
    ack_id: str
    script: str | None
    data: dict[str, Any] | None = field(default=None)
    follow: str | None = None           # a second line said right after (the Netflix follow-up)
    mode: ShowMode | None = None        # play_show: APPLE | NETFLIX | SEARCH (atv_lookup.resolve)
    deeplink: str | None = None         # play_show: the first deep link atv_lookup found (tv.apple.com/..., videos://)

    @property
    def text(self) -> str:
        return f"{self.spoken} {self.follow}" if self.follow else self.spoken


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _before_10am() -> bool:
    return datetime.now(TZ).hour < 10


def endpoint() -> str:
    try:
        t = ENDPOINT_PATH.read_text().strip().lower()
    except Exception:
        return "skwoop"
    return "grokbot" if t == "grokbot" else "skwoop"


def set_endpoint(name: str) -> None:
    ENDPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENDPOINT_PATH.write_text(("grokbot" if name == "grokbot" else "skwoop") + "\n")


def _maybe_endpoint(iid: str) -> None:
    if iid == "endpoint_grokbot":
        set_endpoint("grokbot")
    elif iid == "endpoint_skwoop":
        set_endpoint("skwoop")


def _load_intents() -> list[dict[str, Any]]:
    data = json.loads(INTENTS_PATH.read_text())
    return list(data.get("intents") or [])


def play_show_hit(text: str, show: ShowContext | None = None) -> Hit | None:
    m = _PLAY_SHOW.match(" ".join(str(text).split()))
    if not m:
        return None

    title = " ".join(m.group(2).split())
    framed = _TV_FRAME.sub("", title).strip()
    if m.group(1).lower() == "play" and framed == title:
        return None
    title = framed
    if not title or _norm(title) in _NOT_SHOW or _NOT_A_TITLE.search(title):
        return None

    show = show or show_context()
    found = show.resolve(title)
    link = next((u for u in found.urls if "://" in u), None)
    said = PUTTING_ON.format(title=show.display_title(title))
    data = {"query": title}                       # the HA script's contract is unchanged
    if found.mode == "NETFLIX":
        return Hit(said, "tv_netflix_stuck", "script.play_on_living_room_tv", data, follow=NETFLIX_LINE,
                   mode="NETFLIX", deeplink=None)

    if found.mode == "APPLE":
        return Hit(said, "tv_play_show", "script.play_on_living_room_tv", data, mode="APPLE", deeplink=link)

    return Hit("Playing.", "tv_play_show", "script.play_on_living_room_tv", data, mode="SEARCH", deeplink=link)


def match(text: str, show: ShowContext | None = None) -> Hit | None:
    t = _norm(text)
    if not t:
        return None
    if (hit := play_show_hit(text, show)) is not None:
        return hit
    for row in _load_intents():
        phrases = [_norm(p) for p in (row.get("any") or []) if p]
        if not any(p and p in t for p in phrases):
            continue
        iid = str(row["id"])
        if iid == "bedroom_on" and _before_10am():
            return Hit("Bedroom stays off until after 10.", "blocked_bedroom", None)
        return Hit(str(row.get("ack") or "Okay."), iid, row.get("script"))
    return None


def hit_from_id(iid: str, heard: str = "", show: ShowContext | None = None) -> Hit | None:
    if iid == "blocked_bedroom":
        return Hit("Bedroom stays off until after 10.", "blocked_bedroom", None)
    if iid == "tv_play_show":
        return play_show_hit(heard, show)
    for row in _load_intents():
        if str(row.get("id")) == iid:
            if iid == "bedroom_on" and _before_10am():
                return Hit("Bedroom stays off until after 10.", "blocked_bedroom", None)
            return Hit(str(row.get("ack") or "Okay."), iid, row.get("script"))
    return None


async def _post_ha(http: ClientSession, url: str, headers: dict[str, str], script: str,
                   payload: dict[str, Any]) -> None:
    try:
        async with http.post(url, json=payload, headers=headers,
                             timeout=ClientTimeout(total=90)) as r:
            if r.status >= 400:
                log.error("HA HTTP %s for %s", r.status, script)
                return
        log.info("HA %s", script)
    except Exception as e:
        log.error("HA call failed: %s", type(e).__name__)


async def _fire(http: ClientSession | None, hit: Hit) -> Hit:
    """Carry out a hit. match() and hit_from_id() only look things up; switching the endpoint happens here."""
    _maybe_endpoint(hit.ack_id)
    if not hit.script:
        return hit
    url = _env("HA_URL") or _env("HASS_URL") or "http://127.0.0.1:8123"
    token = _env("HA_TOKEN") or _env("HASS_TOKEN")
    if http is None or not token:
        log.error("HA link missing")
        return Hit("Couldn't do that.", "fail", None)
    domain, _, obj = hit.script.partition(".")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    endpoint = f"{url.rstrip('/')}/api/services/{domain}/{obj}"
    asyncio.create_task(_post_ha(http, endpoint, headers, hit.script, dict(hit.data or {})))
    return hit


async def apply_id(http: ClientSession | None, iid: str, heard: str = "",
                   show: ShowContext | None = None) -> Hit | None:
    hit = hit_from_id(iid, heard, show)
    if hit is None:
        return None
    return await _fire(http, hit)


async def apply(http: ClientSession | None, text: str, show: ShowContext | None = None) -> Hit | None:
    hit = match(text, show)
    if hit is None:
        return None
    return await _fire(http, hit)


class AckSpeaker(Protocol):
    """What the acks need from the bridge's TTS (providers.TTS; tests pass a fake)."""
    provider: str
    last_backend: str
    library: SkwoopLibrary | None

    async def synth(self, text: str, mood: str | None = None) -> bytes: ...


async def _one(tts: AckSpeaker, text: str, mood: str | None, cache_dir: Path) -> bytes:
    """A library clip when there is one (free, instant); else live TTS once, cached per voice + text."""
    if tts.library is not None:
        r = tts.library.render(text, mood)
        if r is not None:
            return r.pcm

    from providers import voice_key

    voice_dir = cache_dir / ("v-" + hashlib.sha1(voice_key(tts.provider).encode()).hexdigest()[:10])
    path = voice_dir / (hashlib.sha1(text.encode()).hexdigest()[:16] + ".pcm")
    if path.exists() and path.stat().st_size > 64:
        return path.read_bytes()

    audio = await tts.synth(text, mood=mood)
    if audio and not tts.last_backend.endswith("fallback"):   # don't pin an xAI fallback take as the cached ack
        voice_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)

    return audio or b""


async def ack_pcm(tts: AckSpeaker, hit: Hit, mood: str | None = "happy", cache_dir: Path = ACK_DIR) -> bytes:
    """The spoken ack (+ follow-up line): SkwoopOS clips first, live TTS cached once otherwise. The old per-ack xAI
    cache files in ACK_DIR are left alone (rollback keeps working)."""
    gap = b"\x00\x00" * int(24000 * 0.25)
    parts = [await _one(tts, hit.spoken, mood, cache_dir)]
    if hit.follow:
        parts.append(await _one(tts, hit.follow, "idle", cache_dir))

    return gap.join(p for p in parts if p)


async def warm(tts: AckSpeaker, cache_dir: Path = ACK_DIR) -> None:
    rows = _load_intents()
    seen = {
        "fail": "Couldn't do that.",
        "blocked_bedroom": "Bedroom stays off until after 10.",
        "tv_play_show": "Playing.",
        "tv_netflix_stuck": NETFLIX_LINE,
    }
    for row in rows:
        seen[str(row["id"])] = str(row.get("ack") or "Okay.")
    for ack_id, spoken in seen.items():
        try:
            await ack_pcm(tts, Hit(spoken, ack_id, None), cache_dir=cache_dir)
        except Exception as e:
            log.warning("ack warm %s failed: %s", ack_id, type(e).__name__)
