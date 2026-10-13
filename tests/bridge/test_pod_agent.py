"""The pod model loop against a scripted model client (no network), its round caps and registry limits, and the
proxy client against a local fake proxy."""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from lean_http import ClientSession
from lean_http import web

from tools.house_tools import Escalate, Outcome
from harness.pod_agent import (
    BUSY_LINE,
    DEEP,
    FAST,
    LIMIT_LINE,
    AgentStats,
    ChatMessage,
    LimitReached,
    ModelEvent,
    ModelRequest,
    ModelUnavailable,
    ProxyClient,
    TextDelta,
    TierConfig,
    ToolCall,
    ToolFn,
    UserMessage,
    Usage,
    sentences,
)
from tools.quick_search import DUMMY_BEARER
from harness.toolsets import SCHEMAS, JsonValue

BRIDGE = Path(__file__).resolve().parents[2] / "bridge"


class ScriptedModel:
    """One script per model round: events to stream, or an exception to raise."""

    def __init__(self, rounds: list[list[ModelEvent] | Exception]) -> None:
        self.rounds = rounds
        self.requests: list[ModelRequest] = []

    async def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(req)
        script = self.rounds.pop(0)
        if isinstance(script, Exception):
            raise script

        for event in script:
            yield event


def _text(*parts: str) -> list[ModelEvent]:
    return [TextDelta(p) for p in parts] + [Usage(812, 1_000_000)]


def _run(model: ScriptedModel, registry: dict[str, ToolFn], tier: TierConfig = FAST,
         stats: AgentStats | None = None) -> list[str]:
    messages: list[ChatMessage] = [UserMessage(role="user", content="hi")]
    tools = tuple(SCHEMAS[n] for n in registry)

    async def go() -> list[str]:
        return [s async for s in sentences(model, tier, messages, tools, registry, stats or AgentStats())]

    return asyncio.run(go())


def _reader(text: str, calls: list[dict[str, JsonValue]]) -> ToolFn:
    async def read(args: dict[str, JsonValue]) -> Outcome:
        calls.append(args)
        return Outcome(text)

    return read


def test_plain_answer_streams_sentence_by_sentence_in_one_round() -> None:
    model, stats = ScriptedModel([_text("Crows have three ", "hearts. And blue ", "blood.")]), AgentStats()
    assert _run(model, {}, stats=stats) == ["Crows have three hearts.", "And blue blood."]
    assert stats.rounds == 1 and stats.ttft is not None and stats.prompt_tokens == 812
    assert stats.cost_ticks == 1_000_000 and len(model.requests) == 1
    assert "tools" not in model.requests[0].body()


def test_a_read_goes_back_for_one_more_round_which_cannot_call_tools() -> None:
    calls: list[dict[str, JsonValue]] = []
    model = ScriptedModel([[TextDelta("Let me check that."), ToolCall("c1", "calendar", '{"day_offset": 3}')],
                           _text("You're free on Friday.")])
    said = _run(model, {"calendar": _reader("Calendar Fri: nothing scheduled", calls)})
    assert said == ["You're free on Friday."] and calls == [{"day_offset": 3}]     # the narration is dropped
    first, last = (r.body() for r in model.requests)
    assert first.get("tool_choice") == "auto" and last.get("tool_choice") == "none"
    assert last["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "Calendar Fri: nothing scheduled"}


def test_an_action_ends_on_its_own_line_without_a_second_round() -> None:
    async def lights(args: dict[str, JsonValue]) -> Outcome:
        return Outcome("done: living on", say="Lamp's on.")

    model = ScriptedModel([[ToolCall("c1", "lights", '{"light": "living", "power": "on"}')]])
    assert _run(model, {"lights": lights}) == ["Lamp's on."] and len(model.requests) == 1


def test_fast_round_cap_is_two() -> None:
    loop = [ToolCall("c", "calendar", "{}")]
    model = ScriptedModel([list(loop), list(loop), list(loop)])
    with pytest.raises(Escalate, match="too many tool rounds") as e:
        _run(model, {"calendar": _reader("x", [])})
    assert len(model.requests) == FAST.max_rounds == 2 and e.value.facts == ["calendar: x"]


def test_deep_round_cap_is_three_with_reasoning_low() -> None:
    loop = [ToolCall("c", "web_search", '{"query": "q"}')]
    model = ScriptedModel([list(loop), list(loop), _text("Here's the headline.")])
    assert _run(model, {"web_search": _reader("r", [])}, DEEP) == ["Here's the headline."]
    bodies = [r.body() for r in model.requests]
    assert [b.get("tool_choice") for b in bodies] == ["auto", "auto", "none"]
    assert all(b.get("reasoning_effort") == "low" and b["model"] == "grok-4.6" for b in bodies)
    assert "reasoning_effort" not in ModelRequest(FAST, (), ()).body()


def test_a_tool_outside_the_registry_is_never_run() -> None:
    model = ScriptedModel([[ToolCall("c1", "send_text", '{"to": "Mom", "message": "hi"}')],
                           _text("I can't text from here.")])
    assert _run(model, {}) == ["I can't text from here."]
    assert "isn't available for this ask" in str(model.requests[1].messages[-1]["content"])


def test_escalate_and_guessing_hand_up_with_the_facts() -> None:
    model = ScriptedModel([[ToolCall("c1", "calendar", "{}")],
                           [ToolCall("c2", "escalate", '{"reason": "several steps"}')]])
    with pytest.raises(Escalate, match="several steps") as e:
        _run(model, {"calendar": _reader("Mon: work", [])}, DEEP)
    assert e.value.facts == ["calendar: Mon: work"]
    guess = ScriptedModel([_text("As of my last update, the latest version was 2024.1. ", "Check the site.")])
    with pytest.raises(Escalate, match="no live data"):
        _run(guess, {})


def test_an_empty_answer_or_a_dead_model_is_unavailable() -> None:
    with pytest.raises(ModelUnavailable):
        _run(ScriptedModel([[Usage(10, 0)]]), {})
    with pytest.raises(ModelUnavailable, match="HTTP 502"):
        _run(ScriptedModel([ModelUnavailable("HTTP 502")]), {})


def test_preamble_is_dropped_and_at_most_four_sentences_are_spoken() -> None:
    lines = ["Crows have three hearts and blue blood.", "Their arms can taste what they touch.",
             "Some species change colour in a blink.", "They solve puzzles to reach food.",
             "A fifth line never makes it out."]
    model = ScriptedModel([_text("Let me look that up for you. ", *(f"{s} " for s in lines))])
    assert _run(model, {}) == lines[:4]


# ---------- billing guard and the proxy client
@pytest.mark.parametrize("base", ["https://api.x.ai/v1", "http://api.x.ai/v1", "https://127.0.0.1:8000/v1",
                                  "http://bridge.example.invalid/v1", "http://evil.example/v1", ""])
def test_billing_guard_refuses_anything_but_the_local_proxy(base: str) -> None:
    async def go() -> None:
        async with ClientSession() as http:
            ProxyClient(http, base)

    with pytest.raises(ValueError):
        asyncio.run(go())


def test_no_harness_module_can_reach_the_api_key() -> None:
    assert "key" not in inspect.signature(ProxyClient).parameters
    sources = (
        BRIDGE / "harness" / "pod_agent.py",
        BRIDGE / "harness" / "__init__.py",
        BRIDGE / "harness" / "toolsets.py",
        BRIDGE / "tools" / "quick_search.py",
        BRIDGE / "tools" / "grokipedia.py",
        BRIDGE / "memory" / "memory_write.py",
        BRIDGE / "tools" / "reminders.py",
    )
    for path in sources:
        code = path.read_text()
        assert "XAI_API_KEY" not in code and "api.x.ai" not in code, path.name


SSE = [{"choices": [{"delta": {"content": "Your calendar "}}]},
       {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                                               "function": {"name": "cal", "arguments": '{"day'}}]}}]},
       {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '_offset": 1}'}}]}}]},
       {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 12, "cost_in_usd_ticks": 2_000_000}}]


class FakeProxy:
    def __init__(self, status: int = 200, body: str = "") -> None:
        self.status, self.body = status, body
        self.seen: list[tuple[str, dict[str, object]]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.seen.append((request.headers.get("Authorization", ""), await request.json()))
        if self.status != 200:
            return web.Response(status=self.status, text=self.body)

        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for chunk in SSE:
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")

        return resp


async def _proxy_round(fake: FakeProxy) -> list[ModelEvent]:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        async with ClientSession() as http:
            client = ProxyClient(http, f"http://127.0.0.1:{runner.addresses[0][1]}/v1")
            return [e async for e in client.stream(ModelRequest(DEEP, (), ()))]
    finally:
        await runner.cleanup()


def test_proxy_client_parses_text_tool_fragments_and_usage_with_a_dummy_bearer() -> None:
    fake = FakeProxy()
    events = asyncio.run(_proxy_round(fake))
    assert events == [TextDelta("Your calendar "), Usage(900, 2_000_000),
                      ToolCall("call_1", "cal", '{"day_offset": 1}')]
    assert fake.seen[0][0] == f"Bearer {DUMMY_BEARER}" and fake.seen[0][1]["reasoning_effort"] == "low"


@pytest.mark.parametrize("status, body, line, pool", [
    (403, "You have run out of credits or need a Grok subscription", LIMIT_LINE, True),
    (429, "weekly usage limit reached", LIMIT_LINE, True),
    (429, "rate limited", BUSY_LINE, False),
])
def test_limits_are_spoken_once_with_no_retry(status: int, body: str, line: str, pool: bool) -> None:
    fake = FakeProxy(status, body)
    with pytest.raises(LimitReached) as e:
        asyncio.run(_proxy_round(fake))
    assert (e.value.line, e.value.pool) == (line, pool) and len(fake.seen) == 1


def test_a_server_error_or_a_dead_proxy_is_unavailable() -> None:
    with pytest.raises(ModelUnavailable, match="HTTP 500"):
        asyncio.run(_proxy_round(FakeProxy(500, "boom")))

    async def down() -> None:
        async with ClientSession() as http:
            async for _ in ProxyClient(http, "http://127.0.0.1:9/v1", connect_s=0.5).stream(ModelRequest(FAST, (), ())):
                pass

    with pytest.raises(ModelUnavailable, match="connect"):
        asyncio.run(down())
