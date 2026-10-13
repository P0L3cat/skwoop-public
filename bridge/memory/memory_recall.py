"""One read of Skwoop's long-term memory for the fast tier: the memory service's recall endpoint
(MEMORY_RECALL_URL). Read-only: nothing is retained from here.

Used for one-fact questions about Alex's people and pets ("do you remember who my wife is"), so they skip the deep
agent. A low budget keeps it to about two seconds; anything that needs more than a fact stays with Deep.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from lean_http import ClientError, ClientSession, ClientTimeout

log = logging.getLogger("skwoop.memory")

DEFAULT_URL = os.environ.get(
    "MEMORY_RECALL_URL", "http://127.0.0.1:9177/v1/default/banks/skwoop/memories/recall")
RECALL_TYPES = ("observation", "world", "experience")
MAX_FACTS = 6
FACT_CHARS = 240
TIMEOUT_S = 6.0


class RecallError(RuntimeError):
    """The memory daemon did not answer, or answered with something that is not a recall result."""


@dataclass(frozen=True)
class MemoryFact:
    kind: str
    text: str


def parse_recall(raw: object) -> tuple[MemoryFact, ...]:
    """The daemon's {"results": [{"type", "text", ...}, ...]} -> facts, best first. Unknown fields are ignored."""
    if not isinstance(raw, dict):
        raise RecallError("recall: expected a JSON object")

    results = raw.get("results")
    if not isinstance(results, list):
        raise RecallError("recall: expected a results list")

    out: list[MemoryFact] = []
    for item in results:
        if not isinstance(item, dict):
            continue

        text, kind = item.get("text"), item.get("type")
        if isinstance(text, str) and text.strip():
            out.append(MemoryFact(kind if isinstance(kind, str) else "", " ".join(text.split())))

    return tuple(out)


def facts_text(facts: tuple[MemoryFact, ...]) -> str:
    """What the fast model sees: the best few facts, deduplicated, each cut short."""
    seen: set[str] = set()
    lines: list[str] = []
    for f in facts:
        key = f.text.lower()
        if key in seen:
            continue

        seen.add(key)
        lines.append(f"- {f.text[:FACT_CHARS]}")
        if len(lines) == MAX_FACTS:
            break

    if not lines:
        return "Skwoop's memory has nothing on this."

    return "From Skwoop's long-term memory (best match first; newer wins over older):\n" + "\n".join(lines)


@dataclass(frozen=True)
class MemoryRecall:
    http: ClientSession
    url: str = DEFAULT_URL

    @classmethod
    def from_env(cls, http: ClientSession) -> MemoryRecall:
        return cls(http, os.environ.get("SKWOOP_MEMORY_RECALL_URL") or DEFAULT_URL)

    async def __call__(self, query: str) -> str:
        body = {"query": query, "budget": "low", "max_tokens": 600, "types": list(RECALL_TYPES)}
        try:
            async with self.http.post(self.url, json=body, timeout=ClientTimeout(total=TIMEOUT_S)) as r:
                if r.status != 200:
                    raise RecallError(f"recall HTTP {r.status}")

                raw: object = await r.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as e:
            raise RecallError(f"recall failed: {type(e).__name__}") from e

        return facts_text(parse_recall(raw))
