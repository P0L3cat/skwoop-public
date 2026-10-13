"""The xAI STT socket answers the close handshake ~2 s late; the turn must not wait for it (it did, on every turn)."""
from __future__ import annotations

import asyncio
import json
import time

from lean_http import ClientSession
from lean_http import web
from lean_ws import WSMsgType

import providers

PORT = 18181


class LocalStt(providers.XaiSttStream):
    def _url(self) -> str:
        return f"ws://127.0.0.1:{PORT}/stt"


async def slow_closing_stt(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(autoclose=False)
    await ws.prepare(request)
    await ws.send_str(json.dumps({"type": "transcript.created"}))
    while True:
        msg = await ws.receive()
        if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "finalize":
            await ws.send_str(json.dumps({"type": "transcript.done", "text": "what's the weather"}))
        elif msg.type != WSMsgType.BINARY:
            await asyncio.sleep(2.0)                    # xAI is this slow to answer the close handshake
            await ws.close()

            return ws


def test_closing_the_stt_socket_does_not_hold_up_the_turn():
    async def go() -> tuple[str, float, bool]:
        app = web.Application()
        app.router.add_get("/stt", slow_closing_stt)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        try:
            async with ClientSession() as http:
                stream = LocalStt(http, 16000)
                await stream.start()
                await stream.push(b"\x00\x00" * 1600)
                await stream.finalize()
                text = await stream.wait(timeout=5)
                t0 = time.monotonic()
                await stream.close()
                took = time.monotonic() - t0
                pending = bool(providers._closing)
                await asyncio.gather(*providers._closing)      # the background close still completes

                return text, took, pending
        finally:
            await runner.cleanup()

    text, took, pending = asyncio.run(go())
    assert text == "what's the weather"
    assert took < 0.1 and pending
