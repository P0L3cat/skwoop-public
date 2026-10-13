"""Reminders on the Work calendar (iCloud, via HA calendar.create_event), written HOLD_S late: HA can create CalDAV
events but not delete them, so "never mind" inside the window just drops the held reminder. Held reminders persist
in a small JSON file, and the flush loop writes each once its window has passed (also any left from a restart)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.house_tools import HAError, Outcome
from memory.memory_write import HEALTH

log = logging.getLogger("skwoop.reminders")
TZ = ZoneInfo("America/Chicago")
EVENT = {"entity_id": "calendar.work", "description": "Set from a Skwoop pod"}  # how a later announcer finds ours
DEFAULT_FILE = Path("~/.skwoop/pending-reminders.json").expanduser()
HOLD_S = 60.0
LATE_S = 600.0  # after the write, "never mind" hears that it's on the calendar instead of a made-up "cancelled"
ASK_S = 45.0  # after "what time?", that pod's next turn comes back here
RETRY_S = 5.0  # a failed write is tried again this much later
LENGTH = timedelta(minutes=15)
SAID_SET = "I'll remind you {when}: {text}."
SAID_WHEN = "What time should I remind you?"
SAID_HEALTH = "I don't put health details on the calendar from the pod."
SAID_CANCELLED = "Okay, I won't remind you."
SAID_TOO_LATE = "That one's already on your Work calendar. You can delete it there."

ServiceCall = Callable[[str, str, dict[str, str]], Awaitable[object]]


def parse_when(raw: str, now: datetime) -> datetime | None:
    """The model's local "YYYY-MM-DDTHH:MM" -> an aware time between now and a year out, else None."""
    try:
        at = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    at = at.replace(tzinfo=TZ) if at.tzinfo is None else at.astimezone(TZ)
    return at if now < at <= now + timedelta(days=366) else None


def spoken(at: datetime, now: datetime) -> str:
    days = (at.date() - now.date()).days
    day = ("today", "tomorrow")[days] if days < 2 else at.strftime("%A" if days < 7 else "%A, %B %-d")
    return f"{day} at {at.strftime('%-I:%M %p').replace(':00', '')}"


@dataclass(frozen=True)
class Held:
    device: str
    text: str
    at: str  # str(aware datetime), America/Chicago
    due: float  # wall-clock second it gets written


class Reminders:
    def __init__(self, call: ServiceCall, path: Path = DEFAULT_FILE, wall: Callable[[], float] = time.time) -> None:
        self.call, self.path, self.wall = call, path, wall
        try:
            self.held = [Held(**row) for row in json.loads(path.read_text())]
        except (OSError, ValueError, TypeError):  # missing or hand-edited: start empty rather than stop the bridge
            self.held = []
        self.asked: dict[str, float] = {}  # device -> when the time question went out
        self.wrote: dict[str, float] = {}  # device -> when its last reminder was written
        self._timer: asyncio.TimerHandle | None = None  # one timer at the next due time; none when nothing is held
        self._flushing: asyncio.Task[None] | None = None

    def asking(self, device: str) -> bool:
        return self.wall() - self.asked.get(device, float("-inf")) < ASK_S

    def pending(self, device: str) -> bool:
        late = self.wall() - self.wrote.get(device, float("-inf")) < LATE_S
        return late or self.asking(device) or device in {h.device for h in self.held}

    async def remind(self, args: Mapping[str, object], device: str, dry_run: bool = False) -> Outcome:
        """The remind tool: args {"text", "when", "ask"}. No clear time: speak the model's one question (or ours)."""
        text = " ".join(str(args.get("text") or "").split()).rstrip(".")
        text = text[:1].upper() + text[1:]
        if HEALTH.search(text):
            return Outcome("not set: health detail", say=SAID_HEALTH)
        now = datetime.fromtimestamp(self.wall(), TZ)
        at = parse_when(str(args.get("when") or ""), now)
        if at is None or not text:
            self.asked[device] = self.wall()
            return Outcome("not set: asked for the time", say=str(args.get("ask") or "").strip() or SAID_WHEN)
        self.asked.pop(device, None)
        say = SAID_SET.format(when=spoken(at, now), text=text)
        if not dry_run:
            self.held = [*self.held, Held(device, text, str(at), self.wall() + HOLD_S)]
            self._save()
            self.arm()
        return Outcome(f"{'dry run, not held' if dry_run else f'held {HOLD_S:.0f} s'}: {say}", say=say)

    def cancel(self, device: str) -> Outcome | None:
        """forget_last / "never mind": drop this pod's held reminders and time question; None when it has neither."""
        if self.asked.pop(device, None) is not None or device in {h.device for h in self.held}:
            self.held = [h for h in self.held if h.device != device]
            self._save()
            # Abort an in-flight flush so a slow HA create_event cannot land after "never mind".
            if self._flushing is not None and not self._flushing.done():
                self._flushing.cancel()
                self._flushing = None
            self.arm()
            return Outcome("cancelled before it was written", say=SAID_CANCELLED)
        late = self.wall() - self.wrote.pop(device, float("-inf")) < LATE_S
        return Outcome("already written", say=SAID_TOO_LATE) if late else None

    async def flush(self) -> int:
        written: list[Held] = []
        for h in [h for h in self.held if h.due <= self.wall()]:
            # Re-check after await points: cancel() may have dropped this hold while HA was slow.
            if h not in self.held:
                continue
            event = {**EVENT, "summary": h.text, "start_date_time": h.at}
            event["end_date_time"] = str(datetime.fromisoformat(h.at) + LENGTH)
            try:
                await self.call("calendar", "create_event", event)
            except HAError as e:
                log.warning("reminder not written (%s): retrying", e)
                continue
            except Exception as e:  # noqa: BLE001 - a bad JSON body must not kill the flush task
                log.warning("reminder not written (%s): retrying", type(e).__name__)
                continue
            if h not in self.held:  # cancelled while the HA call ran
                log.info("reminder cancelled in flight for %s; not recording the write", h.device)
                continue
            written.append(h)
            self.wrote[h.device] = self.wall()
            log.info("reminder written to the Work calendar for %s (%s)", h.at, h.device)
        if written:
            self.held = [h for h in self.held if h not in written]
            self._save()
        return len(written)

    def arm(self, at_least_s: float = 0.0) -> None:
        """(Re)set the one timer to the earliest held reminder's due time; no timer while nothing is held, so an idle
        bridge never wakes for reminders. Needs a running loop (the bridge's); without one it does nothing."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self.held:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(at_least_s, min(h.due for h in self.held) - self.wall())
        self._timer = loop.call_later(delay, self._due)

    def _due(self) -> None:
        self._timer = None
        if self._flushing is None or self._flushing.done():
            self._flushing = asyncio.get_running_loop().create_task(self._flush_and_rearm())

    async def _flush_and_rearm(self) -> None:
        try:
            await self.flush()
        except asyncio.CancelledError:
            self.arm()
            raise
        overdue = any(h.due <= self.wall() for h in self.held)   # a write failed: retry in RETRY_S, not at once
        self.arm(RETRY_S if overdue else 0.0)

    def start(self) -> None:
        """Bridge startup: write anything held across a restart that is due, then wait for the next due time."""
        self.arm()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._flushing is not None:
            self._flushing.cancel()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(h) for h in self.held]))
        tmp.replace(self.path)
