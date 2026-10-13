"""`remember` and `forget_last`: durable household facts into Skwoop's memory service.

- Refused in code before any call: health details (never kept from the pod), fragments, long text.
- Dedupe: a recall first; if a top hit already says the same thing, nothing is written ("I already have that.").
- The write: POST /memories, strategy grokbot_verbatim (stored as is), one document_id per fact, tags
  source:skwoop-pod + room:<room>. Undo: "forget that" within UNDO_S on the same pod deletes that document.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypedDict

from lean_http import ClientError, ClientSession, ClientTimeout

from tools.house_tools import Outcome
from memory.memory_recall import MemoryFact, RecallError, parse_recall

log = logging.getLogger("skwoop.memory_write")

SERVICE_URL = os.environ.get("MEMORY_SERVICE_URL", "http://127.0.0.1:9177/v1/default/banks/skwoop")
UNDO_S = 60.0
SAME = 0.75  # word overlap (Jaccard) at which a recalled fact counts as the same fact
HEALTH = re.compile(
    r"\b(health|medic(al|ine|ation|ations)?|doctor|dr\.|nurse|hospital|clinic|diagnos\w*|symptoms?|illness|sick|"
    r"disease|disorder|condition|allerg\w*|prescri\w*|pills?|dos(e|age)|insulin|blood|glucose|sugar levels?|"
    r"pregnan\w*|period|menstrua\w*|cycle|fertility|ovulat\w*|surgery|therap(y|ist)|depress\w*|anxiety|adhd|"
    r"autis\w*|cancer|diabet\w*|asthma|heart rate|blood pressure|weight|bmi|calories|vaccin\w*|injur\w*|pain|"
    r"migraine|covid|flu|infection|mental|psych\w*)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9']+")
_STOP = frozenset("a an the is are was were be to of and or in on at for that this it its my our your his her".split())

SAID_SAVED = "Got it: {fact}"
SAID_HAVE = "I already have that."
SAID_HEALTH = "I don't save health details from the pod."
SAID_UNCLEAR = "I didn't catch a fact to remember."
SAID_FORGOT = "Okay, forgotten."
SAID_NOTHING = "There's nothing from the last minute to forget."
SAID_FAILED = "I couldn't reach my memory just now."


class MemoryWriteError(RuntimeError):
    """The memory daemon refused the write or delete, or didn't answer."""


class RetainItem(TypedDict):
    content: str
    timestamp: str
    context: str
    metadata: dict[str, str]
    document_id: str
    tags: list[str]
    strategy: str


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower().replace("’", "'")) if w not in _STOP}


def same_fact(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)

    return bool(wa and wb) and (len(wa & wb) / len(wa | wb) >= SAME or wa <= wb)


def retain_item(fact: str, now: datetime, device: str, room: str) -> RetainItem:
    at = now.isoformat(timespec="seconds")
    doc = f"skwoop-pod-{now:%Y%m%d-%H%M%S}-{hashlib.sha256(fact.encode()).hexdigest()[:10]}"

    return RetainItem(content=fact, timestamp=at, context="household fact (skwoop pod)",
                      metadata={"source": "skwoop-pod", "at": at, "device": device}, document_id=doc,
                      tags=["source:skwoop-pod", f"room:{room}"], strategy="grokbot_verbatim")


class MemoryStore(Protocol):
    """What MemoryWriter calls: the memory service in the bridge, a fake in tests."""

    async def recall(self, query: str) -> tuple[MemoryFact, ...]: ...

    async def retain(self, item: RetainItem) -> None: ...

    async def delete(self, doc_id: str) -> None: ...


class MemoryService:
    def __init__(self, http: ClientSession, service_url: str = SERVICE_URL, timeout_s: float = 6.0) -> None:
        self.http, self.service_url, self.timeout = http, service_url.rstrip("/"), ClientTimeout(total=timeout_s)

    async def _send(self, method: str, path: str, body: object | None) -> object:
        try:
            async with self.http.request(method, f"{self.service_url}{path}", json=body, timeout=self.timeout) as r:
                if r.status != 200:
                    raise MemoryWriteError(f"{method} {path}: HTTP {r.status}")
                raw: object = await r.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as e:
            raise MemoryWriteError(f"{method} {path}: {type(e).__name__}") from e

        if path != "/memories/recall" and (not isinstance(raw, dict) or raw.get("success") is not True):
            raise MemoryWriteError(f"{method} {path}: the daemon did not report success")

        return raw

    async def recall(self, query: str) -> tuple[MemoryFact, ...]:
        body = {"query": query, "budget": "low", "max_tokens": 400, "types": ["world", "experience", "observation"]}
        try:
            return parse_recall(await self._send("POST", "/memories/recall", body))
        except MemoryWriteError as e:
            raise RecallError(str(e)) from e

    async def retain(self, item: RetainItem) -> None:
        await self._send("POST", "/memories", {"items": [item], "async": False})

    async def delete(self, doc_id: str) -> None:
        await self._send("DELETE", f"/documents/{doc_id}", None)


@dataclass(frozen=True)
class Written:
    document_id: str
    at: float


class MemoryWriter:
    def __init__(self, store: MemoryStore, now: Callable[[], datetime],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self.now, self.clock = store, now, clock
        self.last: dict[str, Written] = {}  # device -> its latest write, for forget_last

    async def remember(self, args: Mapping[str, object], device: str, room: str, dry_run: bool = False) -> Outcome:
        """The remember tool: args {"fact": "..."} from the model."""
        raw = args.get("fact")
        fact = " ".join(raw.split()) if isinstance(raw, str) else ""
        if HEALTH.search(fact):
            return Outcome("refused: health details are never kept from the pod", say=SAID_HEALTH)

        if len(_WORD.findall(fact.lower())) < 3 or len(fact) > 300:
            return Outcome("refused: not one short fact; ask what to remember", say=SAID_UNCLEAR)

        try:
            hits = await self.store.recall(fact)
        except RecallError as e:
            log.warning("memory write: dedupe recall failed (%s); not writing", e)
            return Outcome("error: memory didn't answer", say=SAID_FAILED)

        if any(same_fact(fact, h.text) for h in hits[:3]):
            return Outcome("already known: nothing written", say=SAID_HAVE)

        item = retain_item(fact, self.now(), device, room)
        if dry_run:
            return Outcome(f"dry run: not written ({item['document_id']})", say=SAID_SAVED.format(fact=fact))

        try:
            await self.store.retain(item)
        except MemoryWriteError as e:
            log.warning("memory write failed: %s", e)
            return Outcome("error: memory didn't take it", say=SAID_FAILED)

        self.last[device] = Written(item["document_id"], self.clock())
        log.info("memory write %s (%d chars, room %s)", item["document_id"], len(fact), room)

        return Outcome(f"saved as {item['document_id']}", say=SAID_SAVED.format(fact=fact))

    async def forget_last(self, device: str, dry_run: bool = False) -> Outcome:
        """forget_last: undo this pod's write from the last UNDO_S seconds."""
        w = self.last.get(device)
        if w is None or self.clock() - w.at > UNDO_S:
            return Outcome("nothing written from this pod in the last minute", say=SAID_NOTHING)

        if not dry_run:
            try:
                await self.store.delete(w.document_id)
            except MemoryWriteError as e:
                log.warning("memory undo failed: %s", e)
                return Outcome("error: memory didn't delete it", say=SAID_FAILED)

        del self.last[device]
        log.info("memory undo %s", w.document_id)

        return Outcome(f"deleted {w.document_id}", say=SAID_FORGOT)
