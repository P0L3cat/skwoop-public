"""The house tools' pieces: contacts and the text confirm flow, the bot status read, the quick search's parsing and
daily cap, and the house tools against a fake Home Assistant."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import tools.ha_service as house
import tools.house_tools as house_tools
import tools.messaging as messaging
import router.quick as quick
import status_read
from tools.house_tools import HA, Escalate, HouseTools
from tools.quick_search import SearchBudget, SearchRefused, parse_answer, speakable

CONTACTS = """# family
mom:
  name: Alex Example
  phone: "+15550000001"
dad:
  name: Sam Example
  phone: "+15550000002"
alex:
  name: Alex
  phone: "+15550000003"
"""


# ---------- messaging
def test_contacts_parse_and_lookup():
    c = messaging.parse_contacts(CONTACTS)
    assert sorted(c) == ["alex", "dad", "mom"] and c["mom"].phone == "+15550000001"
    assert messaging.find_contact(c, "my mom").key == "mom"
    assert messaging.find_contact(c, "Mel") is None
    assert messaging.find_contact(c, "my wife") is None          # alias only applies when Mel is a contact


def test_wife_means_mel_and_still_needs_a_yes():
    c = messaging.parse_contacts(CONTACTS + 'mel:\n  name: Mel\n  phone: "+15550000004"\n')
    for said in ("my wife", "wife", "to my wife", "Mel", "the wife"):
        assert messaging.find_contact(c, said).key == "mel", said
    texts = messaging.Texts(c, lambda p, b: asyncio.sleep(0, "sent"), clock=lambda: 0.0)
    pending, _ = texts.propose("d", "my wife", "On my way home")
    assert pending is not None and pending.readback == "Text Mel: On my way home. Send it?"


@pytest.mark.parametrize("bad", ["mom:\n  phone: 555\n", "mom: [1, 2]\n", "  name: x\n"])
def test_contacts_parser_is_strict(bad):
    with pytest.raises(messaging.ContactsError):
        messaging.parse_contacts(bad)


def _texts(results: list[str]) -> tuple[messaging.Texts, list[tuple[str, str]], list[float]]:
    sent: list[tuple[str, str]] = []
    now = [0.0]

    async def sender(phone: str, body: str) -> messaging.SendResult:
        sent.append((phone, body))
        return results.pop(0)  # type: ignore[return-value]

    return messaging.Texts(messaging.parse_contacts(CONTACTS), sender, clock=lambda: now[0]), sent, now


def test_text_needs_a_yes_and_sends_once():
    t, sent, _ = _texts(["sent"])
    pending, _told = t.propose("d", "mom", "running late")
    assert pending is not None and pending.readback.startswith("Text Mom: running late.")
    assert asyncio.run(t.answer("d", "yes")) == "Sent to Mom."
    assert sent == [("+15550000001", "running late.")]
    assert asyncio.run(t.answer("d", "yes")) is None                       # nothing pending any more


@pytest.mark.parametrize(("heard", "reply"), [
    ("no", "Okay, I won't send it."),
    ("nope, cancel", "Okay, I won't send it."),
    ("what's the weather", None),
])
def test_text_no_or_anything_else_never_sends(heard, reply):
    t, sent, _ = _texts([])
    t.propose("d", "dad", "home soon")
    assert asyncio.run(t.answer("d", heard)) == reply
    assert sent == [] and t.waiting("d") is None


def test_text_confirm_expires_and_blocked_dialog_stops():
    t, sent, now = _texts(["blocked"])
    t.propose("d", "mom", "hi")
    now[0] = messaging.CONFIRM_S + 1
    assert asyncio.run(t.answer("d", "yes")) is None and sent == []
    t.propose("d", "mom", "hi")
    assert "needs a tap" in asyncio.run(t.answer("d", "yes"))


def test_unknown_recipient_is_not_pending():
    t, _, _ = _texts([])
    pending, told = t.propose("d", "Mel", "hi")
    assert pending is None and "not in the family contacts" in told and t.waiting("d") is None


# ---------- status read
def _agent_home(tmp: Path, now: float) -> Path:
    (tmp / "cron").mkdir()
    (tmp / "gateway_state.json").write_text(json.dumps({"gateway_state": "running", "platforms": {
        "bluebubbles": {"state": "connected"}, "telegram": {"state": "disconnected"}}}))
    (tmp / "cron" / "jobs.json").write_text(json.dumps({"jobs": {
        "a": {"name": "Mel check-in", "enabled": True, "last_status": "ok", "next_run_at": "2026-10-03T10:00:00-05:00"},
        "b": {"name": "Firecrawl explore", "enabled": True, "last_status": "error", "failure_streak": 2},
        "c": {"name": "old", "enabled": False}}}))
    con = sqlite3.connect(tmp / "state.db")
    con.execute("CREATE TABLE sessions(source text, title text, started_at real, ended_at real)")
    con.execute("INSERT INTO sessions VALUES ('cron', 'Inbox sweep', ?, NULL)", (now - 60,))
    con.execute("INSERT INTO sessions VALUES ('telegram', 'Grocery list', ?, ?)", (now - 900, now - 800))
    con.commit()
    con.close()
    return tmp


def test_status_read_reports_each_source(tmp_path):
    now = 1_790_000_000.0

    async def launchctl() -> str:
        return ("PID\tStatus\tLabel\n123\t0\tai.skwoop.gateway\n-\t1\tcom.skwoop.guard.heartbeat\n"
                "77\t0\tcom.skwoop.guard.watch\n")

    async def tunnel() -> bool:
        return False

    src = status_read.StatusSources(_agent_home(tmp_path, now), launchctl, tunnel, lambda: now)
    out = asyncio.run(status_read.read_status(src, [{"name": "kitchen", "state": "ok"}]))
    assert "connected: iMessage; not connected: Telegram" in out
    assert "Deep is working on: Inbox sweep (cron" in out and "Last Deep run: Grocery list" in out
    assert "2 enabled; failing: Firecrawl explore (error)" in out
    assert "Guard: 1 of 2 jobs running; last exit not clean: heartbeat" in out
    assert "Deep gateway process: running." in out and "BlueBubbles: not loaded." in out
    assert "Remote host: not connected" in out and "kitchen: ok" in out


def test_status_read_survives_missing_sources(tmp_path):
    async def nothing() -> str:
        raise OSError("no launchctl")

    async def tunnel() -> bool:
        return True

    out = asyncio.run(status_read.read_status(status_read.StatusSources(tmp_path, nothing, tunnel)))
    assert "Deep gateway: unknown" in out and "Guard: unknown" in out and "connected (its tunnel is up)" in out


# ---------- quick search
def test_search_answer_parse_and_speakable():
    body = {"output": [{"type": "web_search_call"}, {"type": "message", "content": [
        {"type": "output_text", "text": "**Bitcoin** is about $61,000 [[1]](https://x.example/a) today."}]}],
        "usage": {"cost_in_usd_ticks": 94_000_000, "server_side_tool_usage_details": {"web_search_calls": 1}}}
    a = parse_answer(body)
    assert a.text == "Bitcoin is about $61,000 today." and a.searches == 1 and abs(a.cost_usd - 0.0094) < 1e-9
    assert speakable("See [the docs](https://a.example) or https://b.example now.") == "See the docs or now."


def test_search_budget_caps_per_day(tmp_path):
    day = [date(2026, 10, 2)]
    b = SearchBudget(tmp_path / "b.json", cap=2, today=lambda: day[0])
    b.check()
    b.spend(1, 0.01)
    b.check()
    b.spend(1, 0.01)
    with pytest.raises(SearchRefused):
        b.check()
    day[0] = date(2026, 10, 3)
    b.check()
    assert json.loads((tmp_path / "b.json").read_text())["total_searches"] == 2


# ---------- house tools against a fake Home Assistant
STATES = [
    {"entity_id": "light.desk_lamp", "state": "on", "attributes": {"brightness": 102}},
    {"entity_id": "light.bedroom_lamp", "state": "off", "attributes": {}},
    {"entity_id": "media_player.living_room", "state": "idle", "attributes": {"app_name": "Netflix"}},
    {"entity_id": "person.alex", "state": "home", "attributes": {"friendly_name": "Alex"}},
    {"entity_id": "person.mel", "state": "not_home", "attributes": {"friendly_name": "Mel"}},
    {"entity_id": "weather.forecast_home", "state": "rainy", "attributes": {"temperature": 81}},
]


class FakeHA(HA):
    def __init__(self) -> None:
        super().__init__(None, "", "t")
        self.calls: list[tuple[str, str, dict]] = []

    async def states(self) -> list[dict]:
        return STATES

    async def service(self, domain: str, service: str, data: dict, response: bool = False):
        self.calls.append((domain, service, data))
        if (domain, service) == ("todo", "get_items"):
            return {"service_response": {data["entity_id"]: {"items": [{"summary": "eggs"}, {"summary": "coffee"}]}}}
        return []


def _tools(ha: FakeHA | None = None, before_10: bool = False) -> HouseTools:
    answers = quick.QuickAnswers(quick.WeatherSource(lambda: asyncio.sleep(0)),
                                 quick.TimerBook(ring=lambda d, r, s: asyncio.sleep(0)))
    t, _, _ = _texts([])
    return HouseTools(ha or FakeHA(), answers.timers, answers, t, None, before_10am=lambda: before_10)


def test_snapshot_has_the_house_in_plain_words():
    snap = asyncio.run(_tools().snapshot("d"))
    assert snap.splitlines() == ["Living room lamp: on at 40%.", "Bedroom light: off.", "Apple TV: idle, app Netflix.",
                                 "Home: Alex; away: Mel.", "Weather here now: rainy, 81°F.", "Pod timer: none."]


def test_lights_level_and_colour_go_to_light_turn_on(monkeypatch):
    ha = FakeHA()
    turn = _tools(ha).for_turn("d", "r")
    out = asyncio.run(turn.run("lights", {"light": "living", "brightness_pct": 30, "color": "warm white"}))
    assert out.say == "Okay." and ha.calls == [("light", "turn_on", {
        "entity_id": ["light.desk_lamp"], "brightness_pct": 30, "color_temp_kelvin": 2700})]


def test_bedroom_rule_and_dry_run(monkeypatch):
    fired: list[str] = []

    async def fire(http, hit):
        fired.append(hit.ack_id)
        return hit

    monkeypatch.setattr(house, "_fire", fire)
    out = asyncio.run(_tools(before_10=True).for_turn("d", "r").run("lights", {"light": "bedroom", "power": "on"}))
    assert out.say == "Bedroom stays off until after 10." and fired == []
    ha = FakeHA()
    asyncio.run(_tools(ha).for_turn("d", "r", dry_run=True).run("lights", {"light": "all", "power": "off"}))
    asyncio.run(_tools(ha).for_turn("d", "r", dry_run=True).run("lists", {"list": "shopping", "action": "add",
                                                                         "item": "milk"}))
    assert fired == [] and ha.calls == []
    asyncio.run(_tools().for_turn("d", "r").run("lights", {"light": "all", "power": "off"}))
    assert fired == ["living_off", "bedroom_off"]


def test_lists_read_and_only_shopping_takes_items():
    ha = FakeHA()
    turn = _tools(ha).for_turn("d", "r")
    assert asyncio.run(turn.run("lists", {"list": "shopping"})).text == "shopping list: eggs; coffee"
    assert asyncio.run(turn.run("lists", {"list": "reminders", "action": "add", "item": "x"})).text.startswith("error")
    assert asyncio.run(turn.run("lists", {"list": "shopping", "action": "add", "item": "milk"})).say == \
        "Added milk to the shopping list."


def test_unknown_tool_and_escalate():
    turn = _tools().for_turn("d", "r")
    assert asyncio.run(turn.run("shell", {})).text == "error: no tool named shell"
    with pytest.raises(Escalate):
        asyncio.run(turn.run("escalate", {"reason": "needs documents"}))


def test_specs_have_no_dev_or_code_tools():
    names = set(house_tools.TOOL_NAMES)
    assert names == {"lights", "apple_tv", "timer", "forecast", "calendar", "lists", "bot_status", "send_text",
                     "web_search", "escalate", "history", "recall_memory"}


def test_calendar_drops_the_next_days_all_day_event_and_speaks_dates():
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    class CalHA(FakeHA):
        async def calendar(self, entity, start, end):
            if entity != "calendar.family":
                return []
            return [{"summary": "Alex Work", "start": {"dateTime": "2026-10-02T07:00:00-05:00"},
                     "end": {"dateTime": "2026-10-02T17:30:00-05:00"}},
                    {"summary": "THIRD TRIMESTER", "start": {"date": "2026-10-03"}, "end": {"date": "2026-10-04"}}]

    tools = _tools(CalHA())
    tools.now = lambda: dt(2026, 10, 2, 13, 0, tzinfo=ZoneInfo("America/Chicago"))
    out = asyncio.run(tools.for_turn("d", "r").run("calendar", {"day_offset": 0, "days": 1})).text
    assert out == "Calendar Fri Oct 2: Family: Fri Oct 2 7 AM to 5:30 PM: Alex Work"
    out = asyncio.run(tools.for_turn("d", "r").run("calendar", {"day_offset": 1, "days": 1})).text
    assert out == "Calendar Sat Oct 3: Family: Sat Oct 3 all day: THIRD TRIMESTER"


def test_upgraded_apple_reminders_list_is_reported_not_read_out():
    class StubHA(FakeHA):
        async def service(self, domain, service, data, response=False):
            return {"service_response": {data["entity_id"]: {"items": [
                {"summary": "The creator of this list has upgraded these reminders."},
                {"summary": "Where are my reminders?"}]}}}

    out = asyncio.run(_tools(StubHA()).for_turn("d", "r").run("lists", {"list": "reminders"})).text
    assert "not readable" in out and "Where are my" not in out


def test_held_turn_reads_but_never_acts(monkeypatch):
    fired: list[str] = []

    async def fire(http, hit):
        fired.append(hit.ack_id)
        return hit

    monkeypatch.setattr(house, "_fire", fire)
    ha = FakeHA()
    turn = _tools(ha).for_turn("d", "r", hold="only asked a question")
    out = asyncio.run(turn.run("apple_tv", {"action": "on"}))
    assert out.say is None and out.text.startswith("not done") and fired == []
    assert asyncio.run(turn.run("lists", {"list": "shopping"})).text == "shopping list: eggs; coffee"
    assert asyncio.run(turn.run("timer", {"action": "left"})).say is not None


def test_search_answer_is_cut_to_a_speakable_length():
    from tools.quick_search import brief
    long = ("SpaceX completed three orbital launches in under 13 hours on October 2, 2026: "
            "a Crew-13 Dragon to the ISS, a Transporter-18 rideshare with 130 satellites from California, "
            "and a Falcon Heavy carrying the classified NROL-97 payload from Florida. "
            "This came days after Starship Flight 14 reached orbit.")
    out = brief(long)
    assert out == ("SpaceX completed three orbital launches in under 13 hours on October 2, 2026: "
                   "a Crew-13 Dragon to the ISS.")
    assert brief("Bitcoin is $61,000. It rose 2% today. More text here that is long.") == \
        "Bitcoin is $61,000. It rose 2% today. More text here that is long."
    assert len(brief("word " * 200)) <= 171


def test_calendar_reads_the_prefetched_window_until_it_goes_stale():
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    class CountHA(FakeHA):
        n = 0

        async def calendar(self, entity, start, end):
            CountHA.n += 1
            if entity != "calendar.family":
                return []
            return [{"summary": "Dentist", "start": {"dateTime": "2026-10-05T09:00:00-05:00"},
                     "end": {"dateTime": "2026-10-05T10:00:00-05:00"}}]

    tools = _tools(CountHA())
    tools.now = lambda: dt(2026, 10, 2, 13, 0, tzinfo=ZoneInfo("America/Chicago"))
    now = [100.0]
    tools.clock = lambda: now[0]
    asyncio.run(tools.prefetch_calendars())
    assert CountHA.n == 2
    turn = tools.for_turn("d", "r")
    assert asyncio.run(turn.run("calendar", {"day_offset": 0, "days": 1})).text == (
        "Calendar Fri Oct 2: nothing scheduled")
    out = asyncio.run(turn.run("calendar", {"day_offset": 3, "days": 1})).text
    assert out == "Calendar Mon Oct 5: Family: Mon Oct 5 9 AM to 10 AM: Dentist" and CountHA.n == 2
    now[0] += house_tools.CAL_TTL_S + 1                      # stale -> live read
    asyncio.run(turn.run("calendar", {"day_offset": 0, "days": 1}))
    assert CountHA.n == 4


def _hist_tools(hist: dict):
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    class HistHA(FakeHA):
        asked: list = []

        async def history(self, entities, start, end):
            HistHA.asked.append((entities, start, end))
            return {e: hist[e] for e in entities if e in hist}

    tools = _tools(HistHA())
    tools.now = lambda: dt(2026, 10, 2, 17, 0, tzinfo=ZoneInfo("America/Chicago"))
    return tools, HistHA.asked


def test_internet_history_reports_outages_ping_and_drops():
    hist = {
        "binary_sensor.starlink_connectivity": [
            {"entity_id": "binary_sensor.starlink_connectivity", "state": "on",
             "last_changed": "2026-09-25T22:00:00+00:00"},
            {"state": "off", "last_changed": "2026-09-30T03:10:00+00:00"},
            {"state": "on", "last_changed": "2026-09-30T03:22:00+00:00"}],
        "sensor.starlink_ping": [
            {"entity_id": "sensor.starlink_ping", "state": "30", "last_changed": "2026-09-25T22:00:00+00:00"},
            {"state": "250", "last_changed": "2026-10-01T20:00:00+00:00"},
            {"state": "40", "last_changed": "2026-10-01T20:01:00+00:00"}],
        "sensor.starlink_ping_drop_rate": [
            {"entity_id": "sensor.starlink_ping_drop_rate", "state": "0.0",
             "last_changed": "2026-09-25T22:00:00+00:00"}],
        "binary_sensor.starlink_obstructed": [
            {"entity_id": "binary_sensor.starlink_obstructed", "state": "off",
             "last_changed": "2026-09-25T22:00:00+00:00"}],
    }
    tools, asked = _hist_tools(hist)
    out = asyncio.run(tools.for_turn("d", "r").run("history", {"what": "Starlink", "hours": 168})).text
    assert out.startswith("History, last 7 days: Starlink down 1 time, 12 min in total, longest 12 min (Tue 10:10 PM).")
    assert "Ping: average " in out and "high 250 ms (Thu 3 PM)" in out
    assert "Dropped pings: steady at 0.0%." in out and out.endswith("Dish obstructed: never.")
    assert sorted(asked[0][0]) == sorted(house_tools.INTERNET)
    asyncio.run(tools.for_turn("d", "r").run("history", {"what": "internet", "hours": 168}))
    assert len(asked) == 1                                    # cached


def test_history_refuses_other_domains_and_summarises_a_binary_sensor():
    hist = {"binary_sensor.door": [
        {"entity_id": "binary_sensor.door", "state": "off", "last_changed": "2026-10-01T22:00:00+00:00"},
        {"state": "on", "last_changed": "2026-10-02T10:00:00+00:00"},
        {"state": "off", "last_changed": "2026-10-02T16:00:00+00:00"}]}
    tools, _ = _hist_tools(hist)
    turn = tools.for_turn("d", "r")
    assert asyncio.run(turn.run("history", {"what": "light.living_room"})).text.startswith("error:")
    out = asyncio.run(turn.run("history", {"what": "binary_sensor.door", "hours": 24})).text
    assert out == "History, last 24 hours: binary_sensor.door: off 75% of the time, on 25% of the time, 2 changes."


def test_recall_memory_reads_the_injected_recall_and_reports_a_dead_daemon():
    from memory.memory_recall import RecallError

    asked: list[str] = []

    async def recall(query: str) -> str:
        asked.append(query)
        return "From Skwoop's long-term memory:\n- Mel is Alex's wife."

    async def dead(query: str) -> str:
        raise RecallError("recall HTTP 500")

    tools = _tools()
    tools.recall = recall
    out = asyncio.run(tools.for_turn("d", "r").run("recall_memory", {"query": "who is my wife"}))
    assert asked == ["who is my wife"] and "Mel is Alex's wife." in out.text and out.say is None

    tools.recall = dead
    out = asyncio.run(tools.for_turn("d", "r").run("recall_memory", {"query": "who is my wife"}))
    assert out.text.startswith("error:") and "escalate" in out.text

    tools.recall = None
    assert asyncio.run(tools.for_turn("d", "r").run("recall_memory", {"query": "x"})).text.startswith("error:")
