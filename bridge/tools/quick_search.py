"""The pod's quick web search through the configured deep-model base URL (a dummy bearer; the endpoint attaches
its own credential, never an API key). One short answer.

Built-in search first. Pages, files and anything a quick search can't settle go to the deep model.
max_tool_calls=1 holds a question to one search call. SearchBudget keeps a daily count and spend in
~/.skwoop/search-budget.json and refuses past SEARCH_DAILY_CAP (default 25/day).
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from lean_http import ClientSession, ClientTimeout

log = logging.getLogger("skwoop.search")

PROXY_BASE = os.environ.get("DEEP_MODEL_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
DUMMY_BEARER = "harness-via-deep-proxy"  # the endpoint replaces this; it is not an API key
TICKS_PER_USD = 10_000_000_000
DEFAULT_STATE = Path.home() / ".skwoop" / "search-budget.json"
INSTRUCTIONS = ("Search the web and answer in one or two short spoken sentences: the key fact first, with its date "
                "when it matters. No URLs, no markdown, no lists. If the results don't settle it, say so plainly.")


class SearchRefused(Exception):
    pass


@dataclass(frozen=True)
class SearchAnswer:
    text: str
    searches: int
    cost_usd: float


class SearchBudget:
    def __init__(self, path: Path | None = None, cap: int | None = None, today: Callable[[], date] = date.today):
        self.path = path or Path(os.environ.get("SEARCH_BUDGET_FILE") or DEFAULT_STATE).expanduser()
        self.cap = cap if cap is not None else int(os.environ.get("SEARCH_DAILY_CAP") or 25)
        self.today = today

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict) or raw.get("day") != self.today().isoformat():
            kept = raw if isinstance(raw, dict) else {}
            raw = {"day": self.today().isoformat(), "searches": 0, "usd": 0.0,
                   "total_searches": kept.get("total_searches", 0), "total_usd": kept.get("total_usd", 0.0)}

        return raw

    def check(self) -> None:
        s = self._load()
        if s["searches"] >= self.cap:
            raise SearchRefused(f"daily search cap {self.cap} reached")

    def spend(self, searches: int, usd: float) -> dict[str, Any]:
        s = self._load()
        s["searches"] += searches
        s["usd"] = round(s["usd"] + usd, 6)
        s["total_searches"] = s.get("total_searches", 0) + searches
        s["total_usd"] = round(s.get("total_usd", 0.0) + usd, 6)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(self.path)

        return s


def parse_answer(data: object, max_chars: int = 170) -> SearchAnswer:
    """Responses API body -> the answer text, how many searches ran, and what it cost."""
    if not isinstance(data, dict):
        raise TypeError("not a response object")
    text = ""
    for item in data.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text += str(part.get("text") or "")
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    tools = usage.get("server_side_tool_usage_details") or {}
    searches = int(tools.get("web_search_calls") or usage.get("num_server_side_tools_used") or 0) \
        if isinstance(tools, dict) else int(usage.get("num_server_side_tools_used") or 0)
    ticks = usage.get("cost_in_usd_ticks")
    usd = float(ticks) / TICKS_PER_USD if isinstance(ticks, (int, float)) else 0.005 * max(1, searches)

    return SearchAnswer(brief(speakable(text), max_chars), searches, usd)


def brief(text: str, max_chars: int = 170) -> str:
    """At most ~max_chars of whole sentences for the speaker; an over-long first sentence is cut at a clause break.
    The search model often ignores "one or two short sentences" (a 420-character run-on took 8 s to synthesize)."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text.strip()) if p.strip()]
    out = ""
    for part in parts:
        if out and len(out) + 1 + len(part) > max_chars:
            break
        out = f"{out} {part}".strip()
        if len(out) > max_chars:
            cut = max(out.rfind(sep, 0, max_chars) for sep in (": ", " — ", "—", "; ", ", "))
            out = (out[:cut] if cut > max_chars // 3 else out[:max_chars].rsplit(" ", 1)[0]).rstrip(",;:— ") + "."
            break
    return out


def speakable(text: str) -> str:
    """Drop what the model adds for a screen: [[1]](https://...) citations, bare links, **bold**."""
    text = re.sub(r"\[\[\d+\]\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\(https?://[^)]*\)", r"\1", text)
    text = re.sub(r"\(?https?://\S+\)?", "", text)
    text = re.sub(r"[*_`#]+", "", text)

    return " ".join(text.split()).replace(" .", ".").replace(" ,", ",")


class QuickSearch:
    def __init__(self, http: ClientSession, model: str, budget: SearchBudget, base: str = PROXY_BASE,
                 timeout_s: float = 20.0):
        self.http, self.model, self.budget, self.base, self.timeout_s = http, model, budget, base, timeout_s

    async def ask(self, query: str, instructions: str = INSTRUCTIONS, max_output_tokens: int = 120,
                  max_chars: int = 170) -> SearchAnswer:
        """max_chars: the spoken brief's length (brief()); the harness' web lead asks for a longer one."""
        self.budget.check()
        body = {"model": self.model, "instructions": instructions, "max_output_tokens": max_output_tokens,
                "max_tool_calls": 1, "input": [{"role": "user", "content": query[:300]}],
                "tools": [{"type": "web_search"}]}
        async with self.http.post(f"{self.base}/responses", json=body, timeout=ClientTimeout(total=self.timeout_s),
                                  headers={"Authorization": f"Bearer {DUMMY_BEARER}"}) as r:
            if r.status != 200:
                raise RuntimeError(f"search HTTP {r.status}")
            answer = parse_answer(await r.json(), max_chars)
        state = self.budget.spend(max(1, answer.searches), answer.cost_usd)
        log.info("quick search %r: %d search(es), $%.4f (today %d, $%.3f)", query[:60], answer.searches,
                 answer.cost_usd, state["searches"], state["usd"])

        return answer
