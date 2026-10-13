"""The phone widget's fields: /status "mood" and "next", and GET /face.png. Real StatusStore, real HTTP app,
and HouseTools against a fake Home Assistant calendar (the same seam as test_tiers)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from lean_http import ClientSession, web
from test_tiers import FakeHA, _tools

from status_store import FACES_DIR, StatusStore

TZ = ZoneInfo("America/Chicago")
READ = {"Authorization": "Bearer widget-key"}


async def _up(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[-1][1]}"


async def _get(http: ClientSession, url: str, headers: dict[str, str] | None = None,
               params: dict[str, str] | None = None) -> tuple[int, str, bytes]:
    async with http.get(url, headers=headers, params=params) as r:
        return r.status, r.content_type, await r.read()


class CalHA(FakeHA):
    async def calendar(self, entity, start, end):
        if entity == "calendar.work":
            return [{"summary": "Standup", "start": {"dateTime": "2026-10-03T09:00:00-05:00"},
                     "end": {"dateTime": "2026-10-03T09:15:00-05:00"}},             # already over
                    {"summary": "Ship review", "start": {"dateTime": "2026-10-05T10:00:00-05:00"},
                     "end": {"dateTime": "2026-10-05T11:00:00-05:00"}}]

        return [{"summary": "Mel's birthday", "start": {"date": "2026-10-03"}, "end": {"date": "2026-10-04"}},
                {"summary": "Dentist", "start": {"dateTime": "2026-10-03T15:00:00-05:00"},
                 "end": {"dateTime": "2026-10-03T16:00:00-05:00"}},
                "not an event"]


def test_next_is_the_soonest_item_that_has_not_ended():
    tools = _tools(CalHA())
    now = [datetime(2026, 10, 3, 14, 12, tzinfo=TZ)]
    tools.now = lambda: now[0]
    assert tools.next_event() is None                        # nothing prefetched yet
    asyncio.run(tools.prefetch_calendars())
    assert tools.next_event() == {"title": "Dentist", "start": "2026-10-03T15:00:00-05:00", "all_day": False,
                                  "calendar": "Family"}
    now[0] = datetime(2026, 10, 3, 16, 30, tzinfo=TZ)        # dentist over: today's all-day item is next
    assert tools.next_event() == {"title": "Mel's birthday", "start": "2026-10-03", "all_day": True,
                                  "calendar": "Family"}
    now[0] = datetime(2026, 10, 4, 8, 0, tzinfo=TZ)
    assert tools.next_event()["title"] == "Ship review"


def test_the_calendar_refreshes_on_reads_only_when_stale():
    """No schedule: a /status read kicks a background refresh only when the prefetch is older than CAL_PREFETCH_S."""
    import tools.house_tools as house_tools
    ha = CalHA()
    tools = _tools(ha)
    t = [1000.0]
    tools.clock = lambda: t[0]

    async def go():
        tools.next_event_lazy()                               # nothing prefetched: refresh behind the answer
        assert tools._cal_task is not None
        await tools._cal_task
        first = tools._cal_task
        tools.next_event_lazy()                               # fresh: no work
        assert tools._cal_task is first
        t[0] += house_tools.CAL_PREFETCH_S
        tools.next_event_lazy()
        assert tools._cal_task is not first
        await tools._cal_task

    asyncio.run(go())


def test_mood_follows_skwoop_and_alerts(tmp_path):
    store = StatusStore(path=str(tmp_path / "status.json"))
    assert store.feed()["mood"] == "idle" and store.feed()["next"] is None   # no Skwoop entry, no calendar
    store.upsert_bot("Skwoop", "busy", "deploying")
    assert store.feed()["mood"] == "thinking"
    store.upsert_bot("Skwoop", "error", "build failed")
    assert store.feed()["mood"] == "sad"
    store.set_alert("Timer done", level="info")
    assert store.feed()["mood"] == "sad"                      # an info alert keeps the face
    store.set_alert("Starlink down", level="warn")
    assert store.feed()["mood"] == "alert"
    store.clear_alert()
    store.bots["Skwoop"]["last_seen_ts"] -= store.stale_s + 1
    assert store.feed()["mood"] == "sleep"


def test_status_and_face_png_over_http(tmp_path):
    store = StatusStore(path=str(tmp_path / "status.json"), read_key="widget-key")
    store.next_item = lambda: {"title": "Dentist", "start": "2026-10-03T15:00:00-05:00", "all_day": False,
                               "calendar": "Family"}
    store.upsert_bot("Skwoop", "busy", "deploying")
    app = web.Application()
    app.add_routes(store.routes())

    async def go() -> None:
        runner, base = await _up(app)
        try:
            async with ClientSession() as http:
                assert (await _get(http, base + "/status"))[0] == 401          # read key required (Oct 3 2026)
                assert (await _get(http, base + "/face.png"))[0] == 401
                assert (await _get(http, base + "/status", params={"key": "wrong"}))[0] == 401
                assert (await _get(http, base + "/status", params={"key": "wïdget"}))[0] == 401  # non-ASCII: 401
                status, _ctype, raw = await _get(http, base + "/status", headers=READ)
                feed = json.loads(raw)
                assert status == 200 and feed["mood"] == "thinking" and feed["next"]["title"] == "Dentist"
                assert (await _get(http, base + "/status", params={"key": "widget-key"}))[0] == 200
                status, ctype, body = await _get(http, base + "/face.png", headers=READ)
                assert status == 200 and ctype == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"
                assert body == (FACES_DIR / "thinking.png").read_bytes()
        finally:
            await runner.cleanup()

    asyncio.run(go())



def test_reads_fail_closed_without_a_read_key(tmp_path):
    store = StatusStore(path=str(tmp_path / "status.json"), read_key="")
    app = web.Application()
    app.add_routes(store.routes())

    async def go() -> None:
        runner, base = await _up(app)
        try:
            async with ClientSession() as http:
                assert (await _get(http, base + "/status"))[0] == 401
                assert (await _get(http, base + "/status", params={"key": ""}))[0] == 401
                assert (await _get(http, base + "/face.png", headers={"Authorization": "Bearer "}))[0] == 401
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_an_unauthorized_poll_does_not_register_a_pod(tmp_path):
    store = StatusStore(path=str(tmp_path / "status.json"), read_key="widget-key")
    app = web.Application()
    app.add_routes(store.routes())

    async def go() -> None:
        runner, base = await _up(app)
        try:
            async with ClientSession() as http:
                got = await _get(http, base + "/status", headers={"Device-Id": "aa:bb:cc:00:00:09"})
                assert got[0] == 401
                assert store.bots == {}
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_pod_studio_reads_with_the_device_token_or_a_doubled_question_mark(tmp_path):
    """Oct 4 2026: Pod Studio (desktop sim) polls with the pod device token, or appends ?since= to status_url."""
    store = StatusStore(path=str(tmp_path / "status.json"), read_key="widget-key", device_token="dev-tok")
    app = web.Application()
    app.add_routes(store.routes())

    async def go() -> None:
        runner, base = await _up(app)
        try:
            async with ClientSession() as http:
                tok = {"Authorization": "Bearer dev-tok"}
                assert (await _get(http, base + "/status", headers=tok))[0] == 200
                assert (await _get(http, base + "/face.png", headers=tok))[0] == 200
                assert (await _get(http, base + "/status?key=widget-key?since=0"))[0] == 200
                assert (await _get(http, base + "/status?key=widget-key&since=0"))[0] == 200
                # since/wait still read from the tail
                assert (await _get(http, base + "/status?key=widget-key?since=999&wait=0.2"))[0] == 200
                assert (await _get(http, base + "/status", headers={"Authorization": "Bearer wrong"}))[0] == 401
                assert (await _get(http, base + "/status", headers={"Authorization": "Bearer "}))[0] == 401
                assert (await _get(http, base + "/status?key=wrong?since=0"))[0] == 401
                assert (await _get(http, base + "/status?key=?key=widget-key"))[0] == 401
                assert (await _get(http, base + "/face.png?key=dev-tok"))[0] == 401  # token is a header, not ?key=
                assert (await _get(http, base + "/status"))[0] == 401
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_no_device_token_configured_means_no_token_reads(tmp_path):
    store = StatusStore(path=str(tmp_path / "status.json"), read_key="widget-key", device_token="")
    app = web.Application()
    app.add_routes(store.routes())

    async def go() -> None:
        runner, base = await _up(app)
        try:
            async with ClientSession() as http:
                assert (await _get(http, base + "/status", headers={"Authorization": "Bearer "}))[0] == 401
                assert (await _get(http, base + "/status", headers={"Authorization": "Bearer"}))[0] == 401
        finally:
            await runner.cleanup()

    asyncio.run(go())
