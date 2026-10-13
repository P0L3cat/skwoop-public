"""Skwoop - tiny team status feed (shared by every pod, and later an iPhone widget).

  GET    /status                -> feed (read key required, see "Read key" below)
         Device-Id: <mac>       -> the feed as that pod sees it (its room's alerts/messages)
         ?since=<rev>&wait=20   -> long-poll: answers as soon as anything changes (max 25 s)
  POST   /status                -> upsert one bot   (Bearer STATUS_WRITE_KEY)
         {"name":"Skwoop","state":"busy","detail":"deploying bridge"}
  POST   /alert                 -> set an alert     (Bearer STATUS_WRITE_KEY)
         {"text":"Build failed on the bridge host","level":"warn","ttl_s":3600,"room":"kitchen"}
         "room" is optional: without it every pod shows the alert
  DELETE /alert[?room=kitchen]  -> clear all alerts, or one room's   (Bearer STATUS_WRITE_KEY)
  DELETE /status/{name}         -> remove a bot     (Bearer STATUS_WRITE_KEY)
  POST/GET/DELETE /inbox        -> voice inbox      (see inbox_store.py)

Feed shape:
  {"v":1,"rev":42,"updated_at":"2026-09-25T18:30:00Z","summary":"Skwoop busy | 3 bots ok",
   "room":"kitchen" | null,
   "counts":{"ok":3,"busy":1,"error":0,"offline":0},
   "alert":{"id":"a-1727289000","text":"...","level":"warn","room":null,"at":"...","expires_at":"..."} | null,
   "alerts":[...every alert this reader should see, newest first...],
   "bots":[{"name":"Skwoop","state":"busy","detail":"...","last_seen":"...","stale":false,"kind":"bot"}],
   "inbox":{"unread":2,"latest_from":"Skwoop","latest_id":"m-...","latest_at":"..."},
   "mood":"idle" | "thinking" | "sad" | "alert" | "sleep",
   "next":{"title":"Dentist","start":"2026-10-03T15:00:00-05:00","all_day":false,"calendar":"Family"} | null}

mood is Skwoop's face, picked the way the pod picks its face: a warn/error alert -> alert;
Skwoop's own bot entry offline -> sleep, error -> sad, busy -> thinking; otherwise idle (also when Skwoop has no
entry). next is the next calendar item from the bridge's calendar prefetch (null when there is none or no calendar).

  GET    /face.png              -> the current mood's face as a 240x240 PNG (faces/<mood>.png); read key required

Read key (Oct 3 2026): GET /status and GET /face.png answer 401 without STATUS_READ_KEY, sent as
"Authorization: Bearer <key>" or "?key=<key>" (the pods get it inside the OTA status_url). The write key also
reads, and so does the pod device token (Bearer SKWOOP_DEVICE_TOKEN; Oct 4 2026, for Pod Studio, the desktop sim). A
status_url polled as "...?key=<key>?since=N" (a second "?" instead of "&") is read as key + since. Refused reads are
logged as "read refused" (peer, path, auth scheme, query names; never the values), once a minute per peer + path.
No STATUS_READ_KEY configured = reads are refused (fail closed), like writes without STATUS_WRITE_KEY.

States: ok | busy | error | offline. A bot that has not posted for STATUS_STALE_S
(default 900 s) is reported as offline with "stale": true. Each pod appears as a bot named
"<room> pod" (kind "pod") while it polls.

Run standalone:  python status_store.py   (serves on STATUS_PORT, default 8004)
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import tempfile
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

if TYPE_CHECKING:
    from inbox_store import InboxStore, InboxSummary

from lean_http import (
    Application,
    ClientSession,
    FileResponse,
    Request,
    Response,
    RouteDef,
    StreamResponse,
    web,
)

from rooms import Rooms, norm_room

log = logging.getLogger("skwoop.status")

VALID_STATES = {"ok", "busy", "error", "offline"}
STATE_ALIASES = {
    "idle": "ok", "ready": "ok", "online": "ok", "healthy": "ok", "green": "ok",
    "working": "busy", "running": "busy", "thinking": "busy",
    "failed": "error", "down": "error", "red": "error",
}
MAX_BOTS = 16
ALL = "*"          # alert scope for "every pod"
MAX_WAIT_S = 25
FACES_DIR = Path(__file__).resolve().parent / "faces"

Mood = Literal["idle", "thinking", "sad", "alert", "sleep"]
MOODS: tuple[Mood, ...] = ("idle", "thinking", "sad", "alert", "sleep")


class NextItem(TypedDict):
    title: str
    start: str          # ISO 8601 with offset; a date ("2026-10-04") when all_day
    all_day: bool
    calendar: str


# Raw file JSON at the load boundary. Same alias as house_tools.Json.
Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None


class BotRecord(TypedDict):
    name: str
    state: str
    detail: str
    last_seen_ts: float
    kind: str


class AlertRecord(TypedDict):
    id: str
    text: str
    level: str
    room: str | None
    at_ts: float
    expires_ts: float


class BotView(TypedDict):
    name: str
    state: str
    detail: str
    last_seen: str | None
    stale: bool
    kind: str


class AlertView(TypedDict):
    id: str
    text: str
    level: str
    room: str | None
    at: str | None
    expires_at: str | None


class Counts(TypedDict):
    ok: int
    busy: int
    error: int
    offline: int


class Feed(TypedDict):
    v: int
    rev: int
    updated_at: str | None
    room: str | None
    summary: str
    counts: Counts
    alert: AlertView | None
    alerts: list[AlertView]
    bots: list[BotView]
    inbox: InboxSummary
    mood: Mood
    next: NextItem | None


def no_next() -> NextItem | None:
    return None


STATE_MOOD: dict[str, Mood] = {"ok": "idle", "busy": "thinking", "error": "sad", "offline": "sleep"}


def pick_mood(skwoop_state: str | None, alerting: bool) -> Mood:
    """alerting = a warn/error alert is up (an info alert, like a finished timer, keeps the face calm)."""
    if alerting:
        return "alert"

    return STATE_MOOD.get(skwoop_state or "ok", "idle")


def _same(got: str, want: str) -> bool:
    """Constant-time compare that also takes non-ASCII input (str compare_digest raises on it)."""
    return hmac.compare_digest(got.encode(), want.encode())


def split_key(raw: str) -> tuple[str, dict[str, str]]:
    """"K?since=3&wait=25" (a second "?" instead of "&") -> ("K", {"since": "3", "wait": "25"})."""
    key, sep, tail = raw.partition("?")
    extra: dict[str, str] = {}
    if sep:
        for part in tail.split("&"):
            name, _, value = part.partition("=")
            if name in ("since", "wait", "room"):
                extra[name] = value
    return key, extra


def unauthorized_read() -> Response:
    return web.json_response({"error": "unauthorized"}, status=401, headers={"WWW-Authenticate": "Bearer"})


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class StatusStore:
    def __init__(self, path: str | None = None, stale_s: int | None = None, rooms: Rooms | None = None,
                 read_key: str | None = None, device_token: str | None = None):
        self.path = Path(path or os.environ.get("STATUS_FILE", "~/.skwoop/status.json")).expanduser()
        self.stale_s = stale_s or int(os.environ.get("STATUS_STALE_S", "900"))
        self.write_key = os.environ.get("STATUS_WRITE_KEY", "")
        self.read_key = os.environ.get("STATUS_READ_KEY", "") if read_key is None else read_key
        self.device_token = (os.environ.get("SKWOOP_DEVICE_TOKEN", "").strip() if device_token is None
                             else device_token)
        self._refused_logged: dict[tuple[str, str], float] = {}
        self.rooms = rooms or Rooms()
        self.bots: dict[str, BotRecord] = {}
        self.alerts: dict[str, AlertRecord] = {}  # "*" or a room name
        self.updated_at = time.time()
        self.rev = 1
        self.inbox: InboxStore | None = None  # attached by the bridge (adds "inbox" to the feed)
        self.next_item = no_next  # the bridge plugs in its calendar prefetch (HouseTools.next_event)
        self.device_seen: dict[str, float] = {}  # device id -> last time it polled or connected
        self._changed: asyncio.Condition | None = None
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        try:
            data = cast(dict[str, Json], json.loads(self.path.read_text()))
            rows = cast(list[BotRecord], data.get("bots", []))
            self.bots = {b["name"]: b for b in rows if b.get("name") and b["name"] != "pod"}
            saved = data.get("alerts")
            self.alerts = cast(dict[str, AlertRecord], saved) if saved else (
                {ALL: cast(AlertRecord, data["alert"])} if data.get("alert") else {})
            self.updated_at = cast(float, data.get("updated_at_ts", time.time()))
            self.rev = int(cast(int, data.get("rev", 1)))
        except FileNotFoundError:
            pass
        except Exception as e:  # corrupt file: start fresh
            log.warning("status file unreadable (%s); starting empty", e)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"bots": list(self.bots.values()), "alerts": self.alerts,
                "updated_at_ts": self.updated_at, "rev": self.rev}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".status-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def changed(self, save: bool = True) -> None:
        """Bump the revision and wake every long-polling pod. Also called by the inbox."""
        self.rev += 1
        self.updated_at = time.time()
        if save:
            self._save()
        if self._changed is not None:
            cond = self._changed

            async def _notify() -> None:
                async with cond:
                    cond.notify_all()
            try:
                asyncio.get_running_loop().create_task(_notify())
            except RuntimeError:
                pass  # no loop (tests calling the store directly)

    # ---------- mutations ----------
    def upsert_bot(self, name: str, state: str, detail: str = "") -> BotRecord:
        name = name.strip()[:32]
        state = STATE_ALIASES.get(state.strip().lower(), state.strip().lower())
        if state not in VALID_STATES:
            raise ValueError(f"state must be one of {sorted(VALID_STATES)}")
        if name not in self.bots and len(self.bots) >= MAX_BOTS:
            raise ValueError("too many bots")
        old = self.bots.get(name)
        self.bots[name] = {"name": name, "state": state, "detail": detail[:120], "last_seen_ts": time.time(),
                           "kind": "bot"}
        if old is None or old["state"] != state or old.get("detail") != detail[:120]:
            self.changed()
        else:
            self._save()  # heartbeat only: no need to wake the pods
        return self.bots[name]

    def remove_bot(self, name: str) -> bool:
        existed = self.bots.pop(name, None) is not None
        if existed:
            self.changed()
        return existed

    def set_alert(self, text: str, level: str = "warn", ttl_s: int = 3600,
                  room: str | None = None) -> AlertRecord:
        room = norm_room(room)
        if not self.rooms.valid_target(room):
            raise ValueError(f"unknown room {room!r}; known: {sorted(self.rooms.known())}")
        now = time.time()
        alert = {
            "id": f"a-{int(now * 1000)}" + (f"-{room}" if room else ""),
            "text": text.strip()[:140],
            "level": level if level in {"info", "warn", "error"} else "warn",
            "room": room,
            "at_ts": now,
            "expires_ts": now + max(30, min(int(ttl_s), 7 * 86400)),
        }
        self.alerts[room or ALL] = alert
        self.changed()
        return alert

    def clear_alert(self, room: str | None = None) -> None:
        room = norm_room(room)
        if room is None:
            self.alerts.clear()
        else:
            self.alerts.pop(room, None)
        self.changed()

    def touch_device(self, device_id: str) -> str:
        """Each pod shows up as a bot "<room> pod" whenever it polls (no key needed)."""
        now = time.time()
        self.device_seen[device_id.lower()] = now
        name = f"{self.rooms.name(device_id)} pod"
        b = self.bots.get(name)
        if b is None and len(self.bots) < MAX_BOTS:
            self.bots[name] = {"name": name, "state": "ok", "detail": device_id.lower(), "last_seen_ts": now,
                               "kind": "pod"}
            self.changed()
        elif b is not None:
            stale = now - b.get("last_seen_ts", 0) > self.stale_s
            b["last_seen_ts"] = now
            if stale:
                self.changed()
        return name

    def active_devices(self, within_s: float) -> set[str]:
        now = time.time()
        return {d for d, t in self.device_seen.items() if now - t <= within_s}

    # ---------- read ----------
    def _visible_alerts(self, room: str | None) -> list[AlertRecord]:
        now = time.time()
        expired = [k for k, a in self.alerts.items() if a.get("expires_ts", 0) < now]
        for k in expired:
            self.alerts.pop(k)
        if room is None:  # widget / admin view: everything
            picked = list(self.alerts.values())
        else:
            picked = [a for k, a in self.alerts.items() if k in (ALL, room)]
        return sorted(picked, key=lambda a: a["at_ts"], reverse=True)

    def feed(self, room: str | None = None) -> Feed:
        now = time.time()
        bots: list[BotView] = []
        counts: Counts = {"ok": 0, "busy": 0, "error": 0, "offline": 0}
        for b in self.bots.values():
            stale = now - b.get("last_seen_ts", 0) > self.stale_s
            state = "offline" if stale else b["state"]
            counts[state] += 1
            bots.append({"name": b["name"], "state": state, "detail": b.get("detail", ""),
                         "last_seen": _iso(b.get("last_seen_ts")), "stale": stale, "kind": b.get("kind", "bot")})
        bots.sort(key=lambda x: (x["name"] != "Skwoop", x["kind"] == "pod", x["name"].lower()))
        alerts: list[AlertView] = [{"id": a["id"], "text": a["text"], "level": a["level"], "room": a.get("room"),
                                    "at": _iso(a["at_ts"]), "expires_at": _iso(a["expires_ts"])}
                                   for a in self._visible_alerts(room)]
        inbox = self.inbox.summary(room) if self.inbox is not None else \
            {"unread": 0, "latest_from": None, "latest_id": None, "latest_at": None}
        return {"v": 1, "rev": self.rev, "updated_at": _iso(self.updated_at), "room": room,
                "summary": self.summary(bots, counts), "counts": counts,
                "alert": alerts[0] if alerts else None, "alerts": alerts, "bots": bots, "inbox": inbox,
                "mood": pick_mood(next((b["state"] for b in bots
                                        if b["name"] == "Skwoop" and b["kind"] == "bot"), None),
                                  any(a["level"] != "info" for a in alerts)),
                "next": self.next_item()}

    @staticmethod
    def summary(bots: list[BotView], counts: Counts) -> str:
        team = [b for b in bots if b.get("kind") != "pod"]
        parts = []
        for want in ("error", "busy"):
            names = [b["name"] for b in team if b["state"] == want]
            if names:
                parts.append(f"{names[0]} {want}" + (f" +{len(names) - 1}" if len(names) > 1 else ""))
                break
        ok = sum(1 for b in team if b["state"] == "ok")
        if ok:
            parts.append(f"{ok} bot{'s' if ok != 1 else ''} ok")
        return "  |  ".join(parts) if parts else ("no bots reporting" if not team else "all quiet")

    # ---------- HTTP ----------
    def _authorized(self, request: Request) -> bool:
        if not self.write_key:
            return False  # writes disabled until a key is configured
        got = request.headers.get("Authorization", "")
        return hmac.compare_digest(got, f"Bearer {self.write_key}")

    def _read_authorized(self, request: Request) -> bool:
        """The read key (header or ?key=), or the write key or pod device token in the header. No read key
        configured = no reads."""
        if not self.read_key:
            return False

        got = request.headers.get("Authorization", "")
        if _same(got, f"Bearer {self.read_key}"):
            return True

        if self.write_key and _same(got, f"Bearer {self.write_key}"):
            return True

        if self.device_token and _same(got, f"Bearer {self.device_token}"):
            return True

        return _same(split_key(request.query.get("key", ""))[0], self.read_key)

    def _read_refused(self, request: Request) -> Response:
        peer, path = request.remote or "?", request.path
        now = time.time()
        if now - self._refused_logged.get((peer, path), 0.0) >= 60:
            self._refused_logged[(peer, path)] = now
            auth = request.headers.get("Authorization", "")
            scheme = (auth.split(" ", 1)[0] or "?") if auth else "none"
            log.warning("read refused: GET %s from %s (auth %s, query %s, device %s)", path, peer, scheme,
                        ",".join(sorted(request.query.keys())) or "-", request.headers.get("Device-Id", "-"))
        return unauthorized_read()

    async def _wait_for_change(self, since: int, wait_s: float) -> None:
        if self._changed is None:
            self._changed = asyncio.Condition()
        if self.rev != since:
            return
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: self.rev != since), timeout=wait_s)
            except TimeoutError:
                pass

    def routes(self) -> list[RouteDef]:
        async def get_status(request: Request) -> Response:
            if not self._read_authorized(request):
                return self._read_refused(request)
            query = {**request.query, **split_key(request.query.get("key", ""))[1]}

            dev = request.headers.get("Device-Id")
            room = None
            if dev:
                self.touch_device(dev)
                room = self.rooms.name(dev)
            elif query.get("room"):
                room = norm_room(query["room"])
            try:
                since = int(query.get("since", "-1"))
                wait_s = min(float(query.get("wait", "0")), MAX_WAIT_S)
            except ValueError:
                since, wait_s = -1, 0
            if since >= 0 and wait_s > 0:
                await self._wait_for_change(since, wait_s)
            return web.json_response(self.feed(room), headers={"Cache-Control": "no-store"})

        async def get_face(request: Request) -> StreamResponse:
            if not self._read_authorized(request):
                return self._read_refused(request)

            mood = self.feed()["mood"]
            return FileResponse(FACES_DIR / f"{mood}.png", headers={"Cache-Control": "no-store"})

        async def post_status(request: Request) -> Response:
            if not self._authorized(request):
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                body = await request.json()
                if "alert" in body:  # convenience: one call can set an alert too
                    if body["alert"]:
                        self.set_alert(str(body["alert"]), room=body.get("room"))
                    else:
                        self.clear_alert(body.get("room"))
                if body.get("name"):
                    self.upsert_bot(str(body["name"]), str(body.get("state", "ok")), str(body.get("detail", "")))
            except (ValueError, json.JSONDecodeError) as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response(self.feed())

        async def delete_bot(request: Request) -> Response:
            if not self._authorized(request):
                return web.json_response({"error": "unauthorized"}, status=401)
            ok = self.remove_bot(request.match_info["name"])
            return web.json_response({"removed": ok})

        async def post_alert(request: Request) -> Response:
            if not self._authorized(request):
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                body = await request.json()
                text = str(body.get("text", "")).strip()
                if not text:
                    raise ValueError("text required")
                self.set_alert(text, str(body.get("level", "warn")), int(body.get("ttl_s", 3600)),
                               room=body.get("room"))
            except (ValueError, json.JSONDecodeError) as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response(self.feed())

        async def delete_alert(request: Request) -> Response:
            if not self._authorized(request):
                return web.json_response({"error": "unauthorized"}, status=401)
            self.clear_alert(request.query.get("room"))
            return web.json_response(self.feed())

        return [
            web.get("/status", get_status),
            web.get("/face.png", get_face),
            web.post("/status", post_status),
            web.delete("/status/{name}", delete_bot),
            web.post("/alert", post_alert),
            web.delete("/alert", delete_alert),
        ]


def main() -> None:
    """Standalone feed (no voice). The inbox accepts uploads; text messages need TTS configured."""
    from inbox_store import InboxStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store = StatusStore()
    store.inbox = InboxStore(rooms=store.rooms, on_change=store.changed)
    app = Application()
    app.add_routes(store.routes())
    app.add_routes(store.inbox.routes())

    async def tts_up(app: Application) -> None:
        from providers import TTS
        app["http"] = ClientSession()
        inbox = store.inbox
        if inbox is None:
            return
        try:
            inbox.synth = TTS(app["http"]).synth
        except SystemExit as e:
            log.warning("inbox: no TTS (%s); only audio uploads will be accepted", e)

    async def tts_down(app: Application) -> None:
        await app["http"].close()

    app.on_startup.append(tts_up)
    app.on_cleanup.append(tts_down)
    if not store.write_key:
        log.warning("STATUS_WRITE_KEY not set: POST/DELETE are disabled")
    if not store.read_key:
        log.warning("STATUS_READ_KEY not set: GET /status and /face.png answer 401")
    web.run_app(app, host=os.environ.get("STATUS_HOST", "0.0.0.0"), port=int(os.environ.get("STATUS_PORT", "8004")))


if __name__ == "__main__":
    main()
