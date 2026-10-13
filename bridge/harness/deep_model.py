"""Deep turns streamed from Deep's api_server (OpenAI-compatible SSE), so the pod speaks Deep's first sentence
as soon as it arrives instead of after the whole turn.

The prompt is the skwoop-voice webhook route's own template (~/.skwoop/agent/webhook_subscriptions.json), so the two
paths ask Deep the same thing. The api_server streams the text of every model call in a turn into one stream,
so SpeechFilter keeps only what reads as the answer: no "I'll look that up" preamble, nothing typed before a tool
call, at most MAX_SENTENCES. A sentence that repeats one already spoken means Deep started over (its
continuation nudge after a preamble makes grok write the answer twice): the filter stops there and the client
disconnects, which also ends Deep's redundant second model call.

Off unless DEEP_MODEL_BASE_URL and DEEP_MODEL_API_KEY are set; OFF_FILE turns it off live. Any failure before the
first sentence raises StreamUnavailable and the bridge falls back to the webhook path.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lean_http import ClientError, ClientSession, ClientTimeout

from providers import _sentence_end, clean_for_speech

log = logging.getLogger("skwoop.deep_model")

ROUTES_FILE = Path(os.environ.get(
    "DEEP_MODEL_PROMPT_FILE", os.path.expanduser("~/.skwoop/agent/webhook_subscriptions.json")))
ROUTE = os.environ.get("SKWOOP_DEEP_ROUTE", "skwoop-voice")
MAX_SENTENCES = int(os.environ.get("SKWOOP_DEEP_MAX_SENTENCES", "4"))
IDLE_S = float(os.environ.get("SKWOOP_DEEP_IDLE_S", "90"))      # longest silence between stream events
TOTAL_S = float(os.environ.get("SKWOOP_WEBHOOK_WAIT_S", "180"))
OFF_FILE = Path(os.environ.get("SKWOOP_DEEP_STREAM_OFF_FILE", os.path.expanduser("~/.skwoop/deep-stream.off")))
# Per-request model and reasoning for pod turns (Deep api_server model_options), read on every turn so it can be
# switched or removed live. One line: "<effort>" or "<model> <effort>", e.g. "low" or "grok-4.7 low". No file: the
# Deep config's own model and reasoning_effort (medium).
REASONING_FILE = Path(os.environ.get("SKWOOP_DEEP_REASONING_FILE",
                                     os.path.expanduser("~/.skwoop/deep-reasoning")))
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,63}$")

_ANNOUNCE = re.compile(
    r"^(ok(ay)?,? |sure,? |alright,? )?(i'?ll|i will|let me|i'?m going to|i am going to|i'?m|i am|going to|checking|"
    r"looking|one (sec|second|moment)|give me a (sec|second|moment))\b.*\b(look|check|search|find|pull|read|grab|"
    r"get|open|see|dig|fetch|scan|go through|summari[sz]e)", re.IGNORECASE)


class StreamUnavailable(RuntimeError):
    """Nothing was spoken and the stream can't be used: take the webhook path."""


def configured() -> bool:
    return bool(os.environ.get("DEEP_MODEL_BASE_URL") and os.environ.get("DEEP_MODEL_API_KEY"))


def enabled() -> bool:
    return configured() and not OFF_FILE.exists()


@dataclass(frozen=True)
class TurnModel:
    model: str | None          # None: Deep' configured model
    effort: str


def parse_turn_model(line: str) -> TurnModel | None:
    """'low' / 'grok-4.7 low' -> TurnModel; anything else -> None (and the config default is used)."""
    parts = line.split()
    if len(parts) == 1 and parts[0] in EFFORTS:
        return TurnModel(None, parts[0])

    if len(parts) == 2 and _MODEL_ID.match(parts[0]) and parts[1] in EFFORTS:
        return TurnModel(parts[0], parts[1])

    return None


def turn_model(path: Path = REASONING_FILE) -> TurnModel | None:
    try:
        line = path.read_text().strip()
    except OSError:
        return None

    picked = parse_turn_model(line)
    if picked is None and line:
        log.warning("%s: %r is not '<effort>' or '<model> <effort>'; using the Deep config", path, line)

    return picked


def request_body(prompt: str, picked: TurnModel | None) -> dict[str, object]:
    body: dict[str, object] = {"model": os.environ.get("DEEP_MODEL_NAME", "deep"), "stream": True,
                               "messages": [{"role": "user", "content": prompt}]}
    if picked is not None:
        body["model_options"] = {"reasoning": {"enabled": picked.effort != "none", "effort": picked.effort}}
        if picked.model is not None:
            body["model"] = picked.model
            provider = os.environ.get("DEEP_MODEL_PROVIDER", "").strip()
            if provider:
                body["provider"] = provider

    return body


def render_prompt(command: str, context: str) -> str:
    try:
        loaded: object = json.loads(ROUTES_FILE.read_text())
        if not isinstance(loaded, dict):
            raise TypeError("routes")
        route = loaded[ROUTE]
        if not isinstance(route, dict):
            raise TypeError("route")
        template = route["prompt"]
        if not isinstance(template, str):
            raise TypeError("prompt")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise StreamUnavailable(f"no {ROUTE} prompt ({type(e).__name__})") from None
    text = template.replace("{command}", command).replace("{context}", context)
    if re.search(r"\{[a-z_][a-z0-9_.]*\}", template.replace("{command}", "").replace("{context}", "")):
        raise StreamUnavailable("route prompt has placeholders the bridge doesn't fill")
    return text


_CONTRACTIONS = ((r"n't\b", " not"), (r"'re\b", " are"), (r"'ll\b", " will"), (r"'ve\b", " have"),
                 (r"'m\b", " am"), (r"'d\b", " would"), (r"\b(it|that|there|what|here|he|she)'s\b", r"\1 is"))
_JOINED = re.compile(r"([a-z][.!?])(?=[A-Z])")    # two model outputs run together: "notes.Latest stable is"


def _words(s: str) -> set[str]:
    s = s.lower().replace("\u2019", "'")
    for pat, rep in _CONTRACTIONS:
        s = re.sub(pat, rep, s)
    return set(re.findall(r"[a-z0-9]+", s))


class SpeechFilter:
    """Text deltas in, sentences to speak out."""

    def __init__(self, max_sentences: int = MAX_SENTENCES):
        self.buf = ""
        self.spoken: list[str] = []
        self.dropped: list[str] = []
        self.max = max_sentences
        self.restarted = False

    def _keep(self, sentence: str) -> str | None:
        s = clean_for_speech(sentence)
        if len(s) < 2 or not re.search(r"[A-Za-z0-9]", s):
            return None
        if not self.spoken and _ANNOUNCE.search(s) and len(s) < 140:
            self.dropped.append(s)
            return None
        w = _words(s)
        for prev in self.spoken:
            p = _words(prev)
            if w and p and len(w & p) / len(w | p) >= 0.5:
                self.dropped.append(s)
                self.restarted = True
                return None
        if len(self.spoken) >= self.max:
            self.dropped.append(s)
            return None
        self.spoken.append(s)
        return s

    def feed(self, delta: str) -> list[str]:
        if self.restarted:
            return []
        self.buf = _JOINED.sub(r"\1 ", self.buf + delta)
        out = []
        while not self.restarted and (end := _sentence_end(self.buf)) is not None:
            sentence, self.buf = self.buf[:end], self.buf[end:]
            if (s := self._keep(sentence)) is not None:
                out.append(s)
        return out

    def tool(self) -> None:
        """A tool call started: text still in the buffer was the model narrating, not the answer."""
        if self.buf.strip():
            self.dropped.append(self.buf.strip())
        self.buf = ""

    def finish(self) -> list[str]:
        rest, self.buf = self.buf, ""
        s = self._keep(rest) if rest.strip() and not self.restarted else None
        return [s] if s else []


async def events(http: ClientSession, prompt: str) -> AsyncIterator[tuple[str, str]]:
    """("text", delta) / ("tool", name) from POST /v1/chat/completions stream=true."""
    url = os.environ["DEEP_MODEL_BASE_URL"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = request_body(prompt, turn_model())
    headers = {"Authorization": f"Bearer {os.environ['DEEP_MODEL_API_KEY']}", "Accept": "text/event-stream"}
    timeout = ClientTimeout(total=TOTAL_S, sock_read=IDLE_S, connect=5)
    try:
        resp = await http.post(url, json=body, headers=headers, timeout=timeout)
    except (ClientError, TimeoutError, OSError) as e:
        raise StreamUnavailable(f"connect: {type(e).__name__}") from None
    async with resp:
        if resp.status != 200:
            raise StreamUnavailable(f"HTTP {resp.status}")
        event = ""
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                event = ""
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if event == "deep.tool.progress" or obj.get("object") == "deep.tool.progress":
                yield "tool", str(obj.get("tool") or obj.get("name") or "")
                continue
            for choice in obj.get("choices") or []:
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    yield "text", delta


class DeepModel(Protocol):
    """A pluggable deep model. The bridge ships one OpenAI-compatible HTTP client."""

    def configured(self) -> bool: ...

    def events(self, http: ClientSession, prompt: str) -> AsyncIterator[tuple[str, str]]: ...


class OpenAICompatibleDeepModel:
    """POST {DEEP_MODEL_BASE_URL}/chat/completions with DEEP_MODEL_API_KEY and DEEP_MODEL_NAME."""

    def configured(self) -> bool:
        return configured()

    def events(self, http: ClientSession, prompt: str) -> AsyncIterator[tuple[str, str]]:
        return events(http, prompt)
