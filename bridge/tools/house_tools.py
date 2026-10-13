"""The pod's house tools and its live picture of the house. Everything here talks to Home Assistant, the house's
scripts, the pod's timer, the family texting path, the bots' status and the quick search: Skwoop's set, nothing else.

A tool returns an Outcome: `text` for the model (facts it phrases an answer from) and, for actions, `say`: the line
the pod speaks right away, so a plain "dim the lamp" needs no second model round. dry_run (the router eval) runs
the reads for real and changes nothing: no light, script, timer, list item, text or paid search.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

from lean_http import ClientError, ClientSession, ClientTimeout

import tools.ha_service as house
import router.quick as quick
import status_read
from memory.memory_recall import RecallError
from tools.messaging import Texts
from tools.quick_search import QuickSearch, SearchRefused
from status_store import NextItem

# Home Assistant JSON: objects, arrays, and scalars. Not typing.Any.
Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None


def _obj(value: object) -> dict[str, Json]:
    return cast(dict[str, Json], value) if isinstance(value, dict) else {}


def _arr(value: object) -> list[Json]:
    return cast(list[Json], value) if isinstance(value, list) else []

log = logging.getLogger("skwoop.tools")

TZ = ZoneInfo("America/Chicago")
SNAPSHOT_TTL_S = 15.0
FORECAST_TTL_S = 600.0
CAL_WINDOW_DAYS = 14         # prefetched span per calendar, from today 00:00
CAL_TTL_S = 300.0            # an older prefetch is not used (live read instead)
CAL_PREFETCH_S = float(os.environ.get("SKWOOP_CAL_PREFETCH_S") or 120.0)   # older: refreshed on the next turn or read
HISTORY_TTL_S = 120.0
INTERNET = ("binary_sensor.starlink_connectivity", "sensor.starlink_ping", "sensor.starlink_ping_drop_rate",
            "binary_sensor.starlink_obstructed")
HISTORY_DOMAINS = ("sensor.", "binary_sensor.", "person.")
_NO_DATA = ("unavailable", "unknown", "")
DEFAULT_LIGHTS = "living=light.desk_lamp,bedroom=light.bedroom_lamp"
DEFAULT_CALENDARS = "Work=calendar.work,Family=calendar.family"
DEFAULT_LISTS = "shopping=todo.shopping_list,reminders=todo.reminders_2"
LIGHT_NAMES = {"living": "living room lamp", "bedroom": "bedroom light"}
LIGHT_SCRIPTS = {("living", "on"): "living_on", ("living", "off"): "living_off",
                 ("bedroom", "on"): "bedroom_on", ("bedroom", "off"): "bedroom_off"}
TV_ACTIONS = ("on", "off", "pause", "play", "home", "press_play")
WRITABLE_LISTS = ("shopping",)


def _pairs(spec: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, _, v in (p.partition("=") for p in spec.split(",")) if k.strip() and v.strip()}


@dataclass(frozen=True)
class Outcome:
    text: str                      # what the model sees
    say: str | None = None         # an action's spoken line: the turn can end on it


class Escalate(Exception):
    def __init__(self, reason: str, facts: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.facts = facts or []


# ------------------------------------------------------------------ Home Assistant
class HAError(Exception):
    pass


class HA:
    def __init__(self, http: ClientSession | None, base: str, token: str, timeout_s: float = 6.0):
        self.http, self.base, self.token, self.timeout_s = http, base.rstrip("/"), token, timeout_s

    async def _req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Json:
        if self.http is None or not self.token:
            raise HAError("no Home Assistant link")
        try:
            async with self.http.request(method, f"{self.base}{path}", json=body,
                                         headers={"Authorization": f"Bearer {self.token}"},
                                         timeout=ClientTimeout(total=self.timeout_s)) as r:
                if r.status >= 400:
                    raise HAError(f"HTTP {r.status}")
                return cast(Json, await r.json(content_type=None))
        except (ClientError, TimeoutError) as e:
            raise HAError(type(e).__name__) from e

    async def states(self) -> list[dict[str, Any]]:
        data = await self._req("GET", "/api/states")
        if not isinstance(data, list):
            raise HAError("states: not a list")
        return [s for s in data if isinstance(s, dict) and isinstance(s.get("entity_id"), str)]

    async def service(self, domain: str, service: str, data: dict[str, Any], response: bool = False) -> Json:
        q = "?return_response" if response else ""
        return await self._req("POST", f"/api/services/{domain}/{service}{q}", data)

    async def calendar(self, entity: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        query = f"/api/calendars/{entity}?start={quote(start.isoformat())}&end={quote(end.isoformat())}"
        data = await self._req("GET", query)
        if not isinstance(data, list):
            raise HAError("calendar: not a list")
        return [e for e in data if isinstance(e, dict)]


    async def history(self, entities: list[str], start: datetime, end: datetime) -> dict[str, list[dict[str, Any]]]:
        """Read-only recorder history: entity -> [{state, last_changed}], first item = the state at *start*."""
        data = await self._req("GET", f"/api/history/period/{quote(start.isoformat())}?filter_entity_id="
                                      f"{','.join(entities)}&end_time={quote(end.isoformat())}&minimal_response&no_attributes")
        if not isinstance(data, list):
            raise HAError("history: not a list")
        rows: dict[str, list[dict[str, Any]]] = {}
        if isinstance(data, list):
            for pts in data:
                if not isinstance(pts, list) or not pts or not isinstance(pts[0], dict):
                    continue
                eid = pts[0].get("entity_id")
                if isinstance(eid, str) and eid:
                    rows[eid] = cast(list[dict[str, Any]], pts)
        return rows


def ha_from_env(http: ClientSession | None) -> HA:
    return HA(http, os.environ.get("HA_URL") or os.environ.get("HASS_URL") or "http://127.0.0.1:8123",
              os.environ.get("HA_TOKEN") or os.environ.get("HASS_TOKEN") or "")


def _local(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=TZ)).astimezone(TZ)


def next_candidate(cal: str, raw: object, now: datetime) -> tuple[tuple[date, int, datetime], NextItem] | None:
    """Parse one HA calendar event (the boundary). Returns (sort key, item) if it hasn't ended yet, else None.
    Same-day timed events sort before that day's all-day events."""
    if not isinstance(raw, dict):
        return None

    st, en = raw.get("start"), raw.get("end")
    st = st if isinstance(st, dict) else {}
    en = en if isinstance(en, dict) else {}
    title = str(raw.get("summary") or "(no title)")[:80]
    if "dateTime" in st:
        s0 = _local(str(st["dateTime"]))
        if s0 is None:
            return None

        s1 = _local(str(en.get("dateTime") or "")) or s0
        if s1 <= now:
            return None

        return (s0.date(), 0, s0), NextItem(title=title, start=s0.isoformat(), all_day=False, calendar=cal)

    try:
        d0 = date.fromisoformat(str(st.get("date")))
        d1 = date.fromisoformat(str(en.get("date"))) if en.get("date") else d0 + timedelta(days=1)
    except ValueError:
        return None

    if d1 <= now.date():                 # HA's all-day end date is exclusive
        return None

    day = max(d0, now.date())
    return (day, 1, datetime(day.year, day.month, day.day, tzinfo=TZ)), \
        NextItem(title=title, start=d0.isoformat(), all_day=True, calendar=cal)


def _clock(d: datetime) -> str:
    return d.strftime("%-I:%M %p").replace(":00 ", " ")


def _num(v: object) -> bool:
    try:
        float(str(v))
    except ValueError:
        return False
    return True


def snapshot_lines(states: list[dict[str, Any]], lights: dict[str, str], tv: str, weather: str,
                   timer_left: int | None) -> list[str]:
    """Home Assistant states -> the plain lines the fast model reads. Missing entities are skipped, not invented."""
    by = {s["entity_id"]: s for s in states}
    out = []
    for key, eid in lights.items():
        s = by.get(eid)
        if s is None:
            continue
        b = (s.get("attributes") or {}).get("brightness")
        level = f" at {round(int(b) * 100 / 255)}%" if s.get("state") == "on" and isinstance(b, (int, float)) else ""
        out.append(f"{LIGHT_NAMES.get(key, key).capitalize()}: {s.get('state')}{level}.")
    if (s := by.get(tv)) is not None:
        app = (s.get("attributes") or {}).get("app_name")
        out.append(f"Apple TV: {s.get('state')}" + (f", app {app}" if app else "") + ".")
    people = [(s.get("attributes") or {}).get("friendly_name") or s["entity_id"].split(".", 1)[1]
              for s in states if s["entity_id"].startswith("person.") and s.get("state") == "home"]
    away = [((s.get("attributes") or {}).get("friendly_name") or s["entity_id"].split(".", 1)[1], s.get("state"))
            for s in states if s["entity_id"].startswith("person.") and s.get("state") != "home"]
    if people or away:
        away_bits = ", ".join(n if st == "not_home" else f"{n} ({st})" for n, st in away)
        out.append("Home: " + (", ".join(people) or "nobody") +
                   ("" if not away else "; away: " + away_bits) + ".")
    if (s := by.get(weather)) is not None:
        a = s.get("attributes") or {}
        hum = a.get("humidity")
        out.append(f"Weather here now: {s.get('state')}, {a.get('temperature')}{a.get('temperature_unit') or '°F'}"
                   + (f", humidity {hum}%" if isinstance(hum, (int, float)) else "") + ".")
    rise, sets = by.get("sensor.sun_next_rising"), by.get("sensor.sun_next_setting")
    if rise and sets and (r := _local(rise.get("state"))) and (st := _local(sets.get("state"))):
        out.append(f"Next sunset {st.strftime('%a')} {_clock(st)}, next sunrise {r.strftime('%a')} {_clock(r)}.")
    if (s := by.get("binary_sensor.starlink_connectivity")) is not None:
        ping = by.get("sensor.starlink_ping")
        ms = ping.get("state") if ping else None
        out.append("Internet (Starlink): " + ("connected" if s.get("state") == "on" else str(s.get("state"))) +
                   (f", ping {round(float(str(ms)))} ms" if _num(ms) else "") + ".")
    timer = f"{quick.duration_words(quick.round_left(timer_left))} left." if timer_left else "none."
    out.append("Pod timer: " + timer)
    return out


# ------------------------------------------------------------------ history summaries
def _spans(points: list[dict[str, Any]], start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
    """History points -> (from, to, state) spans clipped to [start, end]."""
    out = []
    for i, p in enumerate(points):
        t0 = max(_local(p.get("last_changed")) or start, start)
        t1 = min((_local(points[i + 1].get("last_changed")) if i + 1 < len(points) else None) or end, end)
        if t1 > t0:
            out.append((t0, t1, str(p.get("state") or "")))
    return out


def _dur(secs: float) -> str:
    m = round(secs / 60)
    return f"{m} min" if m < 120 else f"{m / 60:.1f} h"


def _when(t: datetime) -> str:
    return f"{t.strftime('%a')} {_clock(t)}"


def _numeric(spans: list[tuple[datetime, datetime, str]], unit: str) -> str | None:
    vals = [(float(st), (t1 - t0).total_seconds(), t0) for t0, t1, st in spans if _num(st)]
    if not vals:
        return None
    total = sum(w for _, w, _ in vals)
    avg = sum(v * w for v, w, _ in vals) / total
    hi = max(vals, key=lambda x: x[0])
    lo = min(vals, key=lambda x: x[0])
    precise = max(abs(hi[0]), abs(lo[0])) < 10

    def fmt(v: float) -> str:
        return f"{v:.1f}" if precise else f"{round(v)}"

    f = fmt
    if f(hi[0]) == f(lo[0]):
        return f"steady at {f(hi[0])}{unit}"
    return f"average {f(avg)}{unit}, low {f(lo[0])}{unit}, high {f(hi[0])}{unit} ({_when(hi[2])})"


def _no_data(spans: list[tuple[datetime, datetime, str]]) -> str:
    gap = sum((t1 - t0).total_seconds() for t0, t1, st in spans if st in _NO_DATA)
    return f"; no data for {_dur(gap)}" if gap >= 60 else ""


def internet_history(hist: dict[str, list[dict[str, Any]]], start: datetime, end: datetime) -> list[str]:
    out = []
    if pts := hist.get("binary_sensor.starlink_connectivity"):
        sp = _spans(pts, start, end)
        down = [(t0, t1) for t0, t1, st in sp if st == "off"]
        if not down:
            out.append("Starlink connected the whole time" + _no_data(sp) + ".")
        else:
            longest = max(down, key=lambda d: d[1] - d[0])
            out.append(f"Starlink down {len(down)} time{'s' * (len(down) > 1)}, "
                       f"{_dur(sum((b - a).total_seconds() for a, b in down))} in total, longest "
                       f"{_dur((longest[1] - longest[0]).total_seconds())} ({_when(longest[0])})" + _no_data(sp) + ".")
    if (pts := hist.get("sensor.starlink_ping")) and (n := _numeric(_spans(pts, start, end), " ms")):
        out.append(f"Ping: {n}.")
    if (pts := hist.get("sensor.starlink_ping_drop_rate")) and (n := _numeric(_spans(pts, start, end), "%")):
        out.append(f"Dropped pings: {n}.")
    if pts := hist.get("binary_sensor.starlink_obstructed"):
        obs = sum((t1 - t0).total_seconds() for t0, t1, st in _spans(pts, start, end) if st == "on")
        out.append("Dish obstructed: " + (_dur(obs) if obs >= 60 else "never") + ".")
    return out


def entity_history(eid: str, pts: list[dict[str, Any]], start: datetime, end: datetime, unit: str = "") -> str:
    sp = _spans(pts, start, end)
    if (n := _numeric(sp, f" {unit}" if unit else "")) is not None:
        return f"{eid}: {n}" + _no_data(sp) + "."
    share: dict[str, float] = {}
    for t0, t1, st in sp:
        if st not in _NO_DATA:
            share[st] = share.get(st, 0.0) + (t1 - t0).total_seconds()
    total = sum(share.values()) or 1.0
    top = sorted(share.items(), key=lambda kv: -kv[1])[:3]
    return (f"{eid}: " + ", ".join(f"{st} {round(100 * secs / total)}% of the time" for st, secs in top)
            + f", {max(0, len(sp) - 1)} changes" + _no_data(sp) + ".") if top else f"{eid}: no data."


# ------------------------------------------------------------------ tool specs (what the model may call)
def _fn(name: str, desc: str, props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


SPECS = [
    _fn("lights", "Switch, dim or colour a light. 'living' is the living room lamp (the trumpet light).",
        {"light": {"type": "string", "enum": ["living", "bedroom", "all"]},
         "power": {"type": "string", "enum": ["on", "off"]},
         "brightness_pct": {"type": "integer", "minimum": 1, "maximum": 100},
         "color": {"type": "string", "description": "a colour name, e.g. purple, warm white"}}, ["light"]),
    _fn("apple_tv", "Control the living room Apple TV, or put on a show or movie by title.",
        {"action": {"type": "string", "enum": [*TV_ACTIONS, "title"]}, "title": {"type": "string"}}, ["action"]),
    _fn("timer", "The pod's kitchen timer: set (seconds), cancel, or time left.",
        {"action": {"type": "string", "enum": ["set", "cancel", "left"]}, "seconds": {"type": "integer"}}, ["action"]),
    _fn("forecast", "Daily forecast for home: today and the next days (condition, high, low, rain)."),
    _fn("calendar", "Alex's calendars (Work and Family) for a day range. day_offset 0 = today, 1 = tomorrow.",
        {"day_offset": {"type": "integer", "minimum": 0, "maximum": 13},
         "days": {"type": "integer", "minimum": 1, "maximum": 7}}, ["day_offset"]),
    _fn("lists", "Read the shopping list or reminders, or add an item to the shopping list.",
        {"action": {"type": "string", "enum": ["read", "add"]},
         "list": {"type": "string", "enum": ["shopping", "reminders"]},
         "item": {"type": "string"}}, ["action", "list"]),
    _fn("history", "How something has been over the past hours or days (Home Assistant history, read-only). "
        "'internet' = Starlink: outages, ping, dropped pings, obstruction. Or a sensor's entity_id.",
        {"what": {"type": "string", "description": "'internet', or an entity_id like sensor.x"},
         "hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "how far back; 168 = a week"}},
        ["what"]),
    _fn("bot_status", "What the house's bots are doing: Deep runs and scheduled jobs, Guard, Remote host."),
    _fn("send_text", "Text a family member (iMessage). The pod reads it back and only sends after Alex says yes.",
        {"to": {"type": "string", "description": "who, e.g. Mom, Dad, Mel (Alex's wife)"},
         "message": {"type": "string", "description": "the message, in Alex's words, first person"}},
        ["to", "message"]),
    _fn("web_search", "One quick web search for a live fact: news, scores, prices, hours, who holds an office, weather "
        "somewhere else. One search per question.", {"query": {"type": "string"}}, ["query"]),
    _fn("recall_memory", "Skwoop's long-term memory, for one fact about Alex's people or pets (who his wife is, "
        "his dog's name, a birthday). Anything more from memory: escalate.",
        {"query": {"type": "string", "description": "the question, in Alex's words"}}, ["query"]),
    _fn("escalate", "Hand this request to Skwoop's deep agent (documents, long-term memory, research across sources, "
        "reading web pages or files, several steps). Use it instead of guessing.",
        {"reason": {"type": "string"}}, ["reason"]),
]
WHITES = {"warm white": 2700, "warm": 2700, "soft white": 2700, "white": 4000, "cool white": 5000, "cool": 5000,
          "daylight": 5500}
def is_action(name: str, args: dict[str, Any]) -> bool:
    """Tools that change something in the house or send something (reads are always allowed)."""
    return name in ("lights", "apple_tv", "send_text") or (
        name == "timer" and args.get("action") in ("set", "cancel")) or (
        name == "lists" and args.get("action") == "add")


TOOL_NAMES = frozenset(s["function"]["name"] for s in SPECS)


@dataclass
class HouseTools:
    """Shared by every pod; `for_turn` binds the pod's device and room."""
    ha: HA
    timers: quick.TimerBook
    answers: quick.QuickAnswers
    texts: Texts
    search: QuickSearch | None
    status: status_read.StatusSources = field(default_factory=status_read.StatusSources)
    show: house.ShowContext | None = None
    http: ClientSession | None = None
    pod_bots: Callable[[], list[dict[str, Any]]] = list
    recall: Callable[[str], Awaitable[str]] | None = None         # memory_recall.MemoryRecall; None = no memory
    lights: dict[str, str] = field(default_factory=lambda: _pairs(os.environ.get("SKWOOP_LIGHTS") or DEFAULT_LIGHTS))
    calendars: dict[str, str] = field(
        default_factory=lambda: _pairs(os.environ.get("SKWOOP_CALENDARS") or DEFAULT_CALENDARS))
    todo_lists: dict[str, str] = field(default_factory=lambda: _pairs(os.environ.get("SKWOOP_LISTS") or DEFAULT_LISTS))
    tv: str = field(default_factory=lambda: os.environ.get("SKWOOP_TV_ENTITY") or "media_player.living_room")
    weather: str = field(default_factory=lambda: os.environ.get("WEATHER_ENTITY") or quick.DEFAULT_WEATHER_ENTITY)
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(TZ)
    before_10am: Callable[[], bool] = house._before_10am
    _snap: tuple[float, list[dict[str, Any]]] | None = None
    _forecast: tuple[float, str] | None = None
    _cal: dict[str, tuple[float, datetime, list[dict[str, Any]]]] = field(default_factory=dict)
    _hist: dict[tuple[str, int], tuple[float, str]] = field(default_factory=dict)
    _cal_task: asyncio.Task[None] | None = None

    async def prefetch_calendars(self) -> None:
        """Fetch each calendar's next CAL_WINDOW_DAYS so calendar asks read from memory."""
        start = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for eid in self.calendars.values():
            try:
                events = await self.ha.calendar(eid, start, start + timedelta(days=CAL_WINDOW_DAYS))
                self._cal[eid] = (self.clock(), start, events)
            except HAError as e:
                log.warning("calendar prefetch %s failed: %s", eid, e)

    def kick_calendars(self) -> None:
        """Refresh the prefetch in the background if any calendar is missing or older than CAL_PREFETCH_S.
        Called at startup, at listen start and on /status reads: nothing refreshes on a schedule, so an idle
        bridge stays asleep."""
        if CAL_PREFETCH_S <= 0:  # SKWOOP_CAL_PREFETCH_S=0: no prefetch, calendar asks read live
            return
        fresh = len(self._cal) == len(self.calendars) and all(
            self.clock() - c[0] < CAL_PREFETCH_S for c in self._cal.values())
        if fresh or (self._cal_task is not None and not self._cal_task.done()):
            return
        try:
            self._cal_task = asyncio.get_running_loop().create_task(self.prefetch_calendars())
        except RuntimeError:  # no loop (a sync caller in tests): the next async caller refreshes
            return

    def next_event_lazy(self) -> NextItem | None:
        """next_event, and a background refresh when the prefetch is stale (the answer is the cached one)."""
        self.kick_calendars()
        return self.next_event()

    async def calendar_events(self, eid: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Prefetched window when fresh and covering [start, end); else a live read. Callers filter by range."""
        c = self._cal.get(eid)
        covered = c is not None and end <= c[1] + timedelta(days=CAL_WINDOW_DAYS)
        if c is not None and self.clock() - c[0] < CAL_TTL_S and c[1] <= start and covered:
            return c[2]
        return await self.ha.calendar(eid, start, end)

    def next_event(self) -> NextItem | None:
        """The next calendar item that hasn't ended, from the prefetch only (no HA call: /status is long-polled)."""
        now = self.now()
        best: tuple[tuple[date, int, datetime], NextItem] | None = None
        for cal, eid in self.calendars.items():
            cached = self._cal.get(eid)
            for raw in cached[2] if cached else []:
                c = next_candidate(cal, raw, now)
                if c is not None and (best is None or c[0] < best[0]):
                    best = c

        return best[1] if best else None

    async def refresh(self) -> None:
        """Prewarm (listen start): fetch states so the turn's snapshot is already here, and age-check the calendars."""
        self.kick_calendars()
        try:
            self._snap = (self.clock(), await self.ha.states())
        except HAError as e:
            log.warning("snapshot refresh failed: %s", e)

    async def snapshot(self, device: str) -> str:
        if self._snap is None or self.clock() - self._snap[0] > SNAPSHOT_TTL_S:
            await self.refresh()
        if self._snap is None:
            return "Home Assistant is not answering right now, so the house state is unknown."
        return "\n".join(snapshot_lines(self._snap[1], self.lights, self.tv, self.weather, self.timers.left(device)))

    def for_turn(self, device: str, room: str, dry_run: bool = False, hold: str | None = None) -> TurnTools:
        return TurnTools(self, device, room, dry_run, hold)


@dataclass
class TurnTools:
    shared: HouseTools
    device: str
    room: str
    dry_run: bool = False
    hold: str | None = None              # router.hold_actions: why action tools may not run this turn
    calls: list[tuple[str, dict[str, Any], float]] = field(default_factory=list)

    async def run(self, name: str, args: dict[str, Any]) -> Outcome:
        t0 = time.monotonic()
        try:
            if name == "escalate":
                raise Escalate(str(args.get("reason") or "needs the deep agent"))
            if self.hold and is_action(name, args):
                return Outcome(f"not done: Alex {self.hold}. Answer from the house state, or ask whether he wants it.")
            fn = self.registry().get(name)
            if fn is None:
                return Outcome(f"error: no tool named {name}")
            return await fn(args)
        except HAError as e:
            return Outcome(f"error: Home Assistant didn't answer ({e})")
        finally:
            self.calls.append((name, args, time.monotonic() - t0))

    def registry(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Outcome]]]:
        """Tool name -> its function, spelled out (no getattr on computed names). `escalate` is handled in run()."""
        return {"lights": self._t_lights, "apple_tv": self._t_apple_tv, "timer": self._t_timer,
                "lists": self._t_lists, "send_text": self._t_send_text, "web_search": self._t_web_search,
                "forecast": self._t_forecast, "calendar": self._t_calendar, "history": self._t_history,
                "recall_memory": self._t_recall_memory, "bot_status": self._t_bot_status}

    # --- actions
    async def _fire(self, iid: str, heard: str = "") -> house.Hit | None:
        hit = house.hit_from_id(iid, heard, self.shared.show)
        if hit is None or self.dry_run:
            return hit
        return await house._fire(self.shared.http, hit)

    async def _t_lights(self, a: dict[str, Any]) -> Outcome:
        which = str(a.get("light") or "living")
        keys = list(self.shared.lights) if which == "all" else [which]
        if any(k not in self.shared.lights for k in keys):
            return Outcome(f"error: no light called {which}; lights: {', '.join(self.shared.lights)}")
        power, pct, color = a.get("power"), a.get("brightness_pct"), a.get("color")
        on = power != "off" and (power == "on" or pct is not None or color)
        if on and "bedroom" in keys and self.shared.before_10am():
            return Outcome("blocked: the bedroom light stays off until 10 AM", say="Bedroom stays off until after 10.")
        if pct is None and not color:
            hits = [await self._fire(LIGHT_SCRIPTS[(k, "on" if on else "off")]) for k in keys]
            say = hits[0].spoken if len(hits) == 1 and hits[0] else ("Lights on." if on else "Lights off.")
            return Outcome(f"done: {', '.join(keys)} {'on' if on else 'off'}", say=say)
        data: dict[str, Any] = {"entity_id": [self.shared.lights[k] for k in keys]}
        if pct is not None:
            data["brightness_pct"] = max(1, min(100, int(pct)))
        if color:
            c = " ".join(str(color).lower().split())
            if c in WHITES:
                data["color_temp_kelvin"] = WHITES[c]
            else:
                data["color_name"] = c.replace(" ", "")
        if not self.dry_run:
            await self.shared.ha.service("light", "turn_on", data)
        return Outcome(f"done: {data}", say="Okay.")

    async def _t_apple_tv(self, a: dict[str, Any]) -> Outcome:
        action = str(a.get("action") or "")
        if action == "title":
            title = str(a.get("title") or "").strip()
            hit = house.play_show_hit(f"watch {title}", self.shared.show) if title else None
            if hit is None:
                return Outcome("error: no title given")
            if not self.dry_run:
                hit = await house._fire(self.shared.http, hit)
            return Outcome(f"done: {hit.text}", say=hit.text)
        if action not in TV_ACTIONS:
            return Outcome(f"error: action must be one of {TV_ACTIONS} or title")
        hit = await self._fire(f"tv_{action}")
        return Outcome(f"done: tv {action}", say=hit.spoken if hit else "Okay.")

    async def _t_timer(self, a: dict[str, Any]) -> Outcome:
        action = a.get("action")
        intent: quick.QuickIntent
        if action == "set":
            secs = int(a.get("seconds") or 0)
            if not 0 < secs <= quick.MAX_TIMER_S:
                return Outcome("error: seconds must be between 1 and 24 hours")
            intent = quick.TimerSet(secs)
        elif action == "cancel":
            intent = quick.TimerCancel()
        else:
            intent = quick.TimerLeft()
        if self.dry_run and action != "left":
            return Outcome(f"done: timer {action}", say="Okay.")
        reply = await self.shared.answers.answer(intent, self.device, self.room)
        return Outcome(f"done: {reply.text if reply else ''}", say=reply.text if reply else "Okay.")

    async def _t_lists(self, a: dict[str, Any]) -> Outcome:
        name = str(a.get("list") or "shopping")
        eid = self.shared.todo_lists.get(name)
        if eid is None:
            return Outcome(f"error: lists are {', '.join(self.shared.todo_lists)}")
        if a.get("action") == "add":
            item = " ".join(str(a.get("item") or "").split())[:80]
            if name not in WRITABLE_LISTS or not item:
                return Outcome("error: only the shopping list takes new items, and an item is needed")
            if not self.dry_run:
                await self.shared.ha.service("todo", "add_item", {"entity_id": eid, "item": item})
            return Outcome(f"done: added {item}", say=f"Added {item} to the shopping list.")
        data = await self.shared.ha.service(
            "todo", "get_items", {"entity_id": eid, "status": "needs_action"}, response=True)
        items = _arr(_obj(_obj(_obj(data).get("service_response")).get(eid)).get("items"))
        names = [str(i.get("summary")) for i in items if isinstance(i, dict) and i.get("summary")]
        if any("upgraded these reminders" in n for n in names):    # Apple's stub for a list iCloud moved on from
            return Outcome(f"{name} list: not readable from Home Assistant (Apple upgraded that Reminders list).")
        return Outcome(f"{name} list: " + ("; ".join(names) if names else "empty"))

    async def _t_send_text(self, a: dict[str, Any]) -> Outcome:
        pending, told = self.shared.texts.propose(self.device, str(a.get("to") or ""), str(a.get("message") or ""))
        if pending is None:
            names = " or ".join(c.spoken for c in self.shared.texts.contacts.values()) or "nobody yet"
            who = " ".join(str(a.get("to") or "them").split())[:40]
            return Outcome(told, say=f"{who} isn't in the family contacts, so I can't text them. I can text {names}.")
        return Outcome(told, say=pending.readback)

    async def _t_web_search(self, a: dict[str, Any]) -> Outcome:
        q = str(a.get("query") or "").strip()
        if not q:
            return Outcome("error: empty query")
        if self.dry_run:
            return Outcome(f"(dry run: search for {q!r} not sent)", say="(eval: search not sent)")
        if self.shared.search is None:
            raise Escalate("no quick search on this bridge")
        try:
            ans = await self.shared.search.ask(q)
        except SearchRefused as e:
            return Outcome(f"error: {e}", say="I've used up today's web searches.")
        except (ClientError, TimeoutError, RuntimeError, ValueError, TypeError) as e:
            raise Escalate(f"quick search failed ({type(e).__name__})") from None
        return Outcome(ans.text or "no answer", say=ans.text or None)

    # --- reads
    async def _t_forecast(self, a: dict[str, Any]) -> Outcome:
        s = self.shared
        if s._forecast is not None and s.clock() - s._forecast[0] < FORECAST_TTL_S:
            return Outcome(s._forecast[1])
        data = await s.ha.service("weather", "get_forecasts", {"entity_id": s.weather, "type": "daily"}, response=True)
        days = _arr(_obj(_obj(_obj(data).get("service_response")).get(s.weather)).get("forecast"))
        lines: list[str] = []
        for raw in days[:7]:
            if not isinstance(raw, dict):
                continue
            stamp = raw.get("datetime")
            when = _local(stamp if isinstance(stamp, str) else None)
            if when is None:
                continue
            rain = raw.get("precipitation")
            wind = raw.get("wind_speed")
            wind_bit = f", wind {round(float(cast(int | float | str, wind)))} mph" if wind else ""
            lines.append(f"{when.strftime('%a %b %-d')}: {raw.get('condition')}, high {raw.get('temperature')}, "
                         f"low {raw.get('templow')}" + (f", rain {rain} in" if rain else "") + wind_bit)
        text = "Forecast (°F): " + "; ".join(lines) if lines else "error: no forecast"
        s._forecast = (s.clock(), text)
        return Outcome(text)

    async def _t_calendar(self, a: dict[str, Any]) -> Outcome:
        off = max(0, min(13, int(a.get("day_offset") or 0)))
        days = max(1, min(7, int(a.get("days") or 1)))
        start = self.shared.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=off)
        end = start + timedelta(days=days)
        lines = []
        for cal, eid in self.shared.calendars.items():
            try:
                events = await self.shared.calendar_events(eid, start, end)
            except HAError as e:
                lines.append(f"{cal} calendar: unavailable ({e})")
                continue
            for ev in events:
                st, en = ev.get("start") or {}, ev.get("end") or {}
                if "dateTime" in st:
                    s0, s1 = _local(st["dateTime"]), _local(en.get("dateTime"))
                    if s0 is not None and (s0 >= end or (s1 or s0) <= start):
                        continue
                    when = f"{s0.strftime('%a %b %-d')} {_clock(s0)}" + (
                        f" to {_clock(s1)}" if s1 else "") if s0 else "?"
                else:
                    try:
                        d0 = date.fromisoformat(str(st.get("date")))
                    except ValueError:
                        continue
                    if not start.date() <= d0 < end.date():     # HA also returns the all-day event at the end edge
                        continue
                    when = f"{d0.strftime('%a %b %-d')} all day"
                lines.append(f"{cal}: {when}: {ev.get('summary') or '(no title)'}"
                             + (f" at {ev['location']}" if ev.get("location") else ""))
        end_label = (end - timedelta(days=1)).strftime("%a %b %-d")
        span = start.strftime("%a %b %-d") + ("" if days == 1 else f" to {end_label}")
        return Outcome(f"Calendar {span}: " + ("; ".join(lines) if lines else "nothing scheduled"))

    async def _t_history(self, a: dict[str, Any]) -> Outcome:
        s = self.shared
        what = " ".join(str(a.get("what") or "internet").lower().split())
        hours = max(1, min(168, int(a.get("hours") or 24)))
        if what in ("internet", "starlink", "wifi", "network"):
            what, ents = "internet", list(INTERNET)
        elif what.startswith(HISTORY_DOMAINS) and "," not in what:
            ents = [what]
        else:
            return Outcome("error: what must be 'internet' or one sensor/binary_sensor/person entity_id")
        if (hit := s._hist.get((what, hours))) is not None and s.clock() - hit[0] < HISTORY_TTL_S:
            return Outcome(hit[1])
        end = s.now()
        start = end - timedelta(hours=hours)
        hist = await s.ha.history(ents, start, end)
        if what == "internet":
            lines = internet_history(hist, start, end)
        else:
            unit = ""
            if s._snap is not None:
                st = next((x for x in s._snap[1] if x.get("entity_id") == what), None)
                unit = str(((st or {}).get("attributes") or {}).get("unit_of_measurement") or "")
            lines = [entity_history(what, hist[what], start, end, unit)] if what in hist else []
        span = f"{hours // 24} days" if hours % 24 == 0 and hours > 24 else f"{hours} hours"
        text = f"History, last {span}: " + (" ".join(lines) if lines else "no recorded history for that.")
        s._hist[(what, hours)] = (s.clock(), text)
        return Outcome(text)

    async def _t_recall_memory(self, a: dict[str, Any]) -> Outcome:
        recall = self.shared.recall
        query = str(a.get("query") or "").strip()
        if recall is None or not query:
            return Outcome("error: memory is not available here; escalate if it needs memory")

        try:
            return Outcome(await recall(query))
        except RecallError as e:
            log.warning("recall_memory: %s", e)
            return Outcome("error: memory didn't answer; escalate")

    async def _t_bot_status(self, a: dict[str, Any]) -> Outcome:
        return Outcome(await status_read.read_status(self.shared.status, self.shared.pod_bots()))
