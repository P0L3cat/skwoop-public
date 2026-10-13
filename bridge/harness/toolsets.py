"""Which tier, which tools and which pre-model reads one pod ask gets. Rules only, no model call (microseconds).

pick(route, text) gives a Toolset from the TOOLSETS table, or a Decline: music and note writes stay with the bridge's
Spotify path and Deep. An ask whose facts can be read first (notes, a memory fact, a live fact) is phrased by the
fast model in one round; asks that need reasoning over tools go deep.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, get_args

import tools.messaging as messaging
import router
from tools.house_tools import SPECS
from notes_pick import is_read_ask

Tier = Literal["fast", "deep"]
ToolName = Literal[
    "lights", "apple_tv", "timer", "forecast", "calendar", "lists", "history", "bot_status", "send_text",
    "web_search", "recall_memory", "remember", "forget_last", "remind", "escalate",
]
Kind = Literal[
    "chat", "general", "house", "weather", "calendar", "lists", "bots", "texting", "live", "memory-fact", "memory",
    "notes", "research", "analysis", "multi-step", "basic-info", "memory-write", "reminder",
]
Read = Literal["calendar", "notes", "recall", "forecast", "snapshot", "bot_status", "list", "web", "wiki"]
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

TOOL_NAMES: tuple[ToolName, ...] = get_args(ToolName)
KINDS: tuple[Kind, ...] = get_args(Kind)
MAX_TOOLS = 4
ACTIONS: frozenset[ToolName] = frozenset({"lights", "apple_tv", "timer", "send_text"})
# A read that misses its cap or fails is left out of the prompt and its tool is offered instead.
READ_TOOL: Mapping[Read, ToolName] = {
    "calendar": "calendar", "recall": "recall_memory", "forecast": "forecast", "bot_status": "bot_status",
    "list": "lists", "web": "web_search",
}


def _fn(name: ToolName, desc: str, props: dict[str, JsonValue], required: list[JsonValue]) -> JsonObject:
    params: JsonObject = {"type": "object", "properties": props, "required": required}

    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


SCHEMAS: Mapping[str, JsonObject] = {
    **{str(spec["function"]["name"]): spec for spec in SPECS},
    "remember": _fn("remember", "Keep one household fact in Skwoop's long-term memory.",
                    {"fact": {"type": "string", "description": "One plain third-person sentence."}}, ["fact"]),
    "forget_last": _fn("forget_last", "Undo what this pod saved or set in the last minute.", {}, []),
    "remind": _fn("remind", "Put a reminder on Alex's calendar.", {
        "text": {"type": "string", "description": 'What to do, e.g. "Call the vet"'},
        "when": {"type": "string", "description": "Local YYYY-MM-DDTHH:MM. Empty if no time, or an hour without "
                 "am/pm: never guess."},
        "ask": {"type": "string", "description": "With when empty: one short question for the time."},
    }, ["text", "when"]),
}


@dataclass(frozen=True)
class Toolset:
    kind: Kind
    tier: Tier
    tools: tuple[ToolName, ...]  # escalate is added at offer time while the Deep fallback is on
    reads: tuple[Read, ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class Decline:
    """Not a harness ask: the bridge's other paths (Spotify, Deep) answer it."""

    why: str


_FACTS = "Answer from the facts below. If they don't settle it, say so in one sentence."
TOOLSETS: Mapping[Kind, Toolset] = {t.kind: t for t in (
    Toolset("chat", "fast", (), (), "Small talk or general knowledge: answer it yourself."),
    Toolset("general", "fast", ("web_search", "recall_memory"), ("snapshot",),
            "Answer it yourself if it never changes; web_search for anything live or recent."),
    Toolset("house", "fast", ("lights", "apple_tv", "timer"), ("snapshot",),
            "The house lines below are live: answer state questions from them."),
    Toolset("weather", "fast", (), ("snapshot", "forecast"), _FACTS),
    Toolset("calendar", "fast", (), ("calendar",),
            "Alex's calendar is below, one event per line (calendar: when: event). Tell him what's on it."),
    Toolset("lists", "fast", ("lists",), ("list",), "Lists: read from the facts; adding needs the tool."),
    Toolset("bots", "fast", (), ("bot_status",), _FACTS),
    Toolset("texting", "fast", ("send_text",), (),
            "Texting family: call send_text once, in Alex's words. The pod reads it back and sends after his yes."),
    Toolset("live", "fast", (), ("web",), "A live fact: the web lead below was searched just now. Say the key "
            "facts, with the date when it matters."),
    Toolset("memory-fact", "fast", (), ("recall",),
            "One fact from Skwoop's memory: answer in a few words. If memory doesn't have it, say so."),
    Toolset("memory", "deep", ("recall_memory",), ("recall",), _FACTS),
    Toolset("notes", "fast", (), ("notes",),
            "Alex's notes were searched below. Say what they say, or that nothing matches and what comes closest."),
    Toolset("research", "deep", ("web_search",), (), "Research: one web_search at most, then the headline."),
    Toolset("analysis", "deep", ("history",), ("snapshot",), "How something has been over time: history, then the "
            "headline."),
    Toolset("multi-step", "deep", ("web_search", "recall_memory", "calendar"), ("snapshot",)),
    Toolset("basic-info", "fast", (), ("wiki",), "General knowledge: use the Grokipedia lead below if it fits, "
            "otherwise what you know, in two or three sentences."),
    Toolset("memory-write", "fast", ("remember", "forget_last"), (),
            'Keep a household fact: call remember once, as one plain sentence. "Forget that" right after: '
            "forget_last. Opinions, small talk and health details are never kept."),
    Toolset("reminder", "fast", ("remind", "forget_last"), (),
            "Alex wants a reminder: call remind once. No clear time: when empty and ask set."),
)}


def offered(toolset: Toolset, missed: tuple[ToolName, ...], deep_fallback: bool,
            question: bool = False) -> tuple[ToolName, ...]:
    """The toolset's tools plus the tools of reads that missed, then escalate (fallback on), at most MAX_TOOLS.
    A question gets no action tools: offered one, the model tried to switch the lamp on "are any lights on?"."""
    tail: tuple[ToolName, ...] = ("escalate",) if deep_fallback else ()
    tools = tuple(t for t in dict.fromkeys((*toolset.tools, *missed)) if not (question and t in ACTIONS))

    return (*tools[: MAX_TOOLS - len(tail)], *tail)


# ---------- rules
_CALENDAR = re.compile(
    r"\b(calendar|schedule|agenda|meetings?|appointments?|what do i have|do i have (anything|any|plans)"
    r"|am i free|busy)\b"
)
_LISTS = re.compile(r"\b(shopping list|grocery|groceries|reminders?|to-?do( list)?)\b")
_LIST_ADD = re.compile(r"\b(add|put|remove|delete)\b")
_WEATHER = re.compile(
    r"\b(weather|forecast|temperature|rain(ing|y)?|snow(ing)?|storms?|umbrella|hot|cold|"
    r"degrees|wind(y)?|humid(ity)?)\b"
)
_HOUSE = re.compile(
    r"\b(lights?|lamp|trumpet|bedroom|living room|tv|apple tv|television|timer|who'?s home|"
    r"(anyone|anybody|everyone|who is) home|is [a-z]+ home|sunset|sunrise|internet|starlink|wifi|ping)\b"
)
_INTERNET = re.compile(r"\b(internet|starlink|wifi|ping|outages?|network)\b")
_PEOPLE = "|".join(sorted({w for words in messaging.ALIASES.values() for w in words} - {"me", "myself"}))
_TEXTING = re.compile(
    rf"^(please )?(text|message|send a text|send a message)\b|\b(text|message) (my )?({_PEOPLE})\b"
    rf"|\btell (my )?({_PEOPLE})\b"
)
_LIVE = re.compile(
    r"\b(latest|newest|news|headlines?|scores?|who won|stock|prices?|current(ly)?|right now in|"
    r"this week'?s|today'?s|release|released|version|update|open (right )?now|hours|last night|game|"
    r"who is the (president|prime minister|ceo|governor|mayor))\b"
)
_CHAT = re.compile(
    r"^(hi|hey|hello|thanks|thank you|good (morning|night|evening)|how are you|what'?s up|tell me a "
    r"joke|tell me something (fun|funny|interesting)|you'?re (funny|great|the best))\b"
)
_BOTS = re.compile(r"\b(the bots?|any of the bots)\b|"
                   r"\bis (deep|guard) (running|up|okay|ok|down|stuck|alive)\b")
_BOT_ORDER = re.compile(r"\b(tell|ask|have|get|make) (the bots?)\b")
_NOTE_WRITE = re.compile(r"\b(save|add|put|write|copy) (it |this |that )?(to|in|into|on) my "
                         r"(notes?|documents?|files?)\b")
_ELSEWHERE = re.compile(r"\b(in|at|for|near) (?!the |this |a |an |\d|today|tomorrow|tonight)[a-z]")  # router's
_MINE = re.compile(r"\b(my|our)\b")
_ASK_WRAPPER = re.compile(
    r"^((hey |ok |okay )?skwoop,? )?((can|could|would) you |please )*"
    r"(summari[sz]e|tell me( about)?|give me|look up|search( the web)?( for)?|find( out)?( about)?|check( on)?|"
    r"read me|catch me up on)?\s*",
    re.IGNORECASE,
)
_ASK_TAIL = re.compile(r"(,? ?(for me|please|real quick|quickly))*[.?!\s]*$", re.IGNORECASE)
_MEMORY_WRITE = re.compile(r"\b(remember (that|this|to)|forget (that|about)|from now on|note that i|remind me)\b")
_REMEMBER = re.compile(r"\b(remember (that|this)|keep in mind that|from now on|note that i)\b")
_SET_REMINDER = re.compile(  # "remind me what her name is" stays a memory question
    r"\b(remind me (to|about|that|at|in|on|tomorrow|tonight|this|next)|remember to|don'?t let me forget|"
    r"(set|add|make|create) a reminder)\b"
)
_UNDO = re.compile(r"^((no|wait|oops),? +)?(forget (that|it|what i (just )?said)|undo that|scratch that)\b")
_CANCEL = re.compile(r"^((no|wait|oops),? +)?(never ?mind|cancel (that|it|the reminder)|don'?t remind me)\b")
_BASIC = re.compile(
    r"^((hey |ok |okay )?skwoop,? )?((can|could|would) you |please )*"
    r"(who (was|were|is|are)|what (is|are|was|were)|what'?s an? |tell me about|explain|describe|define|"
    r"how (does|do|did) )"
)


def _fast_kind(t: str, why: str) -> Kind:
    """The kind of a FAST route from its words (the router only says "fast")."""
    if why == "fast-memory-fact":
        return "memory-fact"

    if why == "fast-soft-compare" or _CHAT.search(t):
        return "chat"

    if _TEXTING.search(t):
        return "texting"

    if _CALENDAR.search(t):
        return "calendar"

    if _LISTS.search(t):
        return "lists"

    if router.prepick(t) == ("bot_status", {}) or (_BOTS.search(t) and not _BOT_ORDER.search(t)):
        return "bots"

    if why.startswith("fast-not-a-command") or _HOUSE.search(t):
        return "house"

    if _WEATHER.search(t):
        return "live" if _ELSEWHERE.search(t) else "weather"

    if _LIVE.search(t):
        return "live"

    if _BASIC.search(t) and not _MINE.search(t):
        return "basic-info"

    return "general"


def _deep_kind(t: str, why: str) -> Kind | Decline:
    rule = why.removeprefix("deep-")
    if rule == "music":
        return Decline("music: the bridge's Spotify path, then Deep")

    if rule == "docs":
        if not is_read_ask(t) or _NOTE_WRITE.search(t):
            return Decline("a note write: Deep follows the wiki's SCHEMA.md")

        return "live" if _LIVE.search(t) and not _MINE.search(t) else "notes"

    if rule == "memory":
        return Decline("a memory change Deep handles") if _MEMORY_WRITE.search(t) else "memory"

    if rule == "research":
        return "live" if _LIVE.search(t) else "research"

    return "analysis" if rule == "analysis" else "multi-step"


def search_query(heard: str) -> str:
    """The web lead's query: the ask without its spoken wrapper ("Summarize the latest HA release for me." -> "the
    latest HA release"). A bare query makes the search model run 2 searches, not 5-6 (2.3 s instead of 4.6+ s)."""
    core = _ASK_TAIL.sub("", _ASK_WRAPPER.sub("", heard.strip(), count=1))

    return core if len(core) >= 3 else heard.strip()


def is_undo(text: str, cancel: bool = False) -> bool:
    """cancel: "never mind" counts too (only while a reminder is held, else it's just "never mind")."""
    t = router.normalize(text)

    return _UNDO.search(t) is not None or (cancel and _CANCEL.search(t) is not None)


def pick(route: router.Route, text: str, reminders: bool = True) -> Toolset | Decline:
    """Route + heard text -> the toolset that answers it, or Decline."""
    if route.tier == "instant":
        return Decline("instant: a clip or a script, no model")

    t = router.normalize(text)
    undo = is_undo(text)
    if _SET_REMINDER.search(t) and not undo:
        return TOOLSETS["reminder"] if reminders else Decline("a reminder: reminders are off")

    if undo or _REMEMBER.search(t):
        return TOOLSETS["memory-write"]

    kind = _fast_kind(t, route.why) if route.tier == "fast" else _deep_kind(t, route.why)
    if isinstance(kind, Decline):
        return kind

    ts = TOOLSETS[kind]
    if kind == "house" and _INTERNET.search(t):
        return replace(ts, tools=("lights", "apple_tv", "history"))

    if kind == "lists" and _LIST_ADD.search(t):
        return replace(ts, reads=())

    return ts
