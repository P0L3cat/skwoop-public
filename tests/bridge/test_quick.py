"""Quick intents: matching, the library's wording, the Home Assistant weather boundary + cache, timers. No network."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import router.quick as quick
from router.quick import DayAsk, TimeAsk, TimerCancel, TimerHowLong, TimerLeft, TimerSet, WeatherAsk


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.parametrize(("said", "intent"), [
    ("What's the weather like right now?", WeatherAsk()),
    ("how hot is it outside", WeatherAsk()),
    ("How is it outside?", WeatherAsk()),
    ("what's the temperature", WeatherAsk()),
    ("What time is it?", TimeAsk()),
    ("what day is it", DayAsk("day")),
    ("What's today's date?", DayAsk("date")),
    ("set a timer for 5 minutes", TimerSet(300)),
    ("Set a timer for twenty five minutes.", TimerSet(1500)),
    ("Set a 10-minute timer.", TimerSet(600)),
    ("set a timer for an hour and a half", TimerSet(5400)),
    ("set a timer for half an hour", TimerSet(1800)),
    ("timer for 1 minute and 30 seconds", TimerSet(90)),
    ("set a timer for two and a half hours", TimerSet(9000)),
    ("Set a timer.", TimerHowLong()),
    ("cancel the timer", TimerCancel()),
    ("how much time is left on my timer", TimerLeft()),
])
def test_quick_intents(said, intent):
    assert quick.match(said) == intent


@pytest.mark.parametrize("said", [
    "what's the weather tomorrow", "will it rain later", "what's the weather in Paris",
    "what is the temperature in the bedroom", "tell me something fun about crows", "timer for 25 hours",
    "turn on the living room lights",
])
def test_not_quick_goes_to_the_router(said):
    assert quick.match(said) is None


def test_wording_matches_the_library_templates():
    assert [quick.duration_words(s) for s in (5, 30, 60, 90, 300, 1500, 3600, 4500, 5400, 7200, 9000)] == [
        "five seconds", "thirty seconds", "one minute", "a minute and a half", "five minutes", "twenty-five minutes",
        "one hour", "an hour and fifteen minutes", "an hour and a half", "two hours", "two and a half hours"]
    assert [quick.clock_words(h, m) for h, m in ((0, 0), (12, 0), (8, 5), (20, 0), (13, 45))] == [
        "midnight", "noon", "eight oh five", "eight o'clock", "one forty-five"]
    assert [quick.num_words(n) for n in (-15, 0, 80, 102)] == ["minus fifteen", "zero", "eighty", "one hundred two"]
    assert [quick.ordinal_words(n) for n in (2, 23, 30, 31)] == ["second", "twenty-third", "thirtieth", "thirty-first"]
    assert quick.weather_sentence(quick.WeatherNow(80, "partlycloudy")) == (
        "Right now it's eighty degrees and partly cloudy.")
    assert quick.weather_sentence(quick.WeatherNow(-3, "exceptional")) == "It's minus three degrees outside."


def test_weather_state_is_parsed_at_the_boundary():
    good = {"state": "cloudy", "attributes": {"temperature": 79.6, "temperature_unit": "°F"}}
    assert quick.parse_weather_state(good) == quick.WeatherNow(80, "cloudy")
    for bad in (None, [], {"state": "unavailable", "attributes": {}}, {"state": "sunny", "attributes": {}},
                {"state": "sunny", "attributes": {"temperature": True}}, {"state": "sunny"}):
        with pytest.raises(quick.WeatherUnavailable):
            quick.parse_weather_state(bad)


def test_weather_is_cached_for_ttl():
    clock = FakeClock()
    reads: list[int] = []

    async def fetch() -> object:
        reads.append(1)

        return {"state": "sunny", "attributes": {"temperature": 70 + len(reads)}}

    src = quick.WeatherSource(fetch, ttl_s=300, clock=clock)

    async def go() -> list[int]:
        a = await src.now()
        clock.t += 299
        b = await src.now()
        clock.t += 2
        c = await src.now()

        return [a.temperature, b.temperature, c.temperature]

    assert asyncio.run(go()) == [71, 71, 72] and len(reads) == 2


def _answers(fetch: quick.WeatherFetch, clock: FakeClock, rang: list[tuple[str, str, int]]
             ) -> tuple[quick.QuickAnswers, asyncio.Event]:
    """Answers with a fake clock; timers sleep until the returned gate is set."""
    gate = asyncio.Event()

    async def ring(device: str, room: str, seconds: int) -> None:
        rang.append((device, room, seconds))

    async def sleep(seconds: float) -> None:
        await gate.wait()

    book = quick.TimerBook(ring, clock=clock, sleep=sleep)
    answers = quick.QuickAnswers(quick.WeatherSource(fetch, clock=clock), book,
                                 now=lambda: datetime(2026, 10, 2, 8, 5), clock=clock)

    return answers, gate


async def _down() -> object:
    raise quick.WeatherUnavailable("Home Assistant HTTP 502")


async def _sunny() -> object:
    return {"state": "sunny", "attributes": {"temperature": 72}}


def test_answers_and_fallthrough():
    clock = FakeClock()

    async def go() -> list[str | None]:
        (up, _), (down, _) = _answers(_sunny, clock, []), _answers(_down, clock, [])
        out = [await up.answer(i, "pod-1", "desk") for i in (WeatherAsk(), TimeAsk(), DayAsk("day"), DayAsk("date"))]
        out.append(await down.answer(WeatherAsk(), "pod-1", "desk"))

        return [r.text if r else None for r in out]

    assert asyncio.run(go()) == ["Right now it's seventy-two degrees and sunny.", "It's eight oh five.",
                                 "Today is Friday.", "Today is October second.", None]


def test_timers_set_left_cancel_and_ring():
    clock = FakeClock()
    rang: list[tuple[str, str, int]] = []

    async def go() -> list[str]:
        qa, gate = _answers(_sunny, clock, rang)
        said = []
        for intent in (TimerLeft(), TimerSet(300)):
            r = await qa.answer(intent, "pod-1", "kitchen")
            said.append(r.text if r else "")

        clock.t += 61
        r = await qa.answer(TimerLeft(), "pod-1", "kitchen")
        said.append(r.text if r else "")
        r = await qa.answer(TimerCancel(), "pod-1", "kitchen")
        said.append(r.text if r else "")
        await qa.answer(TimerSet(90), "pod-1", "kitchen")
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        return said

    assert asyncio.run(go()) == ["You don't have any timers.", "Okay, your timer for five minutes is set.",
                                 "Your timer has four minutes left.", "Okay, your timer for five minutes is canceled."]
    assert rang == [("pod-1", "kitchen", 90)]
    assert quick.timer_done_sentence(90) == "Your timer for a minute and a half is done!"


def test_how_long_then_a_duration():
    clock = FakeClock()

    async def go() -> tuple[quick.QuickReply | None, quick.QuickIntent | None, quick.QuickIntent | None]:
        qa, _ = _answers(_sunny, clock, [])
        asked = await qa.answer(qa.match("set a timer", "pod-1") or TimeAsk(), "pod-1", "desk")
        follow = qa.match("five minutes", "pod-1")
        late = qa.match("five minutes", "pod-1")       # only once, right after the question

        return asked, follow, late

    asked, follow, late = asyncio.run(go())
    assert asked == quick.QuickReply("How long should the timer be?", "idle", expects_answer=True)
    assert follow == TimerSet(300) and late is None
