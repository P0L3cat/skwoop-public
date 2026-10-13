"""Harness phase 3: memory writes. Health details are refused before any call, a recall dedupes, the write carries
the source tags and a document id, "forget that" deletes it within a minute on the same pod, bench pods never write.
The memory service is a local fake server for the HTTP client and a fake store for the writer."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from lean_http import ClientSession, web

import memory.memory_write as memory_write
from memory.memory_recall import MemoryFact, RecallError
from memory.memory_write import MemoryService, MemoryWriteError, MemoryWriter, RetainItem

NOW = datetime(2026, 10, 6, 9, 30, tzinfo=ZoneInfo("America/Chicago"))


class FakeStore:
    def __init__(self, known: tuple[str, ...] = (), fail: bool = False) -> None:
        self.known, self.fail = known, fail
        self.recalls: list[str] = []
        self.retained: list[RetainItem] = []
        self.deleted: list[str] = []

    async def recall(self, query: str) -> tuple[MemoryFact, ...]:
        self.recalls.append(query)
        if self.fail:
            raise RecallError("down")
        return tuple(MemoryFact("world", k) for k in self.known)

    async def retain(self, item: RetainItem) -> None:
        self.retained.append(item)

    async def delete(self, doc_id: str) -> None:
        self.deleted.append(doc_id)


def _writer(store: FakeStore, now: list[float]) -> MemoryWriter:
    return MemoryWriter(store, lambda: NOW, clock=lambda: now[0])


def test_a_new_fact_is_written_with_tags_and_a_document_id() -> None:
    store, now = FakeStore(("The trash goes out on Mondays.",)), [0.0]
    r = asyncio.run(_writer(store, now).remember({"fact": "The plumber comes on Tuesdays."}, "web-pod-desk", "office"))
    assert r.say == "Got it: The plumber comes on Tuesdays." and store.recalls == ["The plumber comes on Tuesdays."]
    item = store.retained[0]
    assert item["strategy"] == "grokbot_verbatim" and item["context"] == "household fact (skwoop pod)"
    assert item["tags"] == ["source:skwoop-pod", "room:office"]
    assert item["metadata"] == {"source": "skwoop-pod", "at": "2026-10-06T09:30:00-05:00", "device": "web-pod-desk"}
    assert item["document_id"].startswith("skwoop-pod-20261006-093000-")


def test_a_fact_memory_already_has_is_not_written_again() -> None:
    store = FakeStore(("the plumber comes on tuesdays",))
    r = asyncio.run(_writer(store, [0.0]).remember({"fact": "The plumber comes on Tuesdays."}, "d", "office"))
    assert r.say == memory_write.SAID_HAVE and store.retained == []


@pytest.mark.parametrize("fact", ["Alex takes the blood pressure pills at night.", "Alex's doctor is on Elm Street.",
                                  "The kid has a peanut allergy.", "Alex's insulin is in the fridge."])
def test_health_details_are_refused_before_any_call(fact: str) -> None:
    store = FakeStore()
    r = asyncio.run(_writer(store, [0.0]).remember({"fact": fact}, "d", "office"))
    assert r.say == memory_write.SAID_HEALTH and store.recalls == [] and store.retained == []


@pytest.mark.parametrize("fact", ["", "yes", "x" * 400])
def test_fragments_and_long_text_are_refused(fact: str) -> None:
    store = FakeStore()
    r = asyncio.run(_writer(store, [0.0]).remember({"fact": fact}, "d", "office"))
    assert r.text.startswith("refused") and store.retained == []


def test_a_bench_pod_recalls_but_never_writes() -> None:
    store = FakeStore()
    writer = _writer(store, [0.0])
    r = asyncio.run(writer.remember({"fact": "The plumber comes on Tuesdays."}, "d", "office", dry_run=True))
    assert r.text.startswith("dry run") and store.recalls and store.retained == []


def test_no_memory_means_no_write() -> None:
    store = FakeStore(fail=True)
    r = asyncio.run(_writer(store, [0.0]).remember({"fact": "The plumber comes on Tuesdays."}, "d", "office"))
    assert r.say == memory_write.SAID_FAILED and store.retained == []


def test_forget_that_deletes_the_last_write_from_the_same_pod_within_a_minute() -> None:
    store, now = FakeStore(), [100.0]
    w = _writer(store, now)

    async def go() -> list[str | None]:
        await w.remember({"fact": "The plumber comes on Tuesdays."}, "desk", "office")
        other = await w.forget_last("kitchen")
        now[0] += 30
        mine = await w.forget_last("desk")
        again = await w.forget_last("desk")
        return [other.say, mine.say, again.say]

    assert asyncio.run(go()) == [memory_write.SAID_NOTHING, memory_write.SAID_FORGOT, memory_write.SAID_NOTHING]
    assert store.deleted == [store.retained[0]["document_id"]]


def test_forget_after_a_minute_is_too_late() -> None:
    store, now = FakeStore(), [0.0]
    w = _writer(store, now)
    asyncio.run(w.remember({"fact": "The plumber comes on Tuesdays."}, "desk", "office"))
    now[0] += memory_write.UNDO_S + 1
    assert asyncio.run(w.forget_last("desk")).say == memory_write.SAID_NOTHING and store.deleted == []


def test_same_fact() -> None:
    assert memory_write.same_fact("The plumber comes on Tuesdays.", "the plumber comes tuesdays")
    assert not memory_write.same_fact("The plumber comes on Tuesdays.", "The electrician comes on Fridays.")


class FakeMemoryService:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.bodies: list[object] = []
        self.deleted: list[str] = []

    async def recall(self, request: web.Request) -> web.Response:
        self.bodies.append(await request.json())
        return web.json_response({"results": [{"type": "world", "text": "The gate code is 1234."}]})

    async def retain(self, request: web.Request) -> web.Response:
        self.bodies.append(await request.json())
        return web.json_response({"success": self.success, "bank_id": "skwoop", "items_count": 1, "async": False})

    async def delete(self, request: web.Request) -> web.Response:
        self.deleted.append(request.match_info["doc"])
        return web.json_response({"success": True, "message": "ok", "document_id": request.match_info["doc"],
                                  "memory_units_deleted": 1})


async def _with_daemon(fake: FakeMemoryService, item: RetainItem) -> tuple[tuple[MemoryFact, ...], None]:
    app = web.Application()
    app.router.add_post("/v1/default/banks/skwoop/memories/recall", fake.recall)
    app.router.add_post("/v1/default/banks/skwoop/memories", fake.retain)
    app.router.add_delete("/v1/default/banks/skwoop/documents/{doc}", fake.delete)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    try:
        async with ClientSession() as http:
            service = MemoryService(http, f"http://127.0.0.1:{port}/v1/default/banks/skwoop")
            facts = await service.recall("gate code")
            await service.retain(item)
            await service.delete(item["document_id"])
            return facts, None
    finally:
        await runner.cleanup()


def test_the_daemon_client_recalls_retains_and_deletes() -> None:
    fake = FakeMemoryService()
    item = memory_write.retain_item("The gate code changed.", NOW, "desk", "office")
    facts, _ = asyncio.run(_with_daemon(fake, item))
    assert facts == (MemoryFact("world", "The gate code is 1234."),)
    assert fake.bodies[1] == {"items": [dict(item)], "async": False} and fake.deleted == [item["document_id"]]


def test_a_daemon_without_success_raises() -> None:
    item = memory_write.retain_item("The gate code changed.", NOW, "desk", "office")
    with pytest.raises(MemoryWriteError):
        asyncio.run(_with_daemon(FakeMemoryService(success=False), item))
