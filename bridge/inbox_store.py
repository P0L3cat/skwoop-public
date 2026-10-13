"""Skwoop - voice inbox ("Skwoop has a message").

Bots queue a message; the bridge renders it to audio right away (configured TTS) and
the pod plays it only when Alex taps. Stored under INBOX_DIR (~/.skwoop/inbox/):
  index.json          metadata for every item
  <id>.wav            24 kHz mono 16-bit audio ("Message from X." + the message)

HTTP (mounted by skwoop_bridge.py, and by status_store.py when run standalone):
  POST   /inbox          Bearer STATUS_WRITE_KEY
         JSON       {"from":"Skwoop","text":"Build is green.","room":"kitchen"}   -> TTS at queue time
         multipart  from=..., text=... (optional caption), room=..., audio=<file>  -> pre-recorded
         "room" is optional: without it the message is for every pod (shared inbox).
  GET    /inbox          metadata only: {"count","unread","items":[...]}
                         (message text is included only with the write key)
  DELETE /inbox/{id}     Bearer STATUS_WRITE_KEY

Played items are deleted INBOX_PLAYED_TTL_S (default 24 h) after they were played.

Shared by every pod: playing a message anywhere marks it played everywhere, and a per-message
lock (claim/release) stops two pods from playing the same message at the same time.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import wave
from collections.abc import Awaitable, Callable
from datetime import datetime, UTC
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from dsp import resample
from lean_http import Request, Response, RouteDef, web
from providers import TTS_RATE, pcm_to_wav, wav_to_pcm
from rooms import Rooms, norm_room

log = logging.getLogger("skwoop.inbox")

# Raw index.json at the load boundary. Same alias as house_tools.Json.
Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None

InboxItem = TypedDict("InboxItem", {
    "id": str,
    "from": str,
    "text": str,
    "created_ts": float,
    "played": bool,
    "played_ts": float | None,
    "duration_s": float,
    "source": str,
    "room": str | None,
})


class InboxSummary(TypedDict):
    unread: int
    latest_from: str | None
    latest_id: str | None
    latest_at: str | None


InboxPublic = TypedDict("InboxPublic", {
    "id": str,
    "from": str,
    "created_at": str | None,
    "played": bool,
    "played_at": str | None,
    "duration_s": float,
    "source": str,
    "room": str | None,
    "playing": bool,
    "text": NotRequired[str],
})

MAX_TEXT = 1000          # characters per message
MAX_FROM = 32
MAX_UPLOAD = 8 << 20     # bytes
MAX_AUDIO_S = 180
MAX_ITEMS = 50
GAP_MS = 350             # silence between the spoken preface and the message

Synth = Callable[[str], Awaitable[bytes]]


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _silence(ms: int) -> bytes:
    return b"\x00\x00" * (TTS_RATE * ms // 1000)


def split_sentences(text: str, max_len: int = 400) -> list[str]:
    """Chunk long text for TTS providers with per-request limits."""
    parts, cur = [], ""
    for s in re.split(r"(?<=[.!?])\s+", text.strip()):
        if cur and len(cur) + len(s) + 1 > max_len:
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        parts.append(cur)
    return parts


class InboxStore:
    def __init__(self, path: str | None = None, synth: Synth | None = None, rooms: Rooms | None = None,
                 on_change: Callable[[], None] | None = None):
        self.dir = Path(path or os.environ.get("INBOX_DIR", "~/.skwoop/inbox")).expanduser()
        self.played_ttl_s = int(os.environ.get("INBOX_PLAYED_TTL_S", str(24 * 3600)))
        self.preface = os.environ.get("INBOX_PREFACE", "1") == "1"
        self.write_key = os.environ.get("STATUS_WRITE_KEY", "")
        self.synth = synth           # async text -> PCM @ TTS_RATE (set by the bridge)
        self.synth_parts: Callable[..., Awaitable[bytes]] | None = None
        self.rooms = rooms or Rooms()
        self.on_change = on_change   # StatusStore.changed: bumps the feed rev, wakes long-polls
        self.items: dict[str, InboxItem] = {}
        self.locks: dict[str, str] = {}   # item id -> owner (a voice session) while it plays
        self._lock = asyncio.Lock()
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        try:
            data = cast(dict[str, Json], json.loads((self.dir / "index.json").read_text()))
            rows = cast(list[InboxItem], data.get("items", []))
            self.items = {i["id"]: i for i in rows if i.get("id")}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("inbox index unreadable (%s); starting empty", e)

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".index-")
        with os.fdopen(fd, "w") as f:
            json.dump({"items": list(self.items.values())}, f)
        os.replace(tmp, self.dir / "index.json")

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def audio_path(self, item_id: str) -> Path:
        return self.dir / f"{item_id}.wav"

    # ---------- queue ----------
    async def add(self, sender: str, text: str = "", pcm: bytes | None = None, source: str = "tts",
                  room: str | None = None) -> InboxItem:
        """Queue a message. pcm=None -> render `text` with TTS now (fail fast, not at play time)."""
        room = norm_room(room)
        if not self.rooms.valid_target(room):
            raise ValueError(f"unknown room {room!r}; known: {sorted(self.rooms.known())}")
        sender = (sender or "Skwoop").strip()[:MAX_FROM] or "Skwoop"
        text = (text or "").strip()[:MAX_TEXT]
        if pcm is None and not text:
            raise ValueError("text or audio required")
        synth = self.synth
        preface_text = f"New text from {sender}."   # the SkwoopOS library has it for the family names
        if pcm is None:
            if synth is None:
                raise LookupError("TTS is not configured on this server; upload audio instead")
            if self.synth_parts is not None:       # body read with previous_text/next_text context (one run)
                pcm = await self.synth_parts(split_sentences(text), gap_s=0.2,
                                             context_before=preface_text if self.preface else None)
            else:
                chunks = [await synth(c) for c in split_sentences(text)]
                pcm = _silence(200).join(c for c in chunks if c)
            if not pcm:
                raise RuntimeError("TTS returned no audio")
        if self.preface and synth is not None:
            preface = await synth(preface_text)
            if preface:
                pcm = preface + _silence(GAP_MS) + pcm
        dur = len(pcm) / 2 / TTS_RATE
        if dur > MAX_AUDIO_S + 10:
            raise ValueError(f"audio longer than {MAX_AUDIO_S}s")
        async with self._lock:
            self.expire()
            if len(self.items) >= MAX_ITEMS:
                played = sorted((i for i in self.items.values() if i["played"]), key=lambda i: i["created_ts"])
                if not played:
                    raise OverflowError("inbox full")
                self._remove(played[0]["id"])
            now = time.time()
            item_id = f"m-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
            self.dir.mkdir(parents=True, exist_ok=True)
            self.audio_path(item_id).write_bytes(pcm_to_wav(pcm, TTS_RATE))
            item = {"id": item_id, "from": sender, "text": text, "created_ts": now, "played": False,
                    "played_ts": None, "duration_s": round(dur, 2), "source": source, "room": room}
            self.items[item_id] = item
            self._save()
        self._changed()
        log.info("queued %s from %s for %s (%.1fs, %s)", item_id, sender, room or "all pods", dur, source)
        return item

    def _remove(self, item_id: str) -> bool:
        self.locks.pop(item_id, None)
        item = self.items.pop(item_id, None)
        if item is None:
            return False
        try:
            self.audio_path(item_id).unlink()
        except FileNotFoundError:
            pass
        return True

    def delete(self, item_id: str) -> bool:
        ok = self._remove(item_id)
        if ok:
            self._save()
            self._changed()
        return ok

    def expire(self, now: float | None = None) -> int:
        now = now or time.time()
        old = [i["id"] for i in self.items.values()
               if i["played"] and (i.get("played_ts") or 0) + self.played_ttl_s < now]
        for item_id in old:
            self._remove(item_id)
        if old:
            self._save()  # played items don't change any badge: no rev bump needed
        return len(old)

    # ---------- playback ----------
    @staticmethod
    def _for(item: InboxItem, room: str | None) -> bool:
        """room=None means "any pod" (widget/admin view, or a pod with no room filter)."""
        return room is None or item.get("room") is None or item.get("room") == room

    def unread(self, room: str | None = None) -> list[InboxItem]:
        return sorted((i for i in self.items.values() if not i["played"] and self._for(i, room)),
                      key=lambda i: i["created_ts"])

    def next_unread(self, room: str | None = None) -> InboxItem | None:
        u = self.unread(room)
        return u[0] if u else None

    def claim(self, room: str | None, owner: str) -> tuple[InboxItem | None, str | None]:
        """Lock the oldest unread message this room may play that no other pod is playing.

        Returns (item, None), or (None, owner_of_a_busy_message) when every candidate is
        already playing somewhere else, or (None, None) when there is nothing to play.
        Single-threaded asyncio: check-and-set here cannot interleave.
        """
        busy_owner = None
        for item in self.unread(room):
            holder = self.locks.get(item["id"])
            if holder is None or holder == owner:
                self.locks[item["id"]] = owner
                return item, None
            busy_owner = busy_owner or holder
        return None, busy_owner

    def release(self, item_id: str, owner: str) -> None:
        if self.locks.get(item_id) == owner:
            del self.locks[item_id]

    def load_pcm(self, item_id: str) -> bytes:
        pcm, rate = wav_to_pcm(self.audio_path(item_id).read_bytes())
        return resample(pcm, rate, TTS_RATE)

    def mark_played(self, item_id: str) -> None:
        item = self.items.get(item_id)
        if item and not item["played"]:
            item["played"], item["played_ts"] = True, time.time()
            self._save()
            self._changed()  # every pod's badge updates on its (long-)poll

    # ---------- read ----------
    def summary(self, room: str | None = None) -> InboxSummary:
        """Goes into GET /status as "inbox" (the pod learns about messages from its status poll)."""
        self.expire()
        u = self.unread(room)
        latest = u[-1] if u else None
        return {"unread": len(u), "latest_from": latest["from"] if latest else None,
                "latest_id": latest["id"] if latest else None,
                "latest_at": _iso(latest["created_ts"]) if latest else None}

    def public(self, item: InboxItem, with_text: bool) -> InboxPublic:
        out = {"id": item["id"], "from": item["from"], "created_at": _iso(item["created_ts"]),
               "played": item["played"], "played_at": _iso(item.get("played_ts")),
               "duration_s": item["duration_s"], "source": item["source"], "room": item.get("room"),
               "playing": item["id"] in self.locks}
        if with_text:
            out["text"] = item["text"]
        return out

    # ---------- HTTP ----------
    def _authorized(self, request: Request) -> bool:
        if not self.write_key:
            return False
        return hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {self.write_key}")

    def routes(self) -> list[RouteDef]:
        async def post_inbox(request: Request) -> Response:
            if not self._authorized(request):
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                if request.content_type.startswith("multipart/"):
                    sender, text, room, pcm = await self._read_multipart(request)
                    item = await self.add(sender, text, pcm=pcm, source="upload", room=room)
                else:
                    body = await request.json()
                    item = await self.add(str(body.get("from", "Skwoop")), str(body.get("text", "")),
                                          room=body.get("room"))
            except (ValueError, json.JSONDecodeError, wave.Error) as e:
                return web.json_response({"error": str(e)}, status=400)
            except LookupError as e:
                return web.json_response({"error": str(e)}, status=503)
            except OverflowError as e:
                return web.json_response({"error": str(e)}, status=429)
            except Exception as e:  # TTS provider failures etc.
                log.error("queue failed: %s", e)
                return web.json_response({"error": f"could not render audio: {type(e).__name__}"}, status=502)
            return web.json_response({"ok": True, "item": self.public(item, True),
                                      "inbox": self.summary(item.get("room"))})

        async def get_inbox(request: Request) -> Response:
            self.expire()
            full = self._authorized(request)
            items = sorted(self.items.values(), key=lambda i: i["created_ts"], reverse=True)
            return web.json_response({"count": len(items), "unread": len(self.unread()),
                                      "items": [self.public(i, full) for i in items]},
                                     headers={"Cache-Control": "no-store"})

        async def delete_item(request: Request) -> Response:
            if not self._authorized(request):
                return web.json_response({"error": "unauthorized"}, status=401)
            return web.json_response({"removed": self.delete(request.match_info["id"]), "inbox": self.summary()})

        return [web.post("/inbox", post_inbox), web.get("/inbox", get_inbox),
                web.delete("/inbox/{id}", delete_item)]

    async def _read_multipart(self, request: Request) -> tuple[str, str, str | None, bytes]:
        sender, text, room, data, name = "Skwoop", "", None, b"", ""
        reader = await request.multipart()
        async for part in reader:
            if part.name == "from":
                sender = (await part.text())[:MAX_FROM]
            elif part.name == "text":
                text = (await part.text())[:MAX_TEXT]
            elif part.name == "room":
                room = (await part.text())[:24] or None
            elif part.name in ("audio", "file"):
                name = part.filename or ""
                buf = bytearray()
                while chunk := await part.read_chunk(1 << 16):
                    buf.extend(chunk)
                    if len(buf) > MAX_UPLOAD:
                        raise ValueError("audio upload too large (8 MB max)")
                data = bytes(buf)
        if not data:
            raise ValueError("multipart upload needs an 'audio' file part")
        return sender, text, room, await decode_audio(data, name)


async def decode_audio(data: bytes, filename: str = "") -> bytes:
    """Any upload -> 16-bit mono PCM @ TTS_RATE. WAV natively; mp3/m4a/ogg via ffmpeg or afconvert."""
    if data[:4] == b"RIFF":
        pcm, rate = wav_to_pcm(data)
        return resample(pcm, rate, TTS_RATE)
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in" + (os.path.splitext(filename)[1] or ".bin"))
        dst = os.path.join(d, "out.wav")
        with open(src, "wb") as f:
            f.write(data)
        if shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", src, "-ac", "1", "-ar", str(TTS_RATE),
                   "-sample_fmt", "s16", dst]
        elif shutil.which("afconvert"):  # macOS built-in
            cmd = ["afconvert", "-f", "WAVE", "-d", f"LEI16@{TTS_RATE}", "-c", "1", src, dst]
        else:
            raise ValueError("only WAV uploads are supported here (install ffmpeg for mp3/m4a/ogg)")
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        if await p.wait() != 0 or not os.path.exists(dst):
            raise ValueError("could not decode the uploaded audio")
        with open(dst, "rb") as f:
            pcm, rate = wav_to_pcm(f.read())
    return resample(pcm, rate, TTS_RATE)
