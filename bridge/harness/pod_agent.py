"""The pod model loop: one streamed tool loop for both tiers, through the configured loopback deep-model URL
(OpenAI-compatible). The bearer is a placeholder the endpoint replaces, and there
is no parameter for an API key, so no harness model call can use an API key.

Fast = grok-4.20 non-reasoning, at most 2 rounds; deep = grok-4.6 at reasoning effort low, at most 3. The last round
is sent with tool_choice "none". Text streams out sentence by sentence (deep_model.SpeechFilter: no "let me look"
preamble, at most 4 sentences). Tools run from an explicit registry; a call outside it is answered "not available".
Actions end the turn on their own spoken line.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, NotRequired, Protocol, TypedDict
from urllib.parse import urlsplit

from lean_http import ClientError, ClientSession, ClientTimeout

from harness.deep_model import SpeechFilter
from tools.house_tools import Escalate, Outcome
from providers import split_sentences
from tools.quick_search import DUMMY_BEARER, PROXY_BASE
from harness.toolsets import JsonObject, JsonValue

log = logging.getLogger("skwoop.harness")

LIMIT_LINE = "I'm out of Grok for now, so I can only do the basics."
BUSY_LINE = "Grok is busy for a moment. Try me again in a minute."
_WEEKLY = re.compile(r"(out of credits|grok subscription|weekly|usage limit|spending limit|exhausted)", re.IGNORECASE)
# The model talking about its training instead of using a tool: a guess is on its way, so hand it up instead.
GUESSING = re.compile(
    r"\b(as of my (last )?(update|knowledge|training)|my (training|knowledge) (data|cut-?off)|"
    r"i (don'?t|do not) have (access to )?(real-?time|live|current|up-to-date) (data|info|information|access|updates)"
    r"|i (can'?t|cannot) (browse|access the internet))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TierConfig:
    name: Literal["fast", "deep"]
    model: str
    max_rounds: int
    reasoning_effort: str | None
    max_tokens: int


FAST = TierConfig("fast", "grok-4.20-0309-non-reasoning", 2, None, 300)
DEEP = TierConfig("deep", "grok-4.6", 3, "low", 1500)  # max_tokens covers the reasoning tokens too


# ---------- chat messages (the request body)
class ToolCallMessage(TypedDict):
    id: str
    type: Literal["function"]
    function: dict[str, str]  # name, arguments


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: str | None
    tool_calls: NotRequired[list[ToolCallMessage]]


class ToolMessage(TypedDict):
    role: Literal["tool"]
    tool_call_id: str
    content: str


ChatMessage = SystemMessage | UserMessage | AssistantMessage | ToolMessage


class ChatRequestBody(TypedDict):
    model: str
    messages: list[ChatMessage]
    stream: bool
    stream_options: dict[str, bool]
    temperature: float
    max_tokens: int
    tools: NotRequired[list[JsonObject]]
    tool_choice: NotRequired[Literal["auto", "none"]]
    reasoning_effort: NotRequired[str]


@dataclass(frozen=True)
class ModelRequest:
    tier: TierConfig
    messages: tuple[ChatMessage, ...]
    tools: tuple[JsonObject, ...]
    last: bool = False  # last round: the schemas stay (prompt cache) but tool_choice is "none"

    def body(self) -> ChatRequestBody:
        body = ChatRequestBody(model=self.tier.model, messages=list(self.messages), stream=True,
                               stream_options={"include_usage": True}, temperature=0.3,
                               max_tokens=self.tier.max_tokens)
        if self.tools:
            body["tools"] = list(self.tools)
            body["tool_choice"] = "none" if self.last else "auto"
        if self.tier.reasoning_effort is not None:
            body["reasoning_effort"] = self.tier.reasoning_effort

        return body


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    cost_ticks: int


ModelEvent = TextDelta | ToolCall | Usage


class ModelClient(Protocol):
    def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class ModelUnavailable(RuntimeError):
    """The proxy or the model failed (HTTP error, timeout, broken stream, empty answer)."""


class LimitReached(RuntimeError):
    """The subscription said no (403, or 429). `line` is what the pod says; `pool` raises the status alert."""

    def __init__(self, line: str, pool: bool, detail: str) -> None:
        super().__init__(detail)
        self.line, self.pool = line, pool


def limit_from(status: int, body: str) -> LimitReached | None:
    detail = f"HTTP {status}: {' '.join(body.split())[:160]}"
    if status == 403 or (status == 429 and _WEEKLY.search(body)):
        return LimitReached(LIMIT_LINE, True, detail)

    return LimitReached(BUSY_LINE, False, detail) if status == 429 else None


def _int(raw: object) -> int:
    return int(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0


def _str(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


def parse_chunk(raw: object, calls: dict[int, list[str]]) -> list[TextDelta | Usage]:
    """One streamed chat.completion.chunk (the boundary). Text and usage come out now; tool-call fragments collect
    in `calls` (index -> [id, name, arguments]) and are emitted when the stream ends."""
    if not isinstance(raw, dict):
        return []

    out: list[TextDelta | Usage] = []
    if isinstance(usage := raw.get("usage"), dict):
        out.append(Usage(_int(usage.get("prompt_tokens")), _int(usage.get("cost_in_usd_ticks"))))
    choices = raw.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            continue

        if text := _str(delta.get("content")):
            out.append(TextDelta(text))
        frags = delta.get("tool_calls")
        for tc in frags if isinstance(frags, list) else []:
            if not isinstance(tc, dict):
                continue

            slot = calls.setdefault(_int(tc.get("index")) if "index" in tc else len(calls), ["", "", ""])
            fn = tc.get("function")
            fn = fn if isinstance(fn, dict) else {}
            slot[0] = _str(tc.get("id")) or slot[0]
            slot[1] += _str(fn.get("name"))
            slot[2] += _str(fn.get("arguments"))

    return out


class ProxyClient:
    """POST {base}/chat/completions, stream=true, through the Deep proxy (loopback only: the billing guard)."""

    def __init__(self, http: ClientSession, base: str = PROXY_BASE, connect_s: float = 2.0) -> None:
        if urlsplit(base).scheme != "http" or urlsplit(base).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"harness model calls go only to the local Deep proxy, not {base}")

        self.http, self.base = http, base.rstrip("/")
        self.timeout = ClientTimeout(total=45.0, sock_connect=connect_s, sock_read=12.0)

    async def stream(self, req: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            resp = await self.http.post(f"{self.base}/chat/completions", json=req.body(), timeout=self.timeout,
                                        headers={"Authorization": f"Bearer {DUMMY_BEARER}"})
        except (ClientError, TimeoutError, OSError) as e:
            raise ModelUnavailable(f"connect: {type(e).__name__}") from None

        async with resp:
            if resp.status != 200:
                raise limit_from(resp.status, await resp.text()) or ModelUnavailable(f"HTTP {resp.status}")

            calls: dict[int, list[str]] = {}
            try:
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:") or (payload := line[5:].strip()) == "[DONE]":
                        continue

                    try:
                        chunk: object = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    for event in parse_chunk(chunk, calls):
                        yield event
            except (ClientError, TimeoutError) as e:
                raise ModelUnavailable(f"stream: {type(e).__name__}") from None

            for _, (call_id, name, args) in sorted(calls.items()):
                yield ToolCall(call_id, name, args)


# ---------- the loop
ToolFn = Callable[[dict[str, JsonValue]], Awaitable[Outcome]]


@dataclass
class AgentStats:
    ttft: float | None = None  # seconds from a request to its first token (text or tool call), first round
    first_token_at: float | None = None  # monotonic, for the turn profile
    rounds: int = 0
    prompt_tokens: int = 0  # usage.prompt_tokens of the first round
    cost_ticks: int = 0
    tools: list[tuple[str, float]] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)  # tool results, for the Deep hand-off


def parse_args(raw: str) -> dict[str, JsonValue]:
    """A tool call's arguments JSON (model output: the boundary) -> an object, {} when it isn't one."""
    try:
        value: object = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}

    return value if isinstance(value, dict) else {}


async def _run_calls(calls: list[ToolCall], registry: Mapping[str, ToolFn], messages: list[ChatMessage],
                     stats: AgentStats, clock: Callable[[], float]) -> list[str | None]:
    """One round's tool calls in order, results into `messages`. Returns each tool's spoken line or None."""
    says: list[str | None] = []
    for c in calls:
        t0 = clock()
        args = parse_args(c.arguments)
        if c.name == "escalate":
            reason = args.get("reason")
            raise Escalate(reason if isinstance(reason, str) and reason else "the model asked for Deep", stats.facts)

        fn = registry.get(c.name)
        try:
            out = Outcome(f"error: {c.name} isn't available for this ask") if fn is None else await fn(args)
        except Escalate as e:
            raise Escalate(e.reason, stats.facts) from None

        stats.tools.append((c.name, clock() - t0))
        stats.facts.append(f"{c.name}: {out.text}"[:600])
        messages.append(ToolMessage(role="tool", tool_call_id=c.id, content=out.text))
        says.append(out.say)

    return says


async def sentences(client: ModelClient, tier: TierConfig, messages: list[ChatMessage], tools: tuple[JsonObject, ...],
                    registry: Mapping[str, ToolFn], stats: AgentStats,
                    clock: Callable[[], float] = time.monotonic) -> AsyncIterator[str]:
    """Sentences to speak. Raises Escalate (handed up, guessed, out of rounds), ModelUnavailable or LimitReached."""
    speech = SpeechFilter()
    for n in range(1, tier.max_rounds + 1):
        stats.rounds = n
        calls: list[ToolCall] = []
        content = ""
        t_req = clock()
        async for event in client.stream(ModelRequest(tier, tuple(messages), tools, n == tier.max_rounds)):
            if not isinstance(event, Usage) and stats.ttft is None:
                stats.first_token_at = clock()
                stats.ttft = stats.first_token_at - t_req
            match event:
                case Usage():
                    stats.prompt_tokens = stats.prompt_tokens or event.prompt_tokens
                    stats.cost_ticks += event.cost_ticks
                case TextDelta():
                    content += event.text
                    for sentence in speech.feed(event.text):
                        if len(speech.spoken) == 1 and GUESSING.search(sentence):
                            raise Escalate("the model has no live data for this", stats.facts)
                        yield sentence
                case ToolCall():
                    calls.append(event)

        if not calls:
            for sentence in speech.finish():
                if len(speech.spoken) == 1 and GUESSING.search(sentence):
                    raise Escalate("the model has no live data for this", stats.facts)
                yield sentence
            if not speech.spoken:
                raise ModelUnavailable("the model answered with nothing to say")
            return

        if n == tier.max_rounds:
            raise Escalate("too many tool rounds", stats.facts)

        speech.tool()
        messages.append(AssistantMessage(role="assistant", content=content or None, tool_calls=[
            ToolCallMessage(id=c.id, type="function", function={"name": c.name, "arguments": c.arguments})
            for c in calls]))
        says = await _run_calls(calls, registry, messages, stats, clock)
        log.info("harness tools %s", ", ".join(f"{name} {s:.2f}s" for name, s in stats.tools))
        if all(s is not None for s in says):  # actions only: end on their own lines, no second round
            for s in says:
                for part in split_sentences(s or ""):
                    yield part
            return
