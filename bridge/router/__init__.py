"""Which tier answers an utterance.

INSTANT  quick.py templates (weather now, time, date, timers) and the house scripts in intents.json (lights, TV,
         "play <title>"). A clip or a script, no model.
FAST     the pod harness (harness.py) with a small tool set: chit-chat, home state, one-step house actions, forecast,
         calendar, lists, bot status, texting, a quick web search. It hands off to Deep when it lacks the tool.
DEEP     Deep (skwoop_turn.py): documents, memory, research, analysis, multi-step asks, and music (Spotify on the
         PC speakers).

Two kinds of ask that used to go DEEP on one word now stay FAST (Oct 3 latency work): a short general comparison
("compare cats and dogs as pets in one sentence", nothing to look up) and a one-fact memory question ("do you
remember who my wife is"), which the fast tier answers from one pre-picked recall_memory read.

Rules only, no model call: the old 2B decider is gone from this path (its grammar could only answer with a label, so
"add milk to the shopping list" came back as SCRIPT living_on). Anything not clearly INSTANT or DEEP goes to FAST,
whose escalation is the safety net.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import tools.ha_service as house
from router.quick import QuickIntent

Tier = Literal["instant", "fast", "deep"]

# A house script only fires on a command. These are not commands even when they contain "lights on":
_QUESTION = re.compile(r"^(is|are|was|were|did|does|do|has|have|which|what|whats|why|how|when|who|where|should|"
                       r"will|would|could|can)\b")
_POLITE = re.compile(r"^(can|could|would|will) you (please )?(turn|switch|put|pause|play|dim|set|start|stop|wake|"
                     r"resume|unpause|hit|press|kill)\b")
_NEGATED = re.compile(r"\b(don'?t|do not|never|no need to)\b")
_COMPOUND = re.compile(r"\b(and|then|also|plus)\b.*\b(turn|switch|set|start|play|pause|dim|text|add|tell|remind|"
                       r"cancel|make)\b|\band (the )?(lights?|lamp|tv|bedroom|trumpet)\b")
_LATER = re.compile(r"\b(in|after) (a|an|\d+|one|two|three|four|five|ten|fifteen|twenty|thirty|half an?) "
                    r"(few )?(seconds?|minutes?|mins?|hours?)\b|\bat \d|\bat (noon|midnight)\b|\btonight\b")
# The script would do something other than what was said: a level or colour ("lights on to 30 percent"), or the
# other room / every light ("turn on the lights in the bedroom" substring-matches living_on).
_MODIFIER = re.compile(r"\b(\d+|percent|half|dim|dimmer|brighter|brightness|colou?r|red|blue|green|purple|pink|"
                       r"orange|yellow|warm|cool|white|all)\b|%")
_ROOM_OF = {"living": re.compile(r"\b(bedroom)\b"), "bedroom": re.compile(r"\b(living room|trumpet|lamp)\b")}

_SOFT_RESEARCH = re.compile(r"\b(compare|comparison|pros and cons|difference between)\b")
# Spotify: a "play ..." that house.play_show_hit passed on (no "on TV", no "the show"), and pause/skip/what's playing.
# A bare "stop" or "next" is not music. Any mention of the TV is the fast tier's apple_tv tool.
_MUSIC = re.compile(r"^(?!.*\b(tv|television)\b)(?:(?:can|could|would|will) you (?:please )?|please )?(?:"
                    r"(?:play|shuffle|queue)\b.*"
                    r"|(?:pause|resume|unpause)(?: (?:the )?(?:music|song|spotify))?"
                    r"|put on (?:some |a little |my |the )?(?:music|songs?|tunes)\b.*"
                    r"|(?:switch|change)(?: it| the music)? (?:to|over to) .*\b(?:playlist|album|music|songs)\b.*"
                    r"|stop (?:the )?(?:music|song|spotify)"
                    r"|skip(?: (?:this|the) (?:song|track))?"
                    r"|(?:next|previous|last) (?:song|track)"
                    r"|what(?:'?s| is) (?:playing|this song)(?: (?:right )?now)?"
                    r"|what song is (?:this|playing)(?: (?:right )?now)?)$")
DEEP_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("music", _MUSIC),
    ("docs", re.compile(r"\b(notes?|documents?|docs|files?|folders?|pdfs?|spreadsheets?|warrant(y|ies)|receipts?|"
                        r"manuals?)\b")),
    ("memory", re.compile(r"\b(remember (that|this|to)|do you remember|what did i (tell|say)|i (told|said) you|"
                          r"forget (that|about))\b")),
    ("research", re.compile(r"\b(research|look into|dig into|deep dive|summari[sz]e|go through|"
                            r"find (me )?(the best|a good)|plan (a|my|the|out)|itinerary)\b")),
    ("research", _SOFT_RESEARCH),           # only when it is not a _soft_compare
    ("analysis", re.compile(r"\b(unusual|trends?|history|over time|on average|so far (today|this))\b")),
    ("multi-step", re.compile(r"\b(and then|after that)\b")),
)


@dataclass(frozen=True)
class Route:
    tier: Tier
    why: str  # quick-WeatherAsk | script-living_on | fast | fast-not-a-command | deep-docs
    intent: QuickIntent | None = None
    hit: house.Hit | None = None


def normalize(text: str) -> str:
    t = " ".join(text.lower().replace("’", "'").split())
    return re.sub(r"[.!?,]+", " ", t).strip()


def not_a_command(t: str) -> str | None:
    """Why a keyword hit in `t` must not fire a script: it's a question, a "don't", two asks, or for later."""
    if _NEGATED.search(t):
        return "negated"

    if _COMPOUND.search(t):
        return "compound"

    if _LATER.search(t):
        return "later"

    if _QUESTION.search(t) and not _POLITE.search(t):
        return "question"

    return None


def script_mismatch(ack_id: str, t: str) -> str | None:
    """A light script hit whose words ask for more or other than the script does."""
    room = ack_id.split("_", 1)[0]
    if room not in _ROOM_OF:
        return None
    if _MODIFIER.search(t):
        return "modifier"
    if _ROOM_OF[room].search(t):
        return "other-room"
    return None


# Hold the fast tier's action tools: a plain question ("is the TV on?") or a "don't" + action is never a reason to
# change anything, whatever the model makes of it. "Can you / could you ..." stays a request.
_ASKS = re.compile(r"^(is|are|was|were|did|does|do|has|have|which|what|whats|why|how|when|who|where|should)\b")
_NEG_ACT = re.compile(r"\b(don'?t|do not|never|no need to)( (ever|you|please))? (turn|switch|put|start|play|pause|"
                      r"dim|set|text|send|wake|add|cancel|stop|open)\b")


def hold_actions(text: str) -> str | None:
    t = normalize(text)
    if _NEG_ACT.search(t):
        return "said not to do it"
    if _LATER.search(t):            # "lights off in ten minutes": a timer + "Okay." would sound like it's scheduled
        return "asked for it to happen later, and the pod can't schedule anything; say so"
    if _ASKS.search(t):
        return "only asked a question"
    return None


# A comparison is research only when it needs the web or Alex's own situation ("the two best robot vacuums for pet
# hair"). A short one about things that don't change ("cats and dogs as pets in one sentence") is general knowledge.
_BRIEF = re.compile(r"\b(in (one|a) (sentence|word|nutshell)|one sentence|quick(ly)?|brief(ly)?|short(ly)?|"
                    r"in short|tl ?dr)\b")
_NEEDS_LOOKUP = re.compile(r"\b(best|top|cheapest|buy|buying|price|prices|cost|costs|reviews?|rated|latest|"
                           r"newest|deals?|current|currently|this year|20\d\d|recommend|which (one )?should i|"
                           r"for (my|our|me|us))\b")
SOFT_COMPARE_MAX_WORDS = 14


def _soft_compare(t: str) -> bool:
    """A comparison the fast model can answer itself (it can still escalate)."""
    if _NEEDS_LOOKUP.search(t):
        return False

    return bool(_BRIEF.search(t)) or len(t.split()) <= SOFT_COMPARE_MAX_WORDS


def deep_rule(t: str) -> str | None:
    for name, rx in DEEP_RULES:
        if rx is _SOFT_RESEARCH and _soft_compare(t):
            continue

        if rx.search(t):
            return name

    return None


# One fact Alex told Skwoop about his people or pets: "do you remember who my wife is", "what's my dog's name".
# Not "what did I tell you about the trip" (that is DEEP memory) and not "remember that ..." (a write).
_PEOPLE = (r"(wife|husband|partner|spouse|mom|mother|dad|father|parents|son|daughter|kids?|children|child|brother|"
           r"sister|siblings?|grandma|grandpa|grandmother|grandfather|aunt|uncle|cousin|in-laws|dog|cat|pets?)")
_MEMORY_FACT = re.compile(r"^(?:(?:do|did) you (?:remember|know|recall) |can you remind me |remind me |tell me )?"
                          r"(?:who|what)(?: is|'s|s| are| was| were)? (?:my|our) " + _PEOPLE +
                          r"(?:'s|s')?(?: (?:name|names|birthday))?(?: is| are| was| were)?$")


def memory_fact(t: str) -> bool:
    return bool(_MEMORY_FACT.match(t))


def route(text: str, quick_match: Callable[[str], QuickIntent | None],
          house_match: Callable[[str], house.Hit | None]) -> Route:
    """quick_match/house_match are lookups only (QuickAnswers.match, house.match): nothing fires here."""
    t = normalize(text)
    if not t:
        return Route("fast", "fast-empty")

    blocked = _NEGATED.search(t) or _COMPOUND.search(t)       # "don't set a timer", "lights on and a timer"
    if not blocked:
        intent = quick_match(text)
        if intent is not None:
            return Route("instant", f"quick-{type(intent).__name__}", intent=intent)

    hit = house_match(text)
    if hit is not None:
        why = not_a_command(t) or script_mismatch(hit.ack_id, t)
        if why is None:
            return Route("instant", f"script-{hit.ack_id}", hit=hit)

        if (rule := deep_rule(t)) is not None:
            return Route("deep", f"deep-{rule}")

        return Route("fast", f"fast-not-a-command-{why}")

    if memory_fact(t):
        return Route("fast", "fast-memory-fact")

    if (rule := deep_rule(t)) is not None:
        return Route("deep", f"deep-{rule}")

    if _SOFT_RESEARCH.search(t):
        return Route("fast", "fast-soft-compare")

    return Route("fast", "fast")


# ---------- pre-picked reads ----------
# Asks whose tool is certain from the words alone. The harness runs that read before the model's first round.
# Read-only tools only; anything with another day, place, a second request or a negation is left to the model.
_PP_CAL = re.compile(r"\b((on|in) my (calendar|schedule|agenda)|what'?s my (schedule|agenda)|"
                     r"what do i have (on |going on )?(today|tomorrow)|"
                     r"any (meetings|appointments)( today| tomorrow)?)\b")
_PP_OTHER_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|weekend|month|next|"
                           r"tonight|morning|afternoon|evening|yesterday|last|\d)")
_PP_BOTS = re.compile(r"\b(the bots?)\b.*\b(doing|working on|up to|status|running|busy)\b|"
                      r"\b(status of|check( on)?) (the bots?)\b|"
                      r"\bis (deep|guard) (running|up|okay|ok|down|stuck|alive)\b")
_PP_RAIN = re.compile(r"\b(forecast|rain|raining|umbrella|snow|snowing|storms?)\b|\bweather (tomorrow|tonight|"
                      r"this weekend|later)\b")
_PP_NOW = re.compile(r"\b(is it|it's|it is) (raining|snowing|storming)\b|\bright now\b|\boutside\b")
_PP_ORDER = re.compile(r"\b(tell|ask|have|get|make) (the bots?)\b")
_PP_ELSEWHERE = re.compile(r"\b(in|at|for|near) (?!the |this |a |an |\d|today|tomorrow|tonight)[a-z]")


def prepick(text: str) -> tuple[str, dict] | None:
    """The read-only tool and its arguments for an ask that needs exactly that one read, or None."""
    t = normalize(text)
    if _NEGATED.search(t) or _COMPOUND.search(t) or _LATER.search(t):
        return None
    if _PP_CAL.search(t):
        rest = re.sub(r"\b(today|tomorrow)\b", "", t)
        if _PP_OTHER_DAY.search(rest):
            return None
        return "calendar", {"day_offset": 1 if "tomorrow" in t else 0, "days": 1}
    if _PP_BOTS.search(t) and not _PP_ORDER.search(t):
        return "bot_status", {}
    if _PP_RAIN.search(t) and not _PP_ELSEWHERE.search(t) and not _PP_NOW.search(t):
        return "forecast", {}
    if memory_fact(t):
        return "recall_memory", {"query": text.strip()}
    return None
