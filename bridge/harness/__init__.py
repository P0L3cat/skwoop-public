"""The pod harness: one ask in, sentences out. toolsets.pick chooses the tier, the tools and the reads; the reads run
first, in parallel, each under a time cap; one prompt (persona, rules, this ask's hint, the facts, the last 3 turns)
goes to the pod agent on the subscription proxy. A failure before the first sentence raises HandOff, and the bridge
re-runs the turn on Deep; after it, the turn ends on LOST_LINE.

Environment: SKWOOP_HARNESS=1 turns it on (default off: every model turn goes to Deep). SKWOOP_DEEP_FALLBACK=0 says
CANT_LINE instead of handing off (default 1). SKWOOP_REMINDERS=0 turns the remind tool off (default 1).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Protocol
from zoneinfo import ZoneInfo

import notes_pick
import router
from tools.grokipedia import Lead, topic_of
from tools.house_tools import Escalate, HouseTools, Outcome, TurnTools
from memory.memory_write import MemoryWriter
from harness.pod_agent import (
    DEEP,
    FAST,
    AgentStats,
    AssistantMessage,
    ChatMessage,
    LimitReached,
    ModelClient,
    ModelUnavailable,
    SystemMessage,
    ToolFn,
    UserMessage,
    sentences,
)
from harness.prompt import ESCALATE_RULE, LEAD_INSTRUCTIONS, PERSONA, RULES
from tools.reminders import Reminders
from harness.toolsets import (
    READ_TOOL,
    SCHEMAS,
    TOOLSETS,
    Decline,
    JsonValue,
    Read,
    ToolName,
    Toolset,
    is_undo,
    offered,
    pick,
    search_query,
)

log = logging.getLogger("skwoop.harness")

TZ = ZoneInfo("America/Chicago")
LOST_LINE = "Sorry, I lost that one."
CANT_LINE = "Sorry, I can't answer that one right now."
LIMIT_ALERT = "Grok limit hit: the pod is on basics only"
READ_CAP_S: Mapping[Read, float] = {"recall": 3.0, "web": 5.0}  # every other read: 1.5 s
FACT_CHARS = 1200
HISTORY_MESSAGES = 6  # the last 3 turns
HISTORY_CHARS = 240


class HandOff(Exception):
    """The harness gave up before its first sentence: the bridge re-runs the turn on Deep with these facts."""

    def __init__(self, reason: str, facts: list[str]) -> None:
        super().__init__(reason)
        self.reason, self.facts = reason, facts


class Wiki(Protocol):
    async def lead(self, topic: str) -> Lead | None: ...


@dataclass
class TurnStats(AgentStats):
    """One harness turn, for the bridge's turn profile line."""

    tier: str = ""
    kind: str = ""
    reads: str = ""  # "recall:2.11,snapshot:0.00"; a miss reads "recall:timeout"
    reads_at: float | None = None  # monotonic
    offered: str = ""
    fallback: str = ""

    def fields(self) -> str:
        ttft = f"{self.ttft:.2f}s" if self.ttft is not None else "-"
        return (f" harness=1 tier={self.tier} toolset={self.kind} offered={self.offered or '-'} "
                f"prefill={self.reads or '-'} usage_prompt_tokens={self.prompt_tokens} rounds={self.rounds} "
                f"ttft={ttft}" + (f" fallback={self.fallback}" if self.fallback else ""))


def calendar_window(heard: str) -> tuple[int, int]:
    """(day_offset, days): the exact day when the words say it, else the next 7 days (the prefetched window covers
    it, so it costs nothing). There is no calendar tool: offered one, the model re-read the day it already had."""
    exact = router.prepick(heard)
    if exact is not None and exact[0] == "calendar":
        return int(exact[1].get("day_offset", 0)), int(exact[1].get("days", 1))

    return 0, 7


def _usable(text: str | None) -> str:
    """A read's text, or "empty"/"error" when it can't be used (house_tools reports failures as "error: ..." lines)."""
    if text is None or not text.strip():
        return "empty"

    return "error" if text.lstrip().lower().startswith("error:") else "ok"


class Harness:
    def __init__(self, client: ModelClient, house: HouseTools, memory: MemoryWriter, wiki: Wiki | None = None,
                 reminders: Reminders | None = None, deep_fallback: bool = True,
                 alert: Callable[[str], None] = lambda text: None,
                 notes: Callable[[str], list[str] | None] = notes_pick.facts_for,
                 now: Callable[[], datetime] = lambda: datetime.now(TZ),
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.house, self.memory, self.wiki, self.reminders = client, house, memory, wiki, reminders
        self.deep_fallback, self.alert, self.notes, self.now, self.clock = deep_fallback, alert, notes, now, clock

    def pick(self, device: str, route: router.Route, text: str) -> Toolset | None:
        """The toolset for this ask, or None (the bridge's other paths). While this pod has a reminder in flight,
        "never mind" drops it and the answer to "what time?" goes back to the reminder toolset."""
        rem = self.reminders
        if rem is not None and rem.pending(device):
            if is_undo(text, cancel=True):
                return TOOLSETS["memory-write"]

            if rem.asking(device):
                return TOOLSETS["reminder"]

        picked = pick(route, text, reminders=rem is not None)

        return None if isinstance(picked, Decline) else picked

    async def turn(self, toolset: Toolset, heard: str, device: str, room: str, history: list[dict[str, str]],
                   stats: TurnStats) -> AsyncIterator[str]:
        stats.tier, stats.kind = toolset.tier, toolset.kind
        hold = router.hold_actions(heard)
        tools = self.house.for_turn(device, room, dry_run=False, hold=hold)
        facts, missed = await self._read_all(toolset.reads, heard, tools, stats)
        names = offered(toolset, missed, self.deep_fallback, question=hold is not None)
        stats.offered = ",".join(names)
        messages = self.prompt(heard, room, toolset, names, facts, history, hold)
        spoken = 0
        try:
            async for sentence in sentences(self.client, FAST if toolset.tier == "fast" else DEEP, messages,
                                            tuple(SCHEMAS[n] for n in names), self._registry(names, tools), stats,
                                            self.clock):
                spoken += 1
                yield sentence
        except LimitReached as e:
            log.warning("[%s] harness: %s", room, e)
            stats.fallback = "limit"
            if e.pool:
                self.alert(LIMIT_ALERT)
            if spoken == 0:
                yield e.line
        except (Escalate, ModelUnavailable) as e:
            reason = e.reason if isinstance(e, Escalate) else str(e)
            log.info("[%s] harness %s after %d sentence(s): %s", room,
                     "escalates" if isinstance(e, Escalate) else "failed", spoken, reason)
            if spoken:
                yield LOST_LINE
            elif not self.deep_fallback:
                stats.fallback = "none"
                yield CANT_LINE
            else:
                stats.fallback = "deep"
                raise HandOff(reason, [f"{n}: {t}" for n, t in facts] + stats.facts) from None

    def prompt(self, heard: str, room: str, toolset: Toolset, names: tuple[ToolName, ...],
               facts: list[tuple[str, str]], history: list[dict[str, str]], hold: str | None) -> list[ChatMessage]:
        when = self.now().strftime("%A %B %-d %Y, %-I:%M %p")
        parts = [PERSONA, f"It is {when} (America/Chicago). The user asked from the {room} pod.",
                 f"{RULES}\n{ESCALATE_RULE}" if "escalate" in names else RULES]
        if toolset.hint:
            parts.append(f"This ask: {toolset.hint}")
        if hold:
            parts.append(f"Don't change anything this turn: the user {hold}.")
        if facts:
            parts.append("Facts (looked up just now):\n" + "\n".join(f"[{n}] {t}" for n, t in facts))
        messages: list[ChatMessage] = [SystemMessage(role="system", content="\n\n".join(parts))]
        for m in history[-HISTORY_MESSAGES:]:
            content = str(m.get("content") or "").strip()[:HISTORY_CHARS]
            match m.get("role"):
                case "user":
                    messages.append(UserMessage(role="user", content=content))
                case "assistant":
                    messages.append(AssistantMessage(role="assistant", content=content))
        messages.append(UserMessage(role="user", content=heard.strip()[:500]))

        return messages

    async def _read(self, name: Read, heard: str, tools: TurnTools) -> str | None:
        match name:
            case "calendar":
                day, days = calendar_window(heard)
                return (await tools.run("calendar", {"day_offset": day, "days": days})).text
            case "notes":
                found = await asyncio.to_thread(self.notes, heard)
                return None if found is None else "\n".join(found)
            case "recall":
                return (await tools.run("recall_memory", {"query": heard})).text
            case "forecast" | "bot_status":
                return (await tools.run(name, {})).text
            case "snapshot":
                return await self.house.snapshot(tools.device)
            case "list":
                which = "reminders" if "reminder" in heard.lower() else "shopping"
                return (await tools.run("lists", {"action": "read", "list": which})).text
            case "web":
                if self.house.search is None:
                    return None
                return (await self.house.search.ask(search_query(heard), LEAD_INSTRUCTIONS, 260, 420)).text
            case "wiki":
                lead = None if self.wiki is None else await self.wiki.lead(topic_of(heard))
                return None if lead is None else f"Grokipedia, {lead.title}: {lead.text}"

    async def _read_all(self, reads: tuple[Read, ...], heard: str, tools: TurnTools,
                        stats: TurnStats) -> tuple[list[tuple[str, str]], tuple[ToolName, ...]]:
        """All reads at once, each under its cap. Returns the facts in the toolset's order, and the tools of the
        reads that missed (timeout, error, nothing), so the model can still try them."""

        async def one(name: Read) -> tuple[Read, str | None, str]:
            t0 = self.clock()
            try:
                text = await asyncio.wait_for(self._read(name, heard, tools), READ_CAP_S.get(name, 1.5))
                outcome = _usable(text)
            except TimeoutError:
                text, outcome = None, "timeout"
            except Exception as e:  # noqa: BLE001 - a failing read must never fail the turn; the tool is offered
                log.warning("read %s failed: %s", name, type(e).__name__)
                text, outcome = None, "error"
            if outcome != "ok" or text is None:
                return name, None, f"{name}:{outcome}"

            return name, text.strip()[:FACT_CHARS], f"{name}:{self.clock() - t0:.2f}"

        done = await asyncio.gather(*(one(r) for r in reads))
        stats.reads, stats.reads_at = ",".join(p for _, _, p in done), self.clock()
        facts: list[tuple[str, str]] = [(n, t) for n, t, _ in done if t is not None]
        missed = tuple(READ_TOOL[n] for n, t, _ in done if t is None and n in READ_TOOL)

        return facts, missed

    async def _forget_last(self, device: str, dry_run: bool, args: dict[str, JsonValue]) -> Outcome:
        """A held reminder goes first (it isn't written yet); otherwise the memory write from the last minute."""
        if self.reminders is not None and (dropped := self.reminders.cancel(device)) is not None:
            return dropped

        return await self.memory.forget_last(device, dry_run)

    def _registry(self, names: tuple[ToolName, ...], tools: TurnTools) -> dict[str, ToolFn]:
        """Only this ask's tools. House tools go through TurnTools.run (the hold rule, dry runs, error lines)."""
        out: dict[str, ToolFn] = {}
        for name in names:
            match name:
                case "remember":
                    out[name] = partial(self.memory.remember, device=tools.device, room=tools.room,
                                        dry_run=tools.dry_run)
                case "remind" if self.reminders is not None:
                    out[name] = partial(self.reminders.remind, device=tools.device, dry_run=tools.dry_run)
                case "forget_last":
                    out[name] = partial(self._forget_last, tools.device, tools.dry_run)
                case "remind" | "escalate":
                    pass
                case _:
                    out[name] = partial(tools.run, name)

        return out
