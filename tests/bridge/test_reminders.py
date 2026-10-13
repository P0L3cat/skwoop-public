"""Reminders: the pod says the time back at once, holds the reminder 60 s, then writes one calendar.create_event to
calendar.work; "never mind" inside the window drops it unwritten; held reminders survive a restart; bench pods and
health details never write; no clear time asks one question. HA is a recording fake, the clock an injected wall."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tools.reminders as reminders
from tools.house_tools import HAError
from tools.reminders import Reminders

T0 = datetime(2026, 10, 6, 9, 30, tzinfo=ZoneInfo("America/Chicago")).timestamp()   # a Tuesday


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


class FakeCalendar:
    def __init__(self, fail: int = 0) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def __call__(self, domain: str, service: str, data: dict[str, str]) -> object:
        if self.fail:
            self.fail -= 1
            raise HAError("HA 500")
        self.calls.append((domain, service, data))
        return None


def _rem(tmp: Path, cal: FakeCalendar | None = None) -> tuple[Reminders, FakeCalendar, Clock]:
    cal, clock = cal or FakeCalendar(), Clock()
    return Reminders(cal, tmp / "pending.json", clock), cal, clock


def _remind(rem: Reminders, when: str, text: str = "Call the vet.", dry: bool = False, ask: str = "") -> str:
    out = asyncio.run(rem.remind({"text": text, "when": when, "ask": ask}, "office", dry_run=dry))
    assert out.say is not None
    return out.say


def test_the_pod_confirms_at_once_and_writes_the_event_after_the_hold(tmp_path: Path) -> None:
    rem, cal, clock = _rem(tmp_path)
    assert _remind(rem, "2026-10-13T15:00") == "I'll remind you Tuesday, October 13 at 3 PM: Call the vet."
    clock.t += 59
    assert asyncio.run(rem.flush()) == 0 and cal.calls == []
    clock.t += 1
    assert asyncio.run(rem.flush()) == 1 and rem.held == []
    assert cal.calls == [("calendar", "create_event", {
        "entity_id": "calendar.work", "summary": "Call the vet", "description": "Set from a Skwoop pod",
        "start_date_time": "2026-10-13 15:00:00-05:00", "end_date_time": "2026-10-13 15:15:00-05:00"})]


def test_never_mind_inside_the_window_drops_it_unwritten(tmp_path: Path) -> None:
    rem, cal, clock = _rem(tmp_path)
    _remind(rem, "2026-10-06T15:00")
    assert rem.cancel("kitchen") is None                                                  # another pod's undo
    out = rem.cancel("office")
    assert out is not None and out.say == reminders.SAID_CANCELLED and not rem.pending("office")
    clock.t += 120
    assert asyncio.run(rem.flush()) == 0 and cal.calls == []
    assert rem.cancel("office") is None


def test_never_mind_after_the_write_says_it_is_on_the_calendar(tmp_path: Path) -> None:
    rem, cal, clock = _rem(tmp_path)
    _remind(rem, "2026-10-06T15:00", "pick up the dry cleaning")
    clock.t += 61
    asyncio.run(rem.flush())
    assert cal.calls[0][2]["summary"] == "Pick up the dry cleaning" and rem.pending("office")
    out = rem.cancel("office")
    assert out is not None and out.say == reminders.SAID_TOO_LATE and rem.cancel("office") is None
    _remind(rem, "2026-10-06T16:00")
    clock.t += 61
    asyncio.run(rem.flush())
    clock.t += reminders.LATE_S
    assert not rem.pending("office") and rem.cancel("office") is None


def test_held_reminders_survive_a_restart_and_overdue_ones_are_written(tmp_path: Path) -> None:
    rem, _, clock = _rem(tmp_path)
    _remind(rem, "2026-10-07T08:15")
    cal = FakeCalendar()
    later = Reminders(cal, tmp_path / "pending.json", clock)                             # the bridge restarted
    assert [h.text for h in later.held] == ["Call the vet"]
    clock.t += 3600                                                                     # ...after an hour down
    assert asyncio.run(later.flush()) == 1 and cal.calls[0][2]["start_date_time"] == "2026-10-07 08:15:00-05:00"
    assert Reminders(FakeCalendar(), tmp_path / "pending.json", clock).held == []


def test_a_failed_write_is_kept_and_retried(tmp_path: Path) -> None:
    rem, cal, clock = _rem(tmp_path, FakeCalendar(fail=1))
    _remind(rem, "2026-10-06T15:00")
    clock.t += 61
    assert asyncio.run(rem.flush()) == 0 and len(rem.held) == 1
    assert asyncio.run(rem.flush()) == 1 and len(cal.calls) == 1


def test_a_bench_pod_hears_the_confirm_but_nothing_is_held(tmp_path: Path) -> None:
    rem, cal, clock = _rem(tmp_path)
    assert _remind(rem, "2026-10-06T15:00", dry=True) == "I'll remind you today at 3 PM: Call the vet."
    clock.t += 120
    assert rem.held == [] and asyncio.run(rem.flush()) == 0 and not (tmp_path / "pending.json").exists()


def test_health_details_are_refused(tmp_path: Path) -> None:
    rem, _, _ = _rem(tmp_path)
    assert _remind(rem, "2026-10-06T21:00", "Take the blood pressure pills") == reminders.SAID_HEALTH
    assert rem.held == []


@pytest.mark.parametrize("when", ["", "3", "2026-10-06T08:00", "2028-01-01T09:00"])   # none, bare, past, too far
def test_no_clear_time_asks_one_question_and_holds_nothing(tmp_path: Path, when: str) -> None:
    rem, _, clock = _rem(tmp_path)
    assert _remind(rem, when, ask="Three in the afternoon or the morning?") == "Three in the afternoon or the morning?"
    assert rem.held == [] and rem.asking("office") and not rem.asking("kitchen")
    clock.t += reminders.ASK_S
    assert not rem.asking("office")


def test_no_question_from_the_model_falls_back_to_ours(tmp_path: Path) -> None:
    rem, _, _ = _rem(tmp_path)
    assert _remind(rem, "") == reminders.SAID_WHEN
    _remind(rem, "2026-10-06T15:00")
    assert not rem.asking("office")


@pytest.mark.parametrize("when, said", [("2026-10-06T17:30", "today at 5:30 PM"),
                                        ("2026-10-07T09:00", "tomorrow at 9 AM"),
                                        ("2026-10-10T12:00", "Saturday at 12 PM"),
                                        ("2026-12-25T07:05", "Friday, December 25 at 7:05 AM")])
def test_the_time_is_said_back_with_the_day_and_am_pm(tmp_path: Path, when: str, said: str) -> None:
    rem, _, _ = _rem(tmp_path)
    assert _remind(rem, when) == f"I'll remind you {said}: Call the vet."


def test_a_hand_edited_file_starts_empty(tmp_path: Path) -> None:
    (tmp_path / "pending.json").write_text("{not json")
    assert _rem(tmp_path)[0].held == []


def test_one_timer_at_the_next_due_time_and_none_when_nothing_is_held(tmp_path: Path) -> None:
    """No polling: an idle bridge has no reminder timer; a held reminder sets one at its due time; a failed write
    re-arms RETRY_S later."""
    rem, cal, clock = _rem(tmp_path, FakeCalendar(fail=1))

    async def go() -> None:
        loop = asyncio.get_running_loop()
        rem.start()
        assert rem._timer is None
        await rem.remind({"text": "call the vet", "when": "2026-10-06T15:00"}, "office")
        assert rem._timer is not None and abs(rem._timer.when() - loop.time() - reminders.HOLD_S) < 1
        rem.cancel("office")
        assert rem._timer is None
        await rem.remind({"text": "call the vet", "when": "2026-10-06T15:00"}, "office")
        clock.t += reminders.HOLD_S
        rem.arm()                                     # the fake clock jumped: due now
        await asyncio.sleep(0.05)
        assert rem._flushing is not None
        await rem._flushing                           # the write failed: kept, retried RETRY_S later
        assert cal.calls == [] and rem._timer is not None
        assert rem._timer.when() - loop.time() > reminders.RETRY_S - 1
        rem._timer.cancel()
        rem._due()
        await rem._flushing
        assert len(cal.calls) == 1 and rem.held == [] and rem._timer is None
        rem.stop()

    asyncio.run(go())
