"""Cancel-in-flight must not write; non-HAError must not kill flush."""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

import tools.reminders as R


def test_cancel_during_slow_ha_does_not_write() -> None:
    async def go() -> None:
        t = [datetime(2026, 10, 6, 19, 0, tzinfo=R.TZ).timestamp()]
        written: list[str] = []
        gate = asyncio.Event()

        async def slow_call(domain: str, service: str, data: dict[str, str]) -> None:
            await gate.wait()
            written.append(data["summary"])

        r = R.Reminders(slow_call, Path(tempfile.mkdtemp()) / "p.json", wall=lambda: t[0])
        await r.remind({"text": "call mom", "when": "2026-10-07T15:00"}, "pod1")
        t[0] += 61
        # Use the real flush task slot so cancel() aborts the in-flight HA call.
        r._flushing = asyncio.create_task(r._flush_and_rearm())
        await asyncio.sleep(0)
        out = r.cancel("pod1")
        assert out is not None and "won't remind" in out.say
        gate.set()
        await asyncio.sleep(0.05)
        assert written == []

    asyncio.run(go())


def test_valueerror_from_ha_does_not_kill_flush_task() -> None:
    async def go() -> None:
        t = [datetime(2026, 10, 6, 19, 0, tzinfo=R.TZ).timestamp()]
        calls = 0

        async def call(domain: str, service: str, data: dict[str, str]) -> None:
            nonlocal calls
            calls += 1
            raise ValueError("Expecting value")

        r = R.Reminders(call, Path(tempfile.mkdtemp()) / "p.json", wall=lambda: t[0])
        await r.remind({"text": "take out trash", "when": "2026-10-07T15:00"}, "pod1")
        t[0] += 61
        n = await r.flush()
        assert n == 0 and calls >= 1 and len(r.held) == 1

    asyncio.run(go())
