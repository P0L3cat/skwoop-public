"""Skwoop web pod: the pod sim as a web page, served over HTTPS, so any phone, tablet or laptop on
the LAN can stand in for a pod. A small sidecar next to the bridge (its own launchd job; the bridge is untouched
apart from accepting web-pod-* device ids from loopback).

  browser  --wss /pod-->  this sidecar  --ws /xiaozhi/v1/ (Device-Id web-pod-<name>)-->  skwoop_bridge
     mic: 16 kHz 16-bit PCM            ->  Opus 16 kHz / 60 ms frames (like the pod firmware)
     speaker: 24 kHz 16-bit PCM        <-  Opus 24 kHz from the bridge
     JSON: listen start/stop, abort, inbox  ->  as is;   tts / llm / stt / alert  <-  as is

Browsers allow the mic only on HTTPS, so it serves a local-CA certificate (webpod/make_cert.sh); GET /ca.crt hands
out the CA for a one-time "trust" on iPhone/iPad. WEBPOD_KEY, when set, must arrive in the page URL (?k=...) once;
the page keeps it. Config from the environment (the bridge's ~/.skwoop/env):

  WEBPOD_HOST (0.0.0.0)  WEBPOD_PORT (8443)  WEBPOD_TLS_DIR (~/.skwoop/webpod-tls)  WEBPOD_KEY (optional)
  WEBPOD_BRIDGE_URL (ws://127.0.0.1:$SKWOOP_PORT/xiaozhi/v1/)
  SKWOOP_DEVICE_TOKEN (sent to the bridge, never to browsers)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import auth

from lean_http import (
    Application,
    AppKey,
    AppRunner,
    ClientError,
    ClientSession,
    FileResponse,
    HTTPNotFound,
    Request,
    Response,
    StreamResponse,
    TCPSite,
    WebSocketResponse,
    json_response,
)
from lean_ws import WSMsgType, WebSocket

log = logging.getLogger("skwoop.webpod")

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DEVICE_PREFIX = "web-pod-"
IN_RATE, OUT_RATE, FRAME_MS = 16000, 24000, 60
IN_FRAME_SAMPLES = IN_RATE * FRAME_MS // 1000          # 960 samples = 1920 bytes per Opus frame
OUT_MAX_SAMPLES = OUT_RATE * 120 // 1000                # the longest Opus frame the bridge could send
HELLO_TIMEOUT_S = 10.0
FORWARD_FROM_BRIDGE = frozenset({"hello", "tts", "llm", "stt", "alert", "inbox", "status", "listen"})
_SLUG = re.compile(r"[^a-z0-9]+")


# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class WebPodConfig:
    host: str
    port: int
    tls_dir: Path
    access_key: str
    bridge_url: str
    device_token: str

    @property
    def cert(self) -> Path:
        return self.tls_dir / "server.crt"

    @property
    def key(self) -> Path:
        return self.tls_dir / "server.key"

    @property
    def ca_cert(self) -> Path:
        return self.tls_dir / "ca.crt"


def config_from_env(environ: dict[str, str]) -> WebPodConfig:
    def get(name: str, default: str = "") -> str:
        return environ.get(name, default).strip()

    bridge_port = get("SKWOOP_PORT", "8003") or "8003"
    port_text = get("WEBPOD_PORT", "8443") or "8443"
    if not port_text.isdigit():
        raise SystemExit(f"WEBPOD_PORT must be a number, not {port_text!r}")

    tls_dir = get("WEBPOD_TLS_DIR", "~/.skwoop/webpod-tls") or "~/.skwoop/webpod-tls"
    return WebPodConfig(
        host=get("WEBPOD_HOST", "0.0.0.0") or "0.0.0.0",
        port=int(port_text),
        tls_dir=Path(os.path.expanduser(tls_dir)),
        access_key=get("WEBPOD_KEY"),
        bridge_url=get("WEBPOD_BRIDGE_URL") or f"ws://127.0.0.1:{bridge_port}/xiaozhi/v1/",
        device_token=get("SKWOOP_DEVICE_TOKEN"),
    )


# ------------------------------------------------------------------ browser messages (parsed at the boundary)
@dataclass(frozen=True)
class BrowserHello:
    name: str            # the slug after web-pod-
    key: str

    @property
    def device_id(self) -> str:
        return DEVICE_PREFIX + self.name


@dataclass(frozen=True)
class Listen:
    state: Literal["start", "stop"]
    mode: Literal["manual", "auto"]
    barge: bool = False          # the page heard Alex over Skwoop's voice and cut its playback


@dataclass(frozen=True)
class Abort:
    pass


@dataclass(frozen=True)
class InboxPlay:
    action: Literal["play", "next", "skip", "stop"]


BrowserCommand = Listen | Abort | InboxPlay


class BadMessage(ValueError):
    pass


def device_slug(raw: str) -> str:
    """"Alex's iPhone" -> "alex-s-iphone" (lowercase letters, digits and dashes, at most 24 characters)."""
    slug = _SLUG.sub("-", raw.lower()).strip("-")[:24].strip("-")
    if not slug:
        raise BadMessage("device name is empty")

    return slug


def _json_object(text: str) -> dict[str, object]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise BadMessage("not JSON") from e

    if not isinstance(raw, dict):
        raise BadMessage("not a JSON object")

    return {str(k): v for k, v in raw.items()}


def parse_hello(text: str) -> BrowserHello:
    msg = _json_object(text)
    if msg.get("type") != "hello":
        raise BadMessage("the first message must be hello")

    name, key = msg.get("device"), msg.get("key", "")
    if not isinstance(name, str) or not isinstance(key, str):
        raise BadMessage("hello needs a device name")

    return BrowserHello(device_slug(name), key)


def parse_command(text: str) -> BrowserCommand:
    msg = _json_object(text)
    kind = msg.get("type")
    if kind == "listen":
        state, mode = msg.get("state"), msg.get("mode", "manual")
        if state not in ("start", "stop") or mode not in ("manual", "auto"):
            raise BadMessage("listen needs state start|stop and mode manual|auto")

        return Listen(state, mode, msg.get("barge") is True and state == "start")

    if kind == "abort":
        return Abort()

    if kind == "inbox":
        action = msg.get("action", "play")
        if action not in ("play", "next", "skip", "stop"):
            raise BadMessage("inbox action must be play|next|skip|stop")

        return InboxPlay(action)

    raise BadMessage(f"unknown message type {kind!r}")


def to_bridge(cmd: BrowserCommand) -> dict[str, object]:
    """The pod-protocol frame for a browser command (what the firmware sends)."""
    match cmd:
        case Listen(state, mode, barge):
            return {"type": "listen", "state": state, "mode": mode, **({"barge": True} if barge else {})}
        case Abort():
            return {"type": "abort"}
        case InboxPlay(action):
            return {"type": "inbox", "action": action}


def bridge_event(text: str) -> str | None:
    """A bridge text frame -> the JSON to forward to the browser, or None (malformed or not for the page)."""
    try:
        msg = _json_object(text)
    except BadMessage:
        return None

    kind = msg.get("type")
    if not isinstance(kind, str) or kind not in FORWARD_FROM_BRIDGE:
        return None

    return json.dumps(msg)


# ------------------------------------------------------------------ audio
class PcmFramer:
    """Browser PCM arrives in whatever chunk size the audio worklet produced; Opus wants exact 60 ms frames."""

    def __init__(self, frame_bytes: int = IN_FRAME_SAMPLES * 2):
        self.frame_bytes = frame_bytes
        self.pending = bytearray()

    def push(self, chunk: bytes) -> list[bytes]:
        if len(chunk) % 2:
            raise BadMessage("PCM chunks are 16-bit samples")

        self.pending += chunk
        frames = []
        while len(self.pending) >= self.frame_bytes:
            frames.append(bytes(self.pending[:self.frame_bytes]))
            del self.pending[:self.frame_bytes]

        return frames

    def flush(self) -> bytes | None:
        """The last partial frame, zero-padded (end of a push-to-talk utterance)."""
        if not self.pending:
            return None

        frame = bytes(self.pending) + b"\x00" * (self.frame_bytes - len(self.pending))
        self.pending.clear()

        return frame


class Codec(Protocol):
    def encode(self, pcm16k: bytes) -> bytes: ...

    def decode(self, opus24k: bytes) -> bytes: ...


class OpusCodec:
    def __init__(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the bridge's opus.py (libopus via ctypes)
        import opus

        self._enc = opus.Encoder(IN_RATE, 1, "voip")
        self._dec = opus.Decoder(OUT_RATE, 1)

    def encode(self, pcm16k: bytes) -> bytes:
        return self._enc.encode(pcm16k, IN_FRAME_SAMPLES)

    def decode(self, opus24k: bytes) -> bytes:
        return self._dec.decode(opus24k, OUT_MAX_SAMPLES)


# ------------------------------------------------------------------ the relay
def device_hello() -> dict[str, object]:
    """What the pod firmware sends first (xiaozhi protocol v1)."""
    return {"type": "hello", "version": 1, "transport": "websocket", "features": {"mcp": True, "barge_in": "client"},
            "audio_params": {"format": "opus", "sample_rate": IN_RATE, "channels": 1, "frame_duration": FRAME_MS}}


async def _browser_to_bridge(page: WebSocketResponse, bridge: WebSocket, codec: Codec) -> None:
    framer = PcmFramer()
    async for msg in page:
        if msg.type == WSMsgType.BINARY and isinstance(msg.data, bytes):
            for frame in framer.push(msg.data):
                await bridge.send_bytes(codec.encode(frame))

            continue

        if msg.type != WSMsgType.TEXT or not isinstance(msg.data, str):
            break

        try:
            cmd = parse_command(msg.data)
        except BadMessage as e:
            await page.send_str(json.dumps({"type": "webpod", "error": str(e)}))

            continue

        if isinstance(cmd, Listen) and cmd.state == "stop":
            tail = framer.flush()
            if tail is not None:
                await bridge.send_bytes(codec.encode(tail))

        await bridge.send_str(json.dumps(to_bridge(cmd)))


async def _bridge_to_browser(bridge: WebSocket, page: WebSocketResponse, codec: Codec) -> None:
    async for msg in bridge:
        if msg.type == WSMsgType.BINARY and isinstance(msg.data, bytes):
            await page.send_bytes(codec.decode(msg.data))

            continue

        if msg.type != WSMsgType.TEXT or not isinstance(msg.data, str):
            break

        event = bridge_event(msg.data)
        if event is not None:
            await page.send_str(event)


CodecFactory = Callable[[], Codec]


def _cfg(app: Application) -> WebPodConfig:
    cfg = app.get(CONFIG)
    if not isinstance(cfg, WebPodConfig):
        raise RuntimeError("webpod config missing")

    return cfg


def _http(app: Application) -> ClientSession:
    http = app.get(HTTP)
    if not isinstance(http, ClientSession):
        raise RuntimeError("webpod http missing")

    return http


def _clients(app: Application) -> set[str]:
    clients = app.get(CLIENTS)
    if not isinstance(clients, set):
        raise RuntimeError("webpod clients missing")

    return clients


def _codec(app: Application) -> Codec:
    factory = app.get(CODEC)
    if not callable(factory):
        raise RuntimeError("webpod codec missing")

    return factory()


async def pod_handler(request: Request) -> WebSocketResponse:
    cfg = _cfg(request.app)
    page = WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
    await page.prepare(request)
    try:
        first = await page.receive(timeout=HELLO_TIMEOUT_S)
        text = first.data if first.type == WSMsgType.TEXT and isinstance(first.data, str) else ""
        hello = parse_hello(text)
    except (TimeoutError, BadMessage) as e:
        await page.send_str(json.dumps({"type": "webpod", "error": f"bad hello: {e}"}))
        await page.close()

        return page

    if not auth.access_key_ok(hello.key, cfg.access_key):
        log.warning("web pod %s: wrong access key", hello.device_id)
        await page.send_str(json.dumps({"type": "webpod", "error": "access key needed: open the link with ?k=..."}))
        await page.close()

        return page

    headers = {"Device-Id": hello.device_id, "Client-Id": hello.device_id, "Protocol-Version": "1"}
    if cfg.device_token:
        headers["Authorization"] = f"Bearer {cfg.device_token}"

    try:
        bridge = await _http(request.app).ws_connect(
            cfg.bridge_url, headers=headers, heartbeat=30, max_msg_size=1 << 20)
    except (ClientError, OSError) as e:
        log.warning("web pod %s: bridge unreachable (%s)", hello.device_id, type(e).__name__)
        await page.send_str(json.dumps({"type": "webpod", "error": "the bridge is not reachable"}))
        await page.close()

        return page

    codec = _codec(request.app)
    _clients(request.app).add(hello.device_id)
    log.info("web pod %s connected from %s", hello.device_id, request.remote)
    try:
        await bridge.send_str(json.dumps(device_hello()))
        await page.send_str(json.dumps({"type": "webpod", "device": hello.device_id, "connected": True}))
        up = asyncio.create_task(_browser_to_bridge(page, bridge, codec))
        down = asyncio.create_task(_bridge_to_browser(bridge, page, codec))
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        # one side owns the socket read; let cancel land before close() reads too
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None:
                log.warning("web pod %s relay stopped: %s", hello.device_id, type(exc).__name__)
    finally:
        _clients(request.app).discard(hello.device_id)
        await bridge.close()
        await page.close()
        log.info("web pod %s disconnected", hello.device_id)

    return page


# ------------------------------------------------------------------ pages
NO_CACHE = {"Cache-Control": "no-cache"}


async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


async def ca_cert(request: Request) -> StreamResponse:
    cfg = _cfg(request.app)
    if not cfg.ca_cert.exists():
        raise HTTPNotFound(text="no local CA here (webpod/make_cert.sh)")

    return Response(body=cfg.ca_cert.read_bytes(), content_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="skwoop-webpod-ca.crt"'})


async def healthz(request: Request) -> Response:
    body = {"ok": True, "bridge": _cfg(request.app).bridge_url, "pods": sorted(_clients(request.app))}

    return json_response(body)


CONFIG = AppKey[WebPodConfig]("config")
HTTP = AppKey[ClientSession]("http")
CODEC = AppKey[CodecFactory]("codec")
CLIENTS = AppKey[set[str]]("clients")


def make_app(cfg: WebPodConfig, codec: CodecFactory = OpusCodec) -> Application:
    app = Application()
    app[CONFIG] = cfg
    app[CODEC] = codec
    clients: set[str] = set()
    app[CLIENTS] = clients

    async def open_http(app: Application) -> None:
        app[HTTP] = ClientSession()

    async def close_http(app: Application) -> None:
        http = app.get(HTTP)
        if isinstance(http, ClientSession):
            await http.close()

    app.on_startup.append(open_http)
    app.on_cleanup.append(close_http)
    app.router.add_get("/", index)
    app.router.add_get("/pod", pod_handler)
    app.router.add_get("/ca.crt", ca_cert)
    app.router.add_get("/healthz", healthz)
    app.router.add_static("/static/", STATIC, append_version=True)

    return app


def tls_context(cfg: WebPodConfig) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cfg.cert, cfg.key)

    return ctx


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stdout)
    cfg = config_from_env(dict(os.environ))
    log.info("web pod on https://%s:%d -> %s (access key %s)", cfg.host, cfg.port, cfg.bridge_url,
             "on" if cfg.access_key else "off")

    async def run() -> None:
        runner = AppRunner(make_app(cfg))
        await runner.setup()
        try:
            await TCPSite(runner, cfg.host, cfg.port, ssl_context=tls_context(cfg)).start()
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
