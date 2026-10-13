"""ElevenLabs credit guard. The saved card on the ElevenLabs account is expired, so going past the plan's credits
would leave an unpaid overage invoice: live ElevenLabs TTS stops at a floor and the bridge speaks with xAI instead.

A local, persisted spend counter (no API call: the key has no user_read scope):
  ELEVEN_CREDIT_BALANCE    credits left on the account when you set it (5863 on Oct 2). Changing the value re-seeds
                           the counter. Unset and no state file = fail closed (no live ElevenLabs, xAI only).
  ELEVEN_CREDIT_FLOOR      stop live ElevenLabs when the estimated balance would drop below this (default 1000)
  ELEVEN_CREDIT_STATE      the counter file (default ~/.skwoop/eleven-credits.json, written atomically)
  ELEVEN_CHARS_PER_CREDIT  estimate before a call (default 20); once character-cost headers have been seen, the
                           stricter of this and the measured rate is used
Every charge uses the response's character-cost header when present. Every block is logged (WARNING) and counted.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, TypedDict

log = logging.getLogger("skwoop.credits")

DEFAULT_STATE = Path.home() / ".skwoop" / "eleven-credits.json"
TYPICAL_REPLY_CHARS = 60


class CreditGate(Protocol):
    def allow(self, text: str) -> tuple[bool, str]: ...

    def charge(self, text: str, cost: float | None) -> float: ...


@dataclass
class CreditState:
    seed: float | None = None               # credits left when seeded
    env_balance: float | None = None        # the ELEVEN_CREDIT_BALANCE that seeded it
    seeded_at: str | None = None
    spent: float = 0.0                      # since the seed
    spent_total: float = 0.0
    calls: int = 0
    blocked: int = 0
    measured_chars: float = 0.0             # characters of calls that had a character-cost header ...
    measured_cost: float = 0.0              # ... and what those headers said
    last_block: str | None = None
    last_call: str | None = None


class GuardStatus(TypedDict):
    remaining_est: float | None
    floor: float
    spent_since_seed: float
    calls: int
    blocked: int
    seeded_at: str | None
    live_allowed: bool
    state: str


def _num(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None

    return float(raw)


def parse_state(raw: object) -> CreditState:
    """The state file -> CreditState. Unknown or mistyped fields fall back to their defaults."""
    if not isinstance(raw, dict):
        return CreditState()

    def num(key: str, default: float) -> float:
        v = _num(raw.get(key))

        return default if v is None else v

    def text(key: str) -> str | None:
        v = raw.get(key)

        return v if isinstance(v, str) else None

    return CreditState(seed=_num(raw.get("seed")), env_balance=_num(raw.get("env_balance")),
                       seeded_at=text("seeded_at"), spent=num("spent", 0.0), spent_total=num("spent_total", 0.0),
                       calls=int(num("calls", 0)), blocked=int(num("blocked", 0)),
                       measured_chars=num("measured_chars", 0.0), measured_cost=num("measured_cost", 0.0),
                       last_block=text("last_block"), last_call=text("last_call"))


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None

    try:
        return float(raw)
    except ValueError:
        log.error("%s=%r is not a number: ignored", name, raw)

        return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class CreditGuard:
    def __init__(self, state_path: Path | str | None = None, balance: float | None = None,
                 floor: float | None = None, chars_per_credit: float | None = None):
        self.path = Path(state_path or os.environ.get("ELEVEN_CREDIT_STATE", "").strip() or DEFAULT_STATE).expanduser()
        self.env_balance = balance if balance is not None else _env_float("ELEVEN_CREDIT_BALANCE")
        env_floor = _env_float("ELEVEN_CREDIT_FLOOR")
        self.floor = floor if floor is not None else (1000.0 if env_floor is None else env_floor)

        self.cpc = chars_per_credit or _env_float("ELEVEN_CHARS_PER_CREDIT") or 20.0
        self._lock = threading.Lock()
        self.state = self._load()
        if self.env_balance is not None and self.state.env_balance != self.env_balance:
            self.state.seed = self.state.env_balance = self.env_balance
            self.state.spent, self.state.seeded_at = 0.0, _now()
            log.info("credit guard seeded: %.0f credits left, floor %.0f", self.env_balance, self.floor)
            self._save()

    def _load(self) -> CreditState:
        try:
            return parse_state(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            return CreditState()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.state), indent=1))
            os.replace(tmp, self.path)
        except OSError as e:
            log.error("credit counter not saved (%s)", type(e).__name__)

    def estimate(self, text: str) -> float:
        """Credits a call will probably cost: the stricter of ELEVEN_CHARS_PER_CREDIT and what the headers measured."""
        cpc = self.cpc
        if self.state.measured_chars and self.state.measured_cost:
            cpc = min(cpc, self.state.measured_chars / self.state.measured_cost)

        return float(max(1, math.ceil(len(text) / cpc)))

    def remaining(self) -> float | None:
        if self.state.seed is None:
            return None

        return self.state.seed - self.state.spent

    def allow(self, text: str) -> tuple[bool, str]:
        """(ok, why). ok=False: don't call ElevenLabs; speak with the fallback."""
        rem = self.remaining()
        if rem is None:
            why = "no ELEVEN_CREDIT_BALANCE set (credit guard fails closed)"
        elif rem - self.estimate(text) < self.floor:
            why = f"credit floor: ~{rem:.0f} left, floor {self.floor:.0f}"
        else:
            return True, ""

        with self._lock:
            self.state.blocked += 1
            self.state.last_block = f"{_now()} {why} ({len(text)} chars)"
            self._save()

        log.warning("ElevenLabs live TTS blocked: %s -> xAI TTS", why)

        return False, why

    def charge(self, text: str, cost: float | None) -> float:
        c = float(cost) if cost else self.estimate(text)
        with self._lock:
            self.state.spent += c
            self.state.spent_total += c
            self.state.calls += 1
            if cost:
                self.state.measured_chars += len(text)
                self.state.measured_cost += c
            self.state.last_call = f"{_now()} {c:.1f} credits, {len(text)} chars, {'measured' if cost else 'estimated'}"
            self._save()

        rem = self.remaining()
        if rem is not None and rem < self.floor + 200:
            log.warning("ElevenLabs credits low: ~%.0f left (floor %.0f)", rem, self.floor)

        return c

    def describe(self) -> GuardStatus:
        rem = self.remaining()

        return {"remaining_est": None if rem is None else round(rem, 1), "floor": self.floor,
                "spent_since_seed": round(self.state.spent, 1), "calls": self.state.calls,
                "blocked": self.state.blocked, "seeded_at": self.state.seeded_at,
                "live_allowed": rem is not None and rem - self.estimate("x" * TYPICAL_REPLY_CHARS) >= self.floor,
                "state": str(self.path)}


class BlockAll:
    """A gate that refuses every live call (POST /speak-preview simulate=floor): proves the xAI fallback path."""

    def allow(self, text: str) -> tuple[bool, str]:
        log.warning("ElevenLabs live TTS blocked: simulated credit floor -> xAI TTS")

        return False, "simulated credit floor"

    def charge(self, text: str, cost: float | None) -> float:
        return 0.0
