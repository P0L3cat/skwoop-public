"""Music fast path: the Spotify Web API straight from the bridge.

Clear asks ("play Mr. Brightside", "play some Taylor Swift", "play Bohemian Rhapsody by Queen", "play the album
Abbey Road", "play my metal playlist") search Spotify and play the best match. Loose asks ("something for
my morning coffee", "some 90s hip hop", "something new") go to the fast model through the configured deep-model
base URL (DEEP_MODEL_BASE_URL). This module never sends an API key. The model names a few songs, the bridge
looks them up in parallel and plays them. Pause, resume, skip, previous and "what's playing" are one call each.
Anything that fails returns None and the turn goes to the deep model as before.

Token: ~/.skwoop/spotify-auth.json (SPOTIFY_AUTH_PATH), providers.spotify. When it is within REFRESH_MARGIN_S of
expiry, or Spotify answers 401, the bridge runs SPOTIFY_REFRESH_CMD. That command refreshes the token and writes
the same JSON back. The bridge never logs the token or writes it anywhere.

Live switch: touch ~/.skwoop/music-fast.off and music turns go to the deep model again, no restart.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import shlex
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from lean_http import ClientError, ClientSession, ClientTimeout

log = logging.getLogger("skwoop.music")

AUTH_PATH = Path(os.environ.get("SPOTIFY_AUTH_PATH", str(Path.home() / ".skwoop" / "spotify-auth.json")))
OFF_FLAG = Path.home() / ".skwoop" / "music-fast.off"
SPOTIFY_API = "https://api.spotify.com/v1"
_PROXY_BASE = os.environ.get("DEEP_MODEL_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
PROXY_URL = _PROXY_BASE if _PROXY_BASE.endswith("/chat/completions") else _PROXY_BASE + "/chat/completions"
PICK_MODEL = "grok-4.20-0309-non-reasoning"
REFRESH_MARGIN_S = 120          # Deep's own skew (SPOTIFY_ACCESS_TOKEN_REFRESH_SKEW_SECONDS)
DEVICE_TTL_S = 300
TASTE_TTL_S = 900
TZ = ZoneInfo("America/Chicago")


class MusicError(Exception):
    """A step of the fast path failed; the caller hands the turn to Deep."""


# ---------------------------------------------------------------- what Alex asked for


Action = Literal["pause", "resume", "next", "previous", "now_playing"]
ClearKind = Literal["any", "artist", "album", "playlist", "mine"]


@dataclass(frozen=True)
class Control:
    action: Action


@dataclass(frozen=True)
class PlayClear:
    """A named thing: a song (optionally "by" an artist), an artist, an album, or a playlist."""

    query: str
    kind: ClearKind = "any"
    artist: str = ""
    loose_ok: bool = True  # no strong match: ask the model instead of giving up


@dataclass(frozen=True)
class PlayLoose:
    """A mood, activity, genre or "something new": the model picks."""

    ask: str


MusicAsk = Control | PlayClear | PlayLoose

_LEAD = re.compile(r"^(?:(?:hey |ok |okay )?skwoop\s+)?(?:(?:can|could|would|will) you (?:please )?|please |"
                   r"i want to (?:hear|listen to) |let'?s (?:hear|listen to) )?")
_TAIL = re.compile(r"\s+(?:please|for me|on spotify|on the (?:pc|computer|speakers?)|(?:right )?now)$")
_CONTROLS: tuple[tuple[Action, re.Pattern[str]], ...] = (
    ("pause", re.compile(r"^(?:pause|stop)(?: (?:the|this) (?:music|song|track))?(?: (?:music|spotify|it))?$")),
    ("resume", re.compile(r"^(?:resume|unpause|continue|keep playing|play)(?: (?:the )?(?:music|song|spotify))?$")),
    ("next", re.compile(r"^(?:skip|next)(?: (?:this|the))?(?: (?:song|track|one))?$|^play the next (?:song|track)$")),
    ("previous", re.compile(r"^(?:previous|last|go back(?: to the)?)(?: (?:song|track))$|"
                            r"^play the (?:previous|last) (?:song|track)(?: again)?$|^go back$")),
    ("now_playing", re.compile(r"^what(?:'?s| is) (?:playing|this song|this)(?: right now| now)?$|"
                               r"^what song is (?:this|playing)(?: right now| now)?$|^who (?:is this|sings this)$")),
)
_PLAY = re.compile(r"^(?:play|shuffle|put on|(?:switch|change)(?: it| the music)? (?:to|over to)|start)\s+(.+)$")
# Asks this path leaves to Deep: adding to the queue, the liked-songs library.
_DEEP_ONLY = re.compile(r"^(?:queue|add)\b|\b(?:liked songs|my library|my likes|my favou?rites)\b")
# A mood, a moment, an activity or "you pick": the model chooses, no search first.
_LOOSE = re.compile(r"\b(?:something|anything|whatever|surprise me|you pick|your choice|random|some music|"
                    r"music (?:for|to|that)|songs? (?:for|to|that)|tunes|vibes?|mood|new music|"
                    r"(?:i )?(?:have not|haven'?t) heard|chill|relaxing|upbeat|mellow|energetic|calm|cozy|"
                    r"background|to focus|focus music|studying|workout|work out|cleaning|cooking|dinner|party|"
                    r"to sleep|bedtime|morning|evening|rainy|sunday|coffee)\b")
_GENRE = re.compile(r"^(?:some |the )?(?:(?:\d0s|nineties|eighties|seventies|sixties|old school|classic|modern|"
                    r"new|soft|hard|indie|alternative|acoustic|instrumental)\s+)*(?:jazz|hip hop|hip-hop|rap|rock|"
                    r"metal|country|classical|lo-?fi|edm|house|techno|pop|r&b|r and b|folk|blues|reggae|punk|soul|"
                    r"funk|disco|ambient|grunge|emo|christmas|oldies)(?: music| songs| playlist)?$")


def clean(text: str) -> str:
    t = " ".join(text.lower().replace("’", "'").split())
    t = re.sub(r"[.!?,]+", " ", t)
    t = " ".join(t.split())
    t = _LEAD.sub("", t, count=1)
    while (shorter := _TAIL.sub("", t)) != t:
        t = shorter

    return t.strip()


def parse_ask(text: str) -> MusicAsk | None:
    """What a router "music" turn wants, or None when it isn't one this path can do (e.g. "queue" alone)."""
    t = clean(text)
    for action, rx in _CONTROLS:
        if rx.match(t):
            return Control(action)

    m = _PLAY.match(t)
    if not m or _DEEP_ONLY.search(t):
        return None

    body = m.group(1).strip()
    if mm := re.match(r"^(?:songs by|music by|something by|anything by) (.+)$", body):
        return PlayClear(mm.group(1).strip(), "artist")
    if _LOOSE.search(body) or _GENRE.match(body):
        return PlayLoose(t)

    if mm := re.match(r"^my (.+?) playlist$|^my playlist (.+)$", body):
        return PlayClear((mm.group(1) or mm.group(2)).strip(), "mine")
    if mm := re.match(r"^(?:the )?playlist (.+)$|^(?:the )?(.+?) playlist$", body):
        return PlayClear((mm.group(1) or mm.group(2)).strip(), "playlist")
    if mm := re.match(r"^(?:the )?album (.+?)(?: by (.+))?$|^(?:the )?(.+?) album(?: by (.+))?$", body):
        return PlayClear((mm.group(1) or mm.group(3)).strip(), "album", (mm.group(2) or mm.group(4) or "").strip())
    if mm := re.match(r"^some (.+)$", body):
        return PlayClear(mm.group(1).strip(), "artist")

    body = re.sub(r"^(?:the song |the track |song )", "", body)
    if mm := re.match(r"^(.+?) by (.+)$", body):
        return PlayClear(mm.group(1).strip(), "any", mm.group(2).strip())

    return PlayClear(body)


# ---------------------------------------------------------------- matching search results


def norm(s: str) -> str:
    """Compare names the way people say them: no accents, case, punctuation, "(Remastered)" or "- Live" tails."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s.lower())
    s = re.sub(r"\s+-\s+.*$", "", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^the ", "", " ".join(s.split()))

    return s


def same(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False

    compact = na.replace(" ", "") == nb.replace(" ", "")
    close = difflib.SequenceMatcher(None, na, nb).ratio() >= 0.88
    return na == nb or compact or close


@dataclass(frozen=True)
class Track:
    uri: str
    name: str
    artists: tuple[str, ...]
    popularity: int


@dataclass(frozen=True)
class Named:
    """An artist, album or playlist: something with a context URI."""

    uri: str
    name: str
    by: str
    popularity: int
    size: int  # tracks in an album or playlist; 0 for an artist


def _str(raw: dict[Any, Any], key: str) -> str:
    v = raw.get(key)
    return v if isinstance(v, str) else ""


def _int(raw: dict[Any, Any], key: str) -> int:
    v = raw.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _items(raw: object, key: str) -> list[dict[Any, Any]]:
    """search -> {"tracks": {"items": [...]}}; playlist search can return null items."""
    if not isinstance(raw, dict):
        return []
    block = raw.get(key)
    items = block.get("items") if isinstance(block, dict) else None

    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def parse_tracks(raw: object) -> list[Track]:
    out = []
    for i in _items(raw, "tracks"):
        artists = tuple(_str(a, "name") for a in i.get("artists") or [] if isinstance(a, dict))
        if _str(i, "uri"):
            out.append(Track(_str(i, "uri"), _str(i, "name"), artists, _int(i, "popularity")))

    return out


def parse_named(raw: object, key: str) -> list[Named]:
    out = []
    for i in _items(raw, key):
        if not _str(i, "uri"):
            continue
        owner = i.get("owner")
        artists = [a for a in i.get("artists") or [] if isinstance(a, dict)]
        if artists:
            by = _str(artists[0], "name")
        elif isinstance(owner, dict):
            by = _str(owner, "display_name")
        else:
            by = ""
        tracks = i.get("tracks")
        size = _int(i, "total_tracks") or (_int(tracks, "total") if isinstance(tracks, dict) else 0)
        out.append(Named(_str(i, "uri"), _str(i, "name"), by, _int(i, "popularity"), size))

    return out


def best_track(tracks: list[Track], title: str, artist: str = "") -> Track | None:
    """The first result (Spotify's order) whose title is the one asked for, by that artist when one was named."""
    for t in tracks:
        if not same(t.name, title):
            continue
        if artist and not any(same(a, artist) or norm(artist) in norm(a) for a in t.artists):
            continue
        return t

    return None


def best_named(items: list[Named], name: str, by: str = "") -> Named | None:
    for n in items:
        if same(n.name, name) and (not by or same(n.by, by)):
            return n

    return None


# ---------------------------------------------------------------- the Spotify token


@dataclass(frozen=True)
class Token:
    value: str
    expires_at: float  # epoch seconds


def read_token(path: Path = AUTH_PATH) -> Token | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    state = raw.get("providers", {}).get("spotify") if isinstance(raw, dict) else None
    if not isinstance(state, dict):
        return None
    value, expires = state.get("access_token"), state.get("expires_at")
    if not isinstance(value, str) or not value or not isinstance(expires, str):
        return None
    try:
        at = datetime.fromisoformat(expires)
    except ValueError:
        return None

    return Token(value, (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp())


async def refresh_spotify(force: bool) -> None:
    """Run SPOTIFY_REFRESH_CMD. It refreshes the Spotify token and writes auth JSON."""
    cmd = os.environ.get("SPOTIFY_REFRESH_CMD", "").strip()
    if not cmd:
        raise MusicError("SPOTIFY_REFRESH_CMD is unset; cannot refresh the Spotify token")
    argv = shlex.split(cmd)
    env = {**os.environ, "SPOTIFY_REFRESH_FORCE": "1" if force else "0"}
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE, env=env)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except TimeoutError as exc:
        proc.kill()
        raise MusicError("Spotify refresh timed out") from exc
    if proc.returncode != 0:
        last = err.decode(errors="replace").strip().splitlines()[-1:] or ["?"]
        raise MusicError(f"Spotify refresh failed: {last[0][:160]}")


class TokenSource:
    def __init__(self, path: Path = AUTH_PATH, resolve: Callable[[bool], Awaitable[None]] = refresh_spotify,
                 clock: Callable[[], float] = time.time) -> None:
        self.path, self.resolve, self.clock = path, resolve, clock
        self._lock = asyncio.Lock()

    async def get(self, force: bool = False) -> str:
        token = read_token(self.path)
        if not force and token is not None and token.expires_at - self.clock() > REFRESH_MARGIN_S:
            return token.value

        async with self._lock:       # one refresh at a time; a second caller reads what the first saved
            token = read_token(self.path)
            if force or token is None or token.expires_at - self.clock() <= REFRESH_MARGIN_S:
                await self.resolve(force)
                token = read_token(self.path)
        if token is None:
            raise MusicError("no Spotify token in Deep's auth store")

        return token.value


# ---------------------------------------------------------------- Spotify Web API


class Spotify:
    def __init__(self, http: ClientSession, tokens: TokenSource, base: str = SPOTIFY_API,
                 device: str = "") -> None:
        self.http, self.tokens, self.base = http, tokens, base
        self.device_pref = device             # a device id or name; empty: the active Computer, else any Computer
        self._device: tuple[str, float] | None = None

    async def call(self, method: str, path: str, params: dict[str, Any] | None = None,
                   body: dict[str, Any] | None = None,
                   timeout: float = 4.0) -> object:
        for attempt in (0, 1):
            token = await self.tokens.get(force=attempt == 1)
            async with self.http.request(method, f"{self.base}{path}", params=params, json=body,
                                         headers={"Authorization": f"Bearer {token}"},
                                         timeout=ClientTimeout(total=timeout)) as r:
                if r.status == 401 and attempt == 0:
                    continue
                if r.status >= 400:
                    raise MusicError(f"spotify {method} {path} -> {r.status}")
                if r.status == 204 or not (await r.read()):
                    return None
                return await r.json()
        raise MusicError("spotify kept answering 401")

    async def device(self, fresh: bool = False) -> str:
        if not fresh and self._device is not None and time.monotonic() - self._device[1] < DEVICE_TTL_S:
            return self._device[0]
        raw = await self.call("GET", "/me/player/devices")
        devices = [d for d in (raw.get("devices") if isinstance(raw, dict) else None) or [] if isinstance(d, dict)]
        pick = next((d for d in devices if self.device_pref and self.device_pref in (d.get("id"), d.get("name"))), None)
        pick = pick or next((d for d in devices if d.get("is_active") and d.get("type") == "Computer"), None)
        pick = pick or next((d for d in devices if d.get("type") == "Computer"), None)
        device_id = pick.get("id") if isinstance(pick, dict) else None
        if not isinstance(device_id, str):
            raise MusicError("no Spotify computer device is online")
        self._device = (device_id, time.monotonic())

        return device_id

    async def search(self, q: str, types: str, limit: int = 5) -> object:
        # no market=from_token: that needs user-read-private, which Deep's Spotify login does not ask for (403)
        return await self.call("GET", "/search", {"q": q, "type": types, "limit": limit})

    async def play(self, body: dict[str, Any]) -> None:
        try:
            await self.call("PUT", "/me/player/play", {"device_id": await self.device()}, body)
        except MusicError as exc:
            if "-> 404" not in str(exc):
                raise
            await self.call("PUT", "/me/player/play", {"device_id": await self.device(fresh=True)}, body)

    async def player(self) -> dict[str, Any]:
        raw = await self.call("GET", "/me/player")
        return raw if isinstance(raw, dict) else {}

    async def queue(self, uri: str) -> None:
        await self.call("POST", "/me/player/queue", {"uri": uri, "device_id": await self.device()})


# ---------------------------------------------------------------- the model's picks (loose asks)

PICK_PROMPT = """You choose music for Alex's Spotify, played on his PC speakers. Reply with JSON only:
{"say": "<one short spoken line, at most 9 words, no song titles>", "songs": [["title", "artist"], ...]}
Give 8 songs that really exist on Spotify, as released (no covers, no live versions). Fit the ask: its mood, \
activity and time of day. Mix well-known songs with a few fresher ones; vary the artists. "Something new" or \
"haven't heard": no chart hits at all; lesser-known songs (deep cuts, smaller or rising artists) close to his \
taste, by artists not in his recent listening. Focus or study: mostly instrumental, steady, no big vocals. \
A genre or era: the defining songs of it. The spoken line sounds like a friend putting a record on, \
e.g. "Mellow jazz for the coffee."."""


@dataclass(frozen=True)
class Picks:
    say: str
    songs: tuple[tuple[str, str], ...]


def parse_picks(text: str) -> Picks:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise MusicError("model reply has no JSON")
    try:
        raw = json.loads(m.group(0))
    except ValueError as exc:
        raise MusicError("model reply is not JSON") from exc
    say = raw.get("say") if isinstance(raw, dict) else None
    songs = raw.get("songs") if isinstance(raw, dict) else None
    if not isinstance(say, str) or not isinstance(songs, list):
        raise MusicError("model reply lacks say/songs")
    pairs = tuple((s[0].strip(), s[1].strip()) for s in songs
                  if isinstance(s, list) and len(s) == 2 and all(isinstance(x, str) and x.strip() for x in s))
    if len(pairs) < 3:
        raise MusicError("model named fewer than 3 songs")

    return Picks(" ".join(say.split())[:90], pairs[:10])


async def pick_songs(http: ClientSession, ask: str, taste: list[str], url: str = PROXY_URL,
                     model: str = PICK_MODEL, now: datetime | None = None) -> Picks:
    now = now or datetime.now(TZ)
    context = f"It's {now:%A} {now:%-I:%M %p}."
    if taste:
        context += " His recent listening: " + ", ".join(taste[:10]) + "."
    body = {"model": model, "max_tokens": 300, "temperature": 0.8,
            "messages": [{"role": "system", "content": PICK_PROMPT},
                         {"role": "user", "content": f"{context}\nHe said: {ask}"}]}
    # No Authorization header: the configured endpoint attaches its own credentials.
    async with http.post(url, json=body, timeout=ClientTimeout(total=6)) as r:
        if r.status >= 400:
            raise MusicError(f"pick model -> {r.status}")
        payload = await r.json()
        data = payload if isinstance(payload, dict) else {}
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MusicError("pick model reply has no content") from exc
    if not isinstance(text, str):
        raise MusicError("pick model content is not text")

    return parse_picks(text)


# ---------------------------------------------------------------- one music turn


@dataclass(frozen=True)
class Played:
    say: str      # the spoken reply
    how: str      # control | track | artist | album | playlist | picks (for the turn profile)
    detail: str   # what started, for the log


def _by(artists: tuple[str, ...]) -> str:
    return artists[0] if artists else "someone"


class Music:
    def __init__(self, http: ClientSession, spotify: Spotify, pick_url: str = PROXY_URL,
                 pick_model: str = PICK_MODEL, off_flag: Path = OFF_FLAG) -> None:
        self.http, self.spotify = http, spotify
        self.pick_url, self.pick_model, self.off_flag = pick_url, pick_model, off_flag
        self._taste: tuple[list[str], float] = ([], 0.0)

    @classmethod
    def from_env(cls, http: ClientSession) -> Music:
        spotify = Spotify(http, TokenSource(), device=os.environ.get("MUSIC_DEVICE", ""))
        return cls(http, spotify, os.environ.get("MUSIC_PICK_URL") or PROXY_URL,
                   os.environ.get("MUSIC_PICK_MODEL") or PICK_MODEL)

    async def handle(self, text: str) -> Played | None:
        """Do the music ask and return what to say, or None to hand the turn to Deep."""
        if self.off_flag.exists():
            return None
        ask = parse_ask(text)
        if ask is None:
            return None
        try:
            if isinstance(ask, Control):
                return await self.control(ask.action)
            if isinstance(ask, PlayLoose):
                return await self.loose(ask.ask)
            return await self.clear(ask, text)
        except (TimeoutError, MusicError, ClientError) as exc:
            log.warning("music fast path gave up (%s): Deep takes %r", exc, text)
            return None

    async def control(self, action: Action) -> Played:
        s = self.spotify
        if action == "now_playing":
            p = await s.player()
            tracks = parse_tracks({"tracks": {"items": [p.get("item")]}})
            if not tracks or not p.get("is_playing"):
                return Played("Nothing's playing right now.", "control", action)
            t = tracks[0]
            return Played(f"This is {t.name} by {_by(t.artists)}.", "control", f"{action} {t.uri}")
        device = {"device_id": await s.device()}
        if action == "pause":
            await s.call("PUT", "/me/player/pause", device)
            return Played("Paused.", "control", action)
        if action == "resume":
            await s.call("PUT", "/me/player/play", device)
            return Played("Back on.", "control", action)
        if action == "next":
            await s.call("POST", "/me/player/next", device)
            return Played("Skipping.", "control", action)
        await s.call("POST", "/me/player/previous", device)
        return Played("Going back.", "control", action)

    async def clear(self, ask: PlayClear, heard: str) -> Played | None:
        """Search and play the named thing. No strong match: let the model pick (or Deep, when it can't)."""
        hit = await self._find(ask)
        if hit is not None:
            return hit
        if ask.loose_ok:
            log.info("music: no strong match for %r, the model picks", ask.query)
            return await self.loose(clean(heard))
        return None

    async def _find(self, ask: PlayClear) -> Played | None:
        s = self.spotify
        if ask.kind == "mine":
            raw = await s.call("GET", "/me/playlists", {"limit": 50})
            mine = parse_named({"playlists": raw}, "playlists")
            pl = best_named(mine, ask.query) or next((p for p in mine if norm(ask.query) in norm(p.name)), None)
            if pl is None:
                return None
            await s.play({"context_uri": pl.uri})
            return Played(f"Playing your {pl.name} playlist.", "playlist", pl.uri)
        if ask.kind == "playlist":
            pls = parse_named(await s.search(ask.query, "playlist"), "playlists")
            pl = best_named(pls, ask.query) or next((p for p in pls if p.size >= 15), None)
            if pl is None:
                return None
            await s.play({"context_uri": pl.uri})
            return Played(f"Playing {pl.name}.", "playlist", pl.uri)
        if ask.kind == "album":
            q = f"album:{ask.query}" + (f" artist:{ask.artist}" if ask.artist else "")
            al = best_named(parse_named(await s.search(q, "album"), "albums"), ask.query, ask.artist)
            if al is None:
                return None
            await s.play({"context_uri": al.uri})
            return Played(f"Playing {al.name} by {al.by}.", "album", al.uri)
        if ask.kind == "artist":
            ar = best_named(parse_named(await s.search(ask.query, "artist"), "artists"), ask.query)
            if ar is None:
                return None
            await s.play({"context_uri": ar.uri})
            return Played(f"Playing {ar.name}.", "artist", ar.uri)

        q = f"track:{ask.query} artist:{ask.artist}" if ask.artist else ask.query
        raw = await s.search(q, "track" if ask.artist else "track,artist")
        track = best_track(parse_tracks(raw), ask.query, ask.artist)
        artist = None if ask.artist else best_named(parse_named(raw, "artists"), ask.query)
        if artist is not None and (track is None or artist.popularity >= 50):
            await s.play({"context_uri": artist.uri})       # "play Muse" is the band; "play Africa" the song
            return Played(f"Playing {artist.name}.", "artist", artist.uri)
        if track is None:
            return None
        await s.play({"uris": [track.uri]})

        return Played(f"Playing {track.name} by {_by(track.artists)}.", "track", track.uri)

    async def loose(self, ask: str) -> Played:
        picks = await pick_songs(self.http, ask, await self.taste(), self.pick_url, self.pick_model)
        found = await asyncio.gather(*(self._lookup(title, artist) for title, artist in picks.songs),
                                     return_exceptions=True)
        tracks = [t for t in found if isinstance(t, Track)]
        uris = list(dict.fromkeys(t.uri for t in tracks))
        if len(uris) < 3:
            raise MusicError(f"only {len(uris)} of {len(picks.songs)} picks are on Spotify")
        await self.spotify.play({"uris": uris})
        names = "; ".join(f"{t.name} - {_by(t.artists)}" for t in tracks)
        log.info("music picks for %r: %s", ask, names)
        say = picks.say if picks.say.endswith((".", "!", "?")) else picks.say + "."

        return Played(say, "picks", names)

    async def _lookup(self, title: str, artist: str) -> Track | None:
        raw = await self.spotify.search(f"track:{title} artist:{artist}", "track", limit=3)
        return best_track(parse_tracks(raw), title, artist)

    async def taste(self) -> list[str]:
        """Artists from his recent listening (cached 15 min), so the model knows his taste. Never blocks a turn
        for long: a slow or failed read just means no taste line."""
        artists, at = self._taste
        if artists and time.monotonic() - at < TASTE_TTL_S:
            return artists
        try:
            raw = await asyncio.wait_for(self.spotify.call("GET", "/me/player/recently-played", {"limit": 30}), 0.8)
        except (TimeoutError, MusicError, ClientError):
            return artists
        items = raw.get("items") if isinstance(raw, dict) else None
        names: list[str] = []
        for item in items if isinstance(items, list) else []:
            track = item.get("track") if isinstance(item, dict) else None
            for t in parse_tracks({"tracks": {"items": [track]}}):
                names.extend(t.artists[:1])
        self._taste = (list(dict.fromkeys(names))[:12], time.monotonic())

        return self._taste[0]
