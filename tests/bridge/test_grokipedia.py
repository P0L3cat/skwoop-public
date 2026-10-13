"""The Grokipedia lead: typeahead snippet, exact-title rule, in-memory cache. A local fake server stands in."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from lean_http import ClientError, ClientSession, web

import tools.grokipedia as grokipedia
from tools.grokipedia import Grokipedia, Lead

MAGNET = "A magnet is any object that produces a magnetic field. It attracts iron and some other metals, which…"


def _hit(title: str, snippet: str = MAGNET, score: float = 100000) -> dict[str, object]:
    return {"slug": title.replace(" ", "_"), "title": title, "relevanceScore": score, "snippet": snippet}


class FakeWiki:
    def __init__(self, hits: dict[str, list[dict[str, object]]]) -> None:
        self.hits = hits
        self.queries: list[str] = []

    async def typeahead(self, request: web.Request) -> web.Response:
        q = request.query["query"]
        self.queries.append(q)
        return web.json_response({"results": self.hits.get(q, [])})


async def _with_wiki(fake: FakeWiki, go: Callable[[Grokipedia], Awaitable[object]]) -> object:
    app = web.Application()
    app.router.add_get("/api/typeahead", fake.typeahead)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        async with ClientSession() as http:
            return await go(Grokipedia(http, base=f"http://127.0.0.1:{runner.addresses[0][1]}"))
    finally:
        await runner.cleanup()


def test_a_plural_ask_finds_the_singular_article_and_caches_hits_and_misses() -> None:
    fake = FakeWiki({"magnets": [_hit("The Magnets")], "magnet": [_hit("Magnet")]})

    async def go(g: Grokipedia) -> list[Lead | None]:
        return [await g.lead("magnets"), await g.lead("Magnets"), await g.lead("zorblax"), await g.lead("zorblax")]

    out = asyncio.run(_with_wiki(fake, go))
    lead = Lead("Magnet", "A magnet is any object that produces a magnetic field.")
    assert out == [lead, lead, None, None]
    assert fake.queries == ["magnets", "magnet", "zorblax"]      # "The Magnets" (a band) is not the topic


def test_an_http_error_raises() -> None:
    async def go() -> None:
        async with ClientSession() as http:
            await Grokipedia(http, base="http://127.0.0.1:9").lead("x")

    with pytest.raises(ClientError):
        asyncio.run(go())


@pytest.mark.parametrize("raw", [None, [], {"results": "x"}, {"results": [3, {"title": "Magnet"}]}])
def test_bad_typeahead_bodies_give_no_lead(raw: object) -> None:
    assert grokipedia.best_lead("magnet", raw) is None


def test_the_best_scoring_exact_title_wins_and_whole_snippets_stay_whole() -> None:
    raw = {"results": [_hit("Magnet", "Low score but a long enough sentence.", 1), _hit("Magnet", "A whole snippet "
                                                                                      "with no cut at the end.", 9)]}
    assert grokipedia.best_lead("magnet", raw) == Lead("Magnet", "A whole snippet with no cut at the end.")


@pytest.mark.parametrize("heard, topic", [("Who was Abraham Lincoln?", "Abraham Lincoln"),
                                          ("What is a platypus?", "platypus"),
                                          ("How do magnets work?", "magnets"),
                                          ("Hey Skwoop, tell me about the Roman Empire.", "Roman Empire"),
                                          ("Explain photosynthesis.", "photosynthesis")])
def test_topic_of(heard: str, topic: str) -> None:
    assert grokipedia.topic_of(heard) == topic
