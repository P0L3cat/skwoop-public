"""Recording stand-in for the bridge /xiaozhi/v1/ socket. Speaks the pod frames the sidecar relays."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from lean_http import Request, WebSocketResponse
from lean_ws import WSMsgType


@dataclass
class FakeBridge:
    headers: dict[str, str] = field(default_factory=dict)
    texts: list[dict[str, object]] = field(default_factory=list)
    frames: list[bytes] = field(default_factory=list)

    async def handle(self, request: Request) -> WebSocketResponse:
        self.headers = dict(request.headers)
        ws = WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.BINARY and isinstance(msg.data, bytes):
                self.frames.append(msg.data)

                continue

            if msg.type != WSMsgType.TEXT or not isinstance(msg.data, str):
                break

            raw = json.loads(msg.data)
            if not isinstance(raw, dict):
                break

            obj = {str(k): v for k, v in raw.items()}
            self.texts.append(obj)
            if obj.get("type") == "hello":
                await ws.send_str(json.dumps({"type": "hello", "transport": "websocket"}))
            elif obj.get("state") == "stop":
                for frame in ({"type": "stt", "text": "hi"}, {"type": "mcp", "payload": {}},
                              {"type": "tts", "state": "start"}):
                    await ws.send_str(json.dumps(frame))

                await ws.send_bytes(b"opus-frame")
                await ws.send_str(json.dumps({"type": "tts", "state": "stop"}))

        return ws
