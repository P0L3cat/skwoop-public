"""Quick factual intents answered on the bridge, without Deep: the weather right now, the time, the day or date,
and kitchen timers.

Every reply is worded exactly like a SkwoopOS library template (weather_now, temp_out, clock, today_day, today_date,
timer_set, timer_done, timer_cancel, timer_left), so it plays as a recorded clip: no LLM, no credits, ~50 ms.

Weather comes from Home Assistant's weather entity (read only, GET /api/states/<entity>), cached for a few minutes.
  HA_URL / HA_TOKEN        Home Assistant (the same ones house.py uses)
  WEATHER_ENTITY           default weather.forecast_home
  WEATHER_CACHE_S          default 300
Forecast questions (tomorrow, tonight, later, this week, rain later...) are not quick: they still go to Deep.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from lean_http import ClientError, ClientSession, ClientTimeout

log = logging.getLogger("skwoop.quick")

DEFAULT_WEATHER_ENTITY = "weather.forecast_home"
DEFAULT_WEATHER_CACHE_S = 300.0
MAX_TIMER_S = 24 * 3600
TIMER_QUESTION_S = 20.0     # "How long should the timer be?" waits this long for "five minutes"


# ------------------------------------------------------------------ intents
@dataclass(frozen=True)
class WeatherAsk:
    pass


@dataclass(frozen=True)
class TimeAsk:
    pass


@dataclass(frozen=True)
class DayAsk:
    kind: Literal["day", "date"]


@dataclass(frozen=True)
class TimerSet:
    seconds: int


@dataclass(frozen=True)
class TimerHowLong:
    """"Set a timer" with no length: ask, and take the next utterance's duration."""


@dataclass(frozen=True)
class TimerCancel:
    pass


@dataclass(frozen=True)
class TimerLeft:
    pass


QuickIntent = WeatherAsk | TimeAsk | DayAsk | TimerSet | TimerHowLong | TimerCancel | TimerLeft


@dataclass(frozen=True)
class QuickReply:
    text: str
    mood: str
    expects_answer: bool = False        # a question back ("How long should the timer be?"): listen for the answer


# ------------------------------------------------------------------ number words
ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORDINAL_ONES = ("_ first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth "
                "fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth").split()
ORDINAL_TENS = {20: "twentieth", 30: "thirtieth"}
MONTHS = ("January February March April May June July August September October November December").split()


def num_words(n: int) -> str:
    """-15 -> "minus fifteen", 102 -> "one hundred two" (the library's spelling)."""
    if n < 0:
        return "minus " + num_words(-n)

    if n < 20:
        return ONES[n]

    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else "-" + ONES[n % 10])

    if n < 1000:
        rest = n % 100

        return ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + num_words(rest))

    return str(n)


def ordinal_words(n: int) -> str:
    if n < 20:
        return ORDINAL_ONES[n]

    if n % 10 == 0:
        return ORDINAL_TENS[n]

    return TENS[n // 10] + "-" + ORDINAL_ONES[n % 10]


_WORD_NUM = {w: i for i, w in enumerate(ONES)} | {w: 10 * i for i, w in enumerate(TENS) if w != "_"}
_WORD_NUM |= {"a": 1, "an": 1, "one": 1}


def parse_count(words: str) -> int | None:
    """"5", "five", "twenty five", "twenty-five", "an" -> int. None if it isn't a number."""
    w = words.strip().lower().replace("-", " ")
    if w.isdigit():
        return int(w)

    total = 0
    for part in w.split():
        value = _WORD_NUM.get(part)
        if value is None:
            return None

        total += value

    return total if w else None


# ------------------------------------------------------------------ phrases the library has
def duration_words(seconds: int) -> str:
    """90 -> "a minute and a half", 300 -> "five minutes", 5400 -> "an hour and a half" (timer_* templates)."""
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if seconds == 90:
        return "a minute and a half"

    if seconds == 9000:
        return "two and a half hours"

    if h == 1 and m == 30 and s == 0:
        return "an hour and a half"

    if seconds < 60:
        return "one second" if seconds == 1 else f"{num_words(seconds)} seconds"

    parts: list[str] = []
    if h:
        parts.append("one hour" if h == 1 and not m else ("an hour" if h == 1 else f"{num_words(h)} hours"))

    if m:
        parts.append("one minute" if m == 1 else f"{num_words(m)} minutes")

    if s:
        parts.append(f"{num_words(s)} seconds")

    return " and ".join(parts)


def clock_words(hour: int, minute: int) -> str:
    """(0, 0) -> "midnight", (12, 0) -> "noon", (8, 5) -> "eight oh five", (20, 0) -> "eight o'clock"."""
    if minute == 0 and hour in (0, 12):
        return "midnight" if hour == 0 else "noon"

    h = num_words(hour % 12 or 12)
    if minute == 0:
        return f"{h} o'clock"

    if minute < 10:
        return f"{h} oh {num_words(minute)}"

    return f"{h} {num_words(minute)}"


# ------------------------------------------------------------------ matching
_FORECAST = re.compile(r"\b(tomorrow|tonight|later|this (week|weekend|afternoon|evening)|next|forecast|"
                       r"will it|going to|gonna|should i|umbrella|sunrise|sunset)\b")
_WEATHER = re.compile(r"\b(weather|temperature|how (hot|cold|warm) is it|is it (hot|cold|warm|raining|sunny)|"
                      r"what'?s it like outside|how('?s| is) it outside)\b")
_TIME = re.compile(r"\b(what time is it|what'?s the time|what is the time|tell me the time|current time|"
                   r"time is it)\b")
# not "what's today's plan"
_DAY = re.compile(r"\b(what day is (it|today)|what'?s today|what is today)\b(?!'s)")
# not "...of Easter"
_DATE = re.compile(r"\b(what'?s the date|what is the date|today'?s date|what date is it)\b(?! of)")
_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_TENS = r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
_COUNT = (r"(?:\d+|an?|" + _TENS + r"[ -]?(?:one|two|three|four|five|six|seven|eight|nine)|" + _TENS +
          r"|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
          r"seventeen|eighteen|nineteen)")
_TIMER_SET = re.compile(rf"\b(?P<n>{_COUNT})(?P<and_half> and a half)?[ -](?P<u>{_UNIT})"
                        rf"(?: and (?:a )?(?P<half>half)| and (?P<n2>{_COUNT})[ -](?P<u2>{_UNIT}))?\b")
_HALF_AN_HOUR = re.compile(r"\bhalf (an|a) hour\b")
_ELSEWHERE = re.compile(r"\b(in|at|near|for) (?!here\b|the house\b)[a-z]")   # Paris, the bedroom...
_TIMER_CANCEL = re.compile(r"\b(cancel|stop|delete|clear|kill) (the |my |that )?timer\b")
_TIMER_LEFT = re.compile(r"\b(how (much time|long)( is)? (left|remaining)|time left|how long left)\b")


def _unit_s(unit: str) -> int:
    if unit.startswith("h"):
        return 3600

    if unit.startswith("m"):
        return 60

    return 1


def _timer_seconds(t: str) -> int | None:
    if _HALF_AN_HOUR.search(t):
        return 1800

    hit = _TIMER_SET.search(t)
    if hit is None:
        return None

    n = parse_count(hit["n"])
    if n is None:
        return None

    unit = _unit_s(hit["u"])
    seconds = n * unit
    if hit["half"] or hit["and_half"]:
        seconds += unit // 2
    elif hit["n2"]:
        n2 = parse_count(hit["n2"])
        if n2 is None:
            return None

        seconds += n2 * _unit_s(hit["u2"])

    return seconds if 0 < seconds <= MAX_TIMER_S else None


def match(text: str) -> QuickIntent | None:
    """The utterance as STT wrote it -> a quick intent, or None (let the router decide)."""
    t = " ".join(text.lower().replace("’", "'").split())
    t = re.sub(r"[.!?,]+", " ", t).strip()
    if "timer" in t:
        if _TIMER_CANCEL.search(t):
            return TimerCancel()

        if _TIMER_LEFT.search(t):
            return TimerLeft()

        seconds = _timer_seconds(t)
        if seconds is not None:
            return TimerSet(seconds)

        return TimerHowLong() if re.search(r"\b(set|start) (a |the )?timer$", t) else None

    if _TIMER_LEFT.search(t):
        return TimerLeft()

    if _WEATHER.search(t) and not _FORECAST.search(t) and not _ELSEWHERE.search(t):
        return WeatherAsk()

    if _TIME.search(t) and not _ELSEWHERE.search(t):     # "what time is it in Tokyo" is not the local clock
        return TimeAsk()

    if _DATE.search(t):
        return DayAsk("date")

    if _DAY.search(t):
        return DayAsk("day")

    return None


# ------------------------------------------------------------------ weather (Home Assistant)
class WeatherUnavailable(RuntimeError):
    """Home Assistant didn't answer, or its weather entity has no usable reading."""


@dataclass(frozen=True)
class WeatherNow:
    temperature: int
    condition: str          # Home Assistant condition: sunny, partlycloudy, rainy, clear-night ...


# Home Assistant conditions -> the library's {wx} words (None: say only the temperature)
CONDITION_WORDS: dict[str, str | None] = {
    "clear-night": "clear", "cloudy": "cloudy", "exceptional": None, "fog": "foggy", "hail": "stormy",
    "lightning": "stormy", "lightning-rainy": "stormy", "partlycloudy": "partly cloudy", "pouring": "pouring",
    "rainy": "rainy", "snowy": "snowy", "snowy-rainy": "slushy", "sunny": "sunny", "windy": "windy",
    "windy-variant": "windy",
}


def parse_weather_state(raw: object) -> WeatherNow:
    """GET /api/states/weather.* JSON -> WeatherNow. WeatherUnavailable when it has no reading."""
    if not isinstance(raw, dict):
        raise WeatherUnavailable("weather state: expected an object")

    state, attrs = raw.get("state"), raw.get("attributes")
    if not isinstance(state, str) or state in ("unavailable", "unknown") or not isinstance(attrs, dict):
        raise WeatherUnavailable(f"weather entity is {state!r}")

    temp = attrs.get("temperature")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
        raise WeatherUnavailable("weather entity has no temperature")

    return WeatherNow(temperature=round(temp), condition=state)


WeatherFetch = Callable[[], Awaitable[object]]


def ha_weather_fetch(http: ClientSession, base_url: str, token: str, entity: str) -> WeatherFetch:
    """The raw JSON of one Home Assistant weather entity (the parsing happens in WeatherSource)."""
    url = f"{base_url.rstrip('/')}/api/states/{entity}"
    headers = {"Authorization": f"Bearer {token}"}

    async def fetch() -> object:
        try:
            async with http.get(url, headers=headers, timeout=ClientTimeout(total=2.5)) as r:
                if r.status != 200:
                    raise WeatherUnavailable(f"Home Assistant HTTP {r.status}")

                body: object = await r.json()

                return body
        except (TimeoutError, ClientError) as e:
            raise WeatherUnavailable(type(e).__name__) from e

    return fetch


class WeatherSource:
    """Cached current conditions: one Home Assistant read per ttl_s."""

    def __init__(self, fetch: WeatherFetch, ttl_s: float = DEFAULT_WEATHER_CACHE_S,
                 clock: Callable[[], float] = time.monotonic):
        self.fetch = fetch
        self.ttl_s = ttl_s
        self.clock = clock
        self.cached: tuple[float, WeatherNow] | None = None
        self.reads = 0

    async def now(self) -> WeatherNow:
        hit = self.cached
        if hit is not None and self.clock() - hit[0] < self.ttl_s:
            return hit[1]

        reading = parse_weather_state(await self.fetch())
        self.reads += 1
        self.cached = (self.clock(), reading)

        return reading


def weather_sentence(w: WeatherNow) -> str:
    temp = f"{num_words(w.temperature)} degrees"
    wx = CONDITION_WORDS.get(w.condition)
    if wx is None:
        return f"It's {temp} outside."

    return f"Right now it's {temp} and {wx}."


# ------------------------------------------------------------------ timers
TimerRing = Callable[[str, str, int], Awaitable[None]]      # (device id, room, seconds) when a timer goes off


@dataclass
class RunningTimer:
    room: str
    seconds: int
    due: float
    task: asyncio.Task[None]


class TimerBook:
    """One kitchen timer per pod. Rings through `ring` (the bridge plays "Your timer for ... is done!")."""

    def __init__(self, ring: TimerRing, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.ring = ring
        self.clock = clock
        self.sleep = sleep
        self.running: dict[str, RunningTimer] = {}

    def start(self, device: str, room: str, seconds: int) -> None:
        self.cancel(device)
        task = asyncio.create_task(self._wait(device, room, seconds))
        self.running[device] = RunningTimer(room, seconds, self.clock() + seconds, task)
        log.info("[%s] timer %ss set", room, seconds)

    async def _wait(self, device: str, room: str, seconds: int) -> None:
        await self.sleep(seconds)
        self.running.pop(device, None)
        log.info("[%s] timer %ss done", room, seconds)
        try:
            await self.ring(device, room, seconds)
        except Exception as e:  # noqa: BLE001 - a failed ring must not kill the loop
            log.error("[%s] timer ring failed: %s", room, e)

    def cancel(self, device: str) -> RunningTimer | None:
        t = self.running.pop(device, None)
        if t is not None:
            t.task.cancel()

        return t

    def left(self, device: str) -> int | None:
        t = self.running.get(device)
        if t is None:
            return None

        return max(1, round(t.due - self.clock()))


def round_left(seconds: int) -> int:
    """Remaining time as people say it: whole minutes above two minutes, 5 s steps below a minute."""
    if seconds > 120:
        return round(seconds / 60) * 60

    if seconds < 60:
        return max(5, round(seconds / 5) * 5)

    return seconds


# ------------------------------------------------------------------ answering
class QuickAnswers:
    def __init__(self, weather: WeatherSource, timers: TimerBook, now: Callable[[], datetime] = datetime.now,
                 clock: Callable[[], float] = time.monotonic):
        self.weather = weather
        self.timers = timers
        self.now = now
        self.clock = clock
        self.asked_how_long: dict[str, float] = {}      # device -> when we asked "How long should the timer be?"

    def match(self, text: str, device: str) -> QuickIntent | None:
        """match(), plus "five minutes" right after we asked how long the timer should be."""
        asked = self.asked_how_long.pop(device, None)
        if asked is not None and self.clock() - asked < TIMER_QUESTION_S:
            seconds = _timer_seconds(" ".join(text.lower().split()))
            if seconds is not None:
                return TimerSet(seconds)

        return match(text)

    async def answer(self, intent: QuickIntent, device: str, room: str) -> QuickReply | None:
        """None: no quick answer after all (Home Assistant is down) - let Deep have it."""
        match intent:
            case WeatherAsk():
                try:
                    w = await self.weather.now()
                except WeatherUnavailable as e:
                    log.warning("quick weather unavailable (%s): asking Deep", e)

                    return None

                return QuickReply(weather_sentence(w), "happy")
            case TimeAsk():
                now = self.now()

                return QuickReply(f"It's {clock_words(now.hour, now.minute)}.", "idle")
            case DayAsk(kind="day"):
                return QuickReply(f"Today is {self.now().strftime('%A')}.", "happy")
            case DayAsk():
                now = self.now()

                return QuickReply(f"Today is {MONTHS[now.month - 1]} {ordinal_words(now.day)}.", "happy")
            case TimerSet(seconds=seconds):
                self.timers.start(device, room, seconds)

                return QuickReply(f"Okay, your timer for {duration_words(seconds)} is set.", "happy")
            case TimerHowLong():
                self.asked_how_long[device] = self.clock()

                return QuickReply("How long should the timer be?", "idle", expects_answer=True)
            case TimerCancel():
                stopped = self.timers.cancel(device)
                if stopped is None:
                    return QuickReply("You don't have any timers.", "idle")

                return QuickReply(f"Okay, your timer for {duration_words(stopped.seconds)} is canceled.", "idle")
            case TimerLeft():
                left = self.timers.left(device)
                if left is None:
                    return QuickReply("You don't have any timers.", "idle")

                return QuickReply(f"Your timer has {duration_words(round_left(left))} left.", "idle")


def timer_done_sentence(seconds: int) -> str:
    return f"Your timer for {duration_words(seconds)} is done!"


def weather_from_env(http: ClientSession) -> WeatherSource:
    base = os.environ.get("HA_URL") or os.environ.get("HASS_URL") or "http://127.0.0.1:8123"
    token = os.environ.get("HA_TOKEN") or os.environ.get("HASS_TOKEN") or ""
    entity = os.environ.get("WEATHER_ENTITY") or DEFAULT_WEATHER_ENTITY
    ttl = float(os.environ.get("WEATHER_CACHE_S") or DEFAULT_WEATHER_CACHE_S)

    return WeatherSource(ha_weather_fetch(http, base, token, entity), ttl_s=ttl)
