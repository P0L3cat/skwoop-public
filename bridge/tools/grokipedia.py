"""A Grokipedia lead for basic-info asks ("who was ...", "what is a ..."): one GET to the unofficial, unauthenticated
typeahead endpoint (`/api/typeahead?query=<topic>&limit=5`, ~0.2-0.4 s), whose best hit carries a plain-text
snippet of the article's opening. Only an article whose title IS the topic counts. Leads are kept in memory for the
life of the bridge. Nothing is sent but the topic words; no model call, no cost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lean_http import ClientSession, ClientTimeout

BASE = "https://grokipedia.com"
USER_AGENT = "skwoop-pod/1.0 (household voice assistant)"
_TOPIC = re.compile(
    r"^((hey |ok |okay )?skwoop,? )?((can|could|would) you |please )*"
    r"(who (was|were|is|are)|what (is|are|was|were)|what'?s|tell me (about|what)|explain|define|describe|"
    r"how (does|do|did))\s+(an? |the )?",
    re.IGNORECASE,
)
_TOPIC_TAIL = re.compile(r"(\s+(mean|work|works|happen|for me|please))*[.?!\s]*$", re.IGNORECASE)


@dataclass(frozen=True)
class Lead:
    title: str
    text: str


def topic_of(heard: str) -> str:
    """ "Who was Abraham Lincoln?" -> "Abraham Lincoln"; "How do magnets work?" -> "magnets"."""
    return _TOPIC_TAIL.sub("", _TOPIC.sub("", heard.strip(), count=1)).strip() or heard.strip()


def _norm(title: str) -> str:
    return " ".join(re.sub(r"\([^)]*\)", "", title).casefold().replace("-", " ").split())


def _forms(topic: str) -> list[str]:
    """The topic, then its singular ("magnets" -> "magnet"), so "how do magnets work" finds the "Magnet" article."""
    t = _norm(topic)
    if t.endswith("ies"):
        return [t, t[:-3] + "y"]

    return [t, t[:-1]] if t.endswith("s") and not t.endswith("ss") else [t]


def _whole_sentences(snippet: str) -> str:
    """A snippet cut mid-sentence ("... 1.5 millimeters in…") back to its last full sentence."""
    text = snippet.strip()
    if not text.endswith(("…", "...")):
        return text

    end = text.rfind(". ")

    return text[: end + 1] if end > 0 else ""


def best_lead(topic: str, raw: object) -> Lead | None:
    """typeahead's {"results": [{"title", "snippet", "relevanceScore"}, ...]} (the boundary) -> the exact-title hit's
    snippet, cut back to its last full sentence. A near miss ("The Magnets", a band) is worse than no lead."""
    results = raw.get("results") if isinstance(raw, dict) else None
    want = set(_forms(topic))
    best: tuple[float, Lead] | None = None
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue

        title, snippet, score = item.get("title"), item.get("snippet"), item.get("relevanceScore")
        if not (isinstance(title, str) and isinstance(snippet, str) and isinstance(score, int | float)):
            continue

        text = _whole_sentences(snippet)
        if _norm(title) in want and len(text) > 20 and (best is None or score > best[0]):
            best = (float(score), Lead(title, text))

    return best[1] if best else None


class Grokipedia:
    def __init__(self, http: ClientSession, base: str = BASE, timeout_s: float = 1.5) -> None:
        self.http, self.base, self.timeout_s = http, base.rstrip("/"), timeout_s
        self.cache: dict[str, Lead | None] = {}

    async def lead(self, topic: str) -> Lead | None:
        """The lead for a topic, or None (no matching article). Network errors raise (the read counts as missed)."""
        key = _norm(topic)
        if key in self.cache:
            return self.cache[key]

        lead: Lead | None = None
        for query in _forms(topic):
            async with self.http.get(f"{self.base}/api/typeahead", params={"query": query, "limit": "5"},
                                     headers={"User-Agent": USER_AGENT},
                                     timeout=ClientTimeout(total=self.timeout_s)) as r:
                r.raise_for_status()
                if (lead := best_lead(topic, await r.json(content_type=None))) is not None:
                    break

        self.cache[key] = lead

        return lead
