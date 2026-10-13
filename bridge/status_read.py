"""What the house's bots are doing, read from where they actually keep it. Read-only.

  Deep        ~/.skwoop/agent/gateway_state.json (gateway + platforms), state.db sessions (runs: open / last finished)
  Deep cron   ~/.skwoop/agent/cron/jobs.json (the scheduled bots: enabled, failing, next up)
  Guard     launchd jobs com.skwoop.guard.* and com.bluebubbles.server (running / last exit)
  Linux         Grok Build's reverse tunnel on 127.0.0.1:8788 (listening = Linux is connected)
  Pods         whatever bots reported to this bridge's /status

This answers "what are the bots working on" from data instead of the model's imagination. AGENT_HOME overrides
~/.skwoop/agent (tests).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("skwoop.status")

TZ = ZoneInfo("America/Chicago")
LINUX_TUNNEL = ("127.0.0.1", 8788)
WATCHED_JOBS = ("ai.skwoop.gateway", "com.bluebubbles.server")
PLATFORM_NAMES = {"bluebubbles": "iMessage", "telegram": "Telegram", "webhook": "the pod webhook",
                  "homeassistant": "Home Assistant"}


@dataclass(frozen=True)
class LaunchJob:
    label: str
    pid: int | None
    last_exit: int


@dataclass(frozen=True)
class CronJob:
    name: str
    enabled: bool
    last_status: str | None
    failure_streak: int
    next_run_at: datetime | None


@dataclass(frozen=True)
class AgentRun:
    source: str
    title: str
    started_at: float
    ended_at: float | None


@dataclass
class StatusSources:
    """Seams: tests hand in their own launchctl output and tunnel check."""
    agent_home: Path = field(default_factory=lambda: Path(
        os.environ.get("AGENT_HOME") or Path.home() / ".skwoop" / "agent"))
    launchctl: Callable[[], Awaitable[str]] | None = None
    tunnel_up: Callable[[], Awaitable[bool]] | None = None
    now: Callable[[], float] = time.time


def parse_launchctl(text: str) -> list[LaunchJob]:
    jobs = []
    for line in text.splitlines()[1:]:
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 3:
            continue
        pid, status, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        try:
            jobs.append(LaunchJob(label, int(pid) if pid.isdigit() else None, int(status)))
        except ValueError:
            continue

    return jobs


def _when(dt: datetime | float | None) -> str:
    if dt is None:
        return "unknown"
    d = datetime.fromtimestamp(dt, TZ) if isinstance(dt, float) else dt.astimezone(TZ)
    today = datetime.now(TZ).date()
    clock = d.strftime("%-I:%M %p")

    return clock if d.date() == today else f"{d.strftime('%a')} {clock}"


def parse_cron(raw: object) -> list[CronJob]:
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    rows = jobs.values() if isinstance(jobs, dict) else jobs if isinstance(jobs, list) else []
    out = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        nxt = j.get("next_run_at")
        try:
            when = datetime.fromisoformat(nxt) if isinstance(nxt, str) else None
        except ValueError:
            when = None
        out.append(CronJob(str(j.get("name") or j.get("id") or "?"), bool(j.get("enabled")),
                           j.get("last_status") if isinstance(j.get("last_status"), str) else None,
                           int(j.get("failure_streak") or 0), when))

    return out


def agent_runs(db: Path, since: float) -> list[AgentRun]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
    try:
        rows = con.execute("SELECT source, COALESCE(title, ''), started_at, ended_at FROM sessions "
                           "WHERE started_at > ? ORDER BY started_at DESC LIMIT 40", (since,)).fetchall()
    finally:
        con.close()

    return [AgentRun(str(s), str(t), float(a), float(e) if e is not None else None) for s, t, a, e in rows]


async def _launchctl() -> str:
    p = await asyncio.create_subprocess_exec("/bin/launchctl", "list", stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(p.communicate(), 5)

    return out.decode("utf-8", "replace")


async def _tunnel_up() -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(*LINUX_TUNNEL), 1.0)
        w.close()

        return True
    except (TimeoutError, OSError):
        return False


async def read_status(src: StatusSources, pod_bots: list[dict] | None = None) -> str:
    """A few plain lines for the fast model. Each source fails on its own ("unknown") without sinking the rest."""
    lines: list[str] = []
    home = src.agent_home
    now = src.now()
    try:
        g = json.loads((home / "gateway_state.json").read_text())
        plats = g.get("platforms") or {}
        up = [PLATFORM_NAMES.get(k, k) for k, v in plats.items()
              if (v.get("state") if isinstance(v, dict) else v) == "connected"]
        down = [PLATFORM_NAMES.get(k, k) for k, v in plats.items()
                if (v.get("state") if isinstance(v, dict) else v) != "connected"]
        lines.append(f"Deep gateway: {g.get('gateway_state') or 'unknown'}; connected: {', '.join(up) or 'none'}"
                     + (f"; not connected: {', '.join(down)}" if down else "") + ".")
    except (OSError, ValueError, AttributeError) as e:
        lines.append(f"Deep gateway: unknown ({type(e).__name__}).")

    try:
        runs = agent_runs(home / "state.db", now - 6 * 3600)
        open_runs = [r for r in runs if r.ended_at is None and now - r.started_at < 3 * 3600]
        done = [r for r in runs if r.ended_at is not None]
        if open_runs:
            lines.append("Deep is working on: " + "; ".join(
                f"{r.title or 'an untitled run'} ({r.source}, since {_when(r.started_at)})"
                for r in open_runs[:4]) + ".")
        else:
            lines.append("Deep is not running anything right now.")
        if done:
            lines.append(f"Last Deep run: {done[0].title or 'untitled'} ({done[0].source}), "
                         f"finished {_when(done[0].ended_at)}.")
    except (OSError, sqlite3.Error) as e:
        lines.append(f"Deep runs: unknown ({type(e).__name__}).")

    try:
        jobs = [j for j in parse_cron(json.loads((home / "cron" / "jobs.json").read_text())) if j.enabled]
        failing = [j for j in jobs if j.failure_streak > 0 or (j.last_status not in (None, "ok"))]
        # SAFETY: the filter drops None next_run_at; sorted's key type is not inferred across the lambda.
        upcoming = sorted((j for j in jobs if j.next_run_at is not None), key=lambda j: j.next_run_at)  # type: ignore[arg-type,return-value]
        msg = f"Scheduled Deep bots: {len(jobs)} enabled"
        fails = ", ".join(
            f"{j.name} ({j.last_status or str(j.failure_streak) + ' fails'})" for j in failing)
        msg += f"; failing: {fails}" if failing else "; none failing"
        if upcoming:
            msg += f"; next up: {upcoming[0].name} at {_when(upcoming[0].next_run_at)}"
        lines.append(msg + ".")
    except (OSError, ValueError) as e:
        lines.append(f"Scheduled Deep bots: unknown ({type(e).__name__}).")

    try:
        jobs_l = parse_launchctl(await (src.launchctl or _launchctl)())
        guard_jobs = [j for j in jobs_l if j.label.startswith("com.skwoop.guard.")]
        bad = [j for j in guard_jobs if j.pid is None and j.last_exit != 0]
        lines.append(f"Guard: {sum(j.pid is not None for j in guard_jobs)} of {len(guard_jobs)} jobs running"
                     + (f"; last exit not clean: {', '.join(j.label.removeprefix('com.skwoop.guard.') for j in bad)}"
                        if bad else "") + ".")
        for label in WATCHED_JOBS:
            j = next((x for x in jobs_l if x.label == label), None)
            name = "Deep gateway process" if label.startswith("ai.skwoop") else "BlueBubbles"
            state = "not loaded" if j is None else "running" if j.pid else f"stopped (exit {j.last_exit})"
            lines.append(f"{name}: " + state + ".")
    except (TimeoutError, OSError) as e:
        lines.append(f"Guard: unknown ({type(e).__name__}).")

    lines.append("Remote host: " + ("connected (its tunnel is up)." if await (src.tunnel_up or _tunnel_up)()
                                         else "not connected (no tunnel)."))
    for b in pod_bots or []:
        seen = b.get("last_seen_ts")
        lines.append(f"Reported to the pod: {b.get('name')}: {b.get('state')}"
                     + (f", last seen {_when(float(seen))}" if isinstance(seen, (int, float)) else "") + ".")

    return "\n".join(lines)
