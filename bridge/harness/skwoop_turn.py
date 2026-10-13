"""Desk-pod AGENT turns → Deep skwoop-voice webhook.

Separate ingress from Siri and iMessage. HMAC lives in env; never log it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from lean_http import ClientError, ClientSession, ClientTimeout

log = logging.getLogger("skwoop.skwoop")

WEBHOOK_URL = os.environ.get(
    "SKWOOP_WEBHOOK_URL", ""
)
STATE_DB = Path(os.environ.get("AGENT_STATE_DB", os.path.expanduser("~/.skwoop/agent/state.db")))
SESSION_DIR = Path(os.environ.get("SKWOOP_SESSION_DIR", os.path.expanduser("~/.skwoop/sessions")))
SESSION_IDLE_S = float(os.environ.get("SKWOOP_SESSION_IDLE_S", "3600"))
WAIT_S = float(os.environ.get("SKWOOP_WEBHOOK_WAIT_S", "180"))
POLL_S = 0.35
HISTORY_TURNS = int(os.environ.get("SKWOOP_SESSION_TURNS", "6"))


def _secret() -> str:
    return (os.environ.get("SKWOOP_WEBHOOK_SECRET") or "").strip()


def _session_path(room: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (room or "pod"))[:40]
    return SESSION_DIR / f"{safe}.json"


def load_session(room: str, now: float | None = None) -> dict[str, Any]:
    """Return the pod session, rotating after SKWOOP_SESSION_IDLE_S idle."""
    now = time.time() if now is None else now
    path = _session_path(room)
    data = {"started_at": now, "last_at": 0.0, "turns": [], "rotated": False}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                data.update(raw)
                data["rotated"] = False
        except Exception:
            data = {"started_at": now, "last_at": 0.0, "turns": [], "rotated": True}
    last = float(cast(Any, data.get("last_at") or 0.0))
    started = float(cast(Any, data.get("started_at") or 0.0))
    idle_from = last or started
    if idle_from and (now - idle_from) > SESSION_IDLE_S:
        data = {"started_at": now, "last_at": 0.0, "turns": [], "rotated": True}
        log.info("pod session rotated room=%s idle=%.0fs", room, now - idle_from)
    data.setdefault("turns", [])
    return data


def save_session(room: str, data: dict[str, Any]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = _session_path(room)
    out = {
        "started_at": data.get("started_at"),
        "last_at": data.get("last_at"),
        "turns": list(data.get("turns") or [])[-HISTORY_TURNS:],
    }
    path.write_text(json.dumps(out))


def context_blob(data: dict[str, Any]) -> str:
    if data.get("rotated") or not data.get("turns"):
        return "(new pod session)"
    lines = []
    for t in data["turns"][-HISTORY_TURNS:]:
        heard = str(t.get("heard") or "").strip()[:240]
        reply = str(t.get("reply") or "").strip()[:240]
        if heard:
            lines.append(f"User: {heard}")
        if reply:
            lines.append(f"Skwoop: {reply}")
    return "\n".join(lines) if lines else "(new pod session)"


def facts_blob(facts: list[str] | None) -> str:
    if not facts:
        return ""
    return "\nAlready looked up on the pod (fresh, use them):\n" + "\n".join(f"- {f[:400]}" for f in facts[:6])


def _poll_reply(delivery_id: str, deadline: float) -> str:
    chat_id = f"webhook:skwoop-voice:{delivery_id}"
    uri = f"file:{STATE_DB}?mode=ro"
    last = ""
    while time.time() < deadline:
        try:
            con = sqlite3.connect(uri, timeout=1)
            row = con.execute(
                "SELECT id, ended_at FROM sessions "
                "WHERE chat_id=? ORDER BY started_at DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
            if row:
                sid, ended = row
                msg = con.execute(
                    "SELECT content FROM messages "
                    "WHERE session_id=? AND role='assistant' AND active=1 "
                    "AND content IS NOT NULL AND TRIM(content) != '' "
                    "AND (tool_calls IS NULL OR tool_calls='' OR tool_calls='[]') "
                    "ORDER BY id DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                if msg and msg[0]:
                    last = str(msg[0]).strip()
                    if ended is not None:
                        con.close()
                        return last
            con.close()
        except Exception as e:
            log.error("poll error: %s", type(e).__name__)
        time.sleep(POLL_S)
    return last


async def ask(http: ClientSession | None, heard: str, room: str = "pod",
              facts: list[str] | None = None) -> str:
    """One skwoop-voice turn. `facts`: what the fast tier already looked up before it handed the turn over."""
    if http is None:
        return "Couldn't reach Skwoop."
    secret = _secret()
    if not secret or not WEBHOOK_URL:
        log.error("SKWOOP_WEBHOOK_SECRET or SKWOOP_WEBHOOK_URL missing")
        return "Couldn't reach Skwoop."
    text = heard.strip()[:500]
    sess = load_session(room)
    payload_obj = {
        "command": text,
        "source": "pod",
        "room": room,
        "context": context_blob(sess) + facts_blob(facts),
    }
    payload_json = json.dumps(payload_obj, separators=(",", ":"))
    sig = hmac.new(secret.encode(), payload_json.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={sig}",
    }
    try:
        async with http.post(
            WEBHOOK_URL,
            data=payload_json,
            headers=headers,
            timeout=ClientTimeout(total=15),
        ) as r:
            raw = await r.text()
            if r.status >= 400:
                log.error("skwoop-voice HTTP %s", r.status)
                return "Couldn't reach Skwoop."
            data = json.loads(raw) if raw else {}
    except Exception as e:
        log.error("skwoop-voice failed: %s", type(e).__name__)
        return "Couldn't reach Skwoop."
    delivery_id = str(data.get("delivery_id") or "")
    if not delivery_id:
        log.error("skwoop-voice missing delivery_id")
        return "Couldn't reach Skwoop."
    reply = await asyncio.to_thread(_poll_reply, delivery_id, time.time() + WAIT_S)
    reply = (reply or "").strip()
    now = time.time()
    turns = list(sess.get("turns") or [])
    turns.append({"heard": text, "reply": reply})
    save_session(
        room,
        {
            "started_at": sess.get("started_at") or now,
            "last_at": now,
            "turns": turns[-HISTORY_TURNS:],
        },
    )
    log.info("skwoop reply_len=%s room=%s rotated=%s", len(reply), room, bool(sess.get("rotated")))
    return reply or "That took too long."


async def ask_stream(http: ClientSession | None, heard: str, room: str = "pod",
                     facts: list[str] | None = None) -> AsyncIterator[str]:
    """The same turn as `ask`, streamed from Deep's api_server: yields sentences to speak as they arrive.
    Raises deep_model.StreamUnavailable before the first sentence when the stream can't be used."""
    import harness.deep_model as deep_model

    if http is None or not deep_model.enabled():
        raise deep_model.StreamUnavailable("off")
    text = heard.strip()[:500]
    sess = load_session(room)
    prompt = deep_model.render_prompt(text, context_blob(sess) + facts_blob(facts))
    speech = deep_model.SpeechFilter()
    tools = 0
    try:
        async for kind, value in deep_model.events(http, prompt):
            if kind == "tool":
                tools += 1
                speech.tool()
                continue
            for sentence in speech.feed(value):
                yield sentence
            if speech.restarted:
                log.info("deep stream: answer started over; disconnecting")
                break
        for sentence in speech.finish():
            yield sentence
    except deep_model.StreamUnavailable:
        raise
    except (ClientError, TimeoutError) as e:
        if not speech.spoken:
            raise deep_model.StreamUnavailable(f"stream: {type(e).__name__}") from None
        log.warning("deep stream cut after %d sentence(s): %s", len(speech.spoken), type(e).__name__)
    finally:
        reply = " ".join(speech.spoken)
        if reply:
            now = time.time()
            turns = [*list(sess.get("turns") or []), {"heard": text, "reply": reply}]
            save_session(room, {"started_at": sess.get("started_at") or now, "last_at": now,
                                "turns": turns[-HISTORY_TURNS:]})
        log.info("skwoop stream reply_len=%s sentences=%d dropped=%d tools=%d room=%s", len(reply),
                 len(speech.spoken), len(speech.dropped), tools, room)
    if not speech.spoken:
        raise deep_model.StreamUnavailable("stream ended with nothing to say")
