"""Interim STT results (Oct 4): they are counted, logged and handed to on_interim, and never reach the transcript or
end the turn. Finals behave exactly as with interim_results=false."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
from lean_http import ClientSession
from lean_http import web
from lean_ws import WSMsgType

import providers

PORT = 18183


class LocalStt(providers.XaiSttStream):
    def _url(self) -> str:
        return f"ws://127.0.0.1:{PORT}/stt"


def partial(text: str, is_final: bool, speech_final: bool = False) -> str:
    return json.dumps({"type": "transcript.partial", "text": text, "is_final": is_final, "speech_final": speech_final})


def run_script(events_before: list[str], on_finalize: list[str]) -> tuple[str, list[str], bool, int]:
    """Server sends events_before after the first audio, then on_finalize after a finalize message."""

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"type": "transcript.created"}))
        sent = False
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and not sent:
                sent = True
                for e in events_before:
                    await ws.send_str(e)
            elif msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "finalize":
                for e in on_finalize:
                    await ws.send_str(e)
        return ws

    async def go() -> tuple[str, list[str], bool, int]:
        app = web.Application()
        app.router.add_get("/stt", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        try:
            async with ClientSession() as http:
                stream = LocalStt(http, 16000)
                seen: list[str] = []
                stream.on_interim = seen.append
                await stream.start()
                await stream.push(b"\x00\x00" * 1600)
                await asyncio.sleep(0.2)
                done_early = stream.finished
                await stream.finalize()
                text = await stream.wait(timeout=5)
                await stream.close()
                await asyncio.gather(*providers._closing)
                return text, seen, done_early, stream.interims
        finally:
            await runner.cleanup()

    return asyncio.run(go())


def test_interims_never_become_the_transcript_or_end_the_turn():
    text, seen, done_early, n = run_script(
        [partial("what's", False), partial("what's the wet", False), partial("what's the", True)],
        [partial("what's the wea", False), json.dumps({"type": "transcript.done", "text": ""})])
    assert text == "what's the"                       # the chunk-final, not the later interim
    assert seen == ["what's", "what's the wet", "what's the wea"]
    assert not done_early and n == 3


def test_utterance_final_still_wins():
    text, seen, _, n = run_script([partial("set a tim", False)],
                                  [partial("set a timer for ten minutes", True, True)])
    assert text == "set a timer for ten minutes" and seen == ["set a tim"] and n == 1


def test_a_broken_interim_handler_cannot_break_the_turn(monkeypatch: pytest.MonkeyPatch):
    def boom(_: str) -> None:
        raise RuntimeError("face mirror failed")

    monkeypatch.setattr(providers.XaiSttStream, "on_interim", boom, raising=False)
    s = providers.XaiSttStream.__new__(providers.XaiSttStream)
    s.on_interim, s.interims, s.first_interim_s, s._t0, s.interim_text = boom, 0, None, None, ""
    s._interim({"type": "transcript.partial", "text": "hi", "is_final": False})
    assert s.interims == 1


def test_interim_classification():
    assert providers.stt_is_interim({"type": "transcript.partial", "is_final": False, "speech_final": False})
    assert not providers.stt_is_interim({"type": "transcript.partial", "is_final": True, "speech_final": False})
    assert not providers.stt_is_interim({"type": "transcript.partial", "text": "x"})   # no is_final: old shape
    assert not providers.stt_is_interim({"type": "transcript.done", "is_final": False})


def test_switch(monkeypatch: pytest.MonkeyPatch):
    def q() -> str:
        return parse_qs(urlparse(providers.XaiSttStream(None, 16000)._url()).query)["interim_results"][0]  # type: ignore[arg-type]

    monkeypatch.delenv("STT_INTERIM", raising=False)
    assert q() == "true"
    monkeypatch.setenv("STT_INTERIM", "0")
    assert q() == "false"
