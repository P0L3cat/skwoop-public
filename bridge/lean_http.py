# ruff: noqa: E301, E302, E303, E305, E701
"""HTTP/1.1 server and client plus the WebSocket handshake. Framing is lean_ws."""
from __future__ import annotations
import asyncio
import base64
import json as _json
import os
import re
import ssl as sslmod
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Generic, Protocol, TypeVar, cast, overload
from urllib.parse import SplitResult, parse_qsl, quote, unquote, urlencode, urlsplit
from lean_ws import WSMessage, WSMsgType, WebSocket, accept_key
MAX_BODY = 4 * 1024 * 1024  # default hard cap for a request/response entity (auth routes never buffer more)
MAX_BODY_UPLOAD = 32 * 1024 * 1024  # opt-in for upload handlers via Application.max_body
SERVER_IDLE_S = 60.0  # drop a quiet keep-alive peer
SERVER_HEADER_S = 15.0  # time to finish the request line + headers
SERVER_BODY_S = 60.0  # time to finish reading one body under the cap
_MIME = {
    ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
    ".png": "image/png", ".svg": "image/svg+xml", ".txt": "text/plain", ".ttf": "font/ttf",
    ".wav": "audio/wav", ".map": "application/json",
}
_H = Mapping[str, str] | None
_P = Mapping[str, object] | None
_S = sslmod.SSLContext | bool | None
# Wire JSON at the parse boundary. Same alias as house_tools.Json.
Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None
_T = TypeVar("_T")
class ClientError(Exception):
    pass
class HTTPException(Exception):
    status = 500
    def __init__(self, *, text: str = "", headers: _H = None) -> None:
        super().__init__(text)
        self.text = text
        self.headers = dict(headers or {})
class HTTPNotFound(HTTPException):
    status = 404
class _MethodNotAllowed(HTTPException):
    status = 405
    def __init__(self, allow: str) -> None:
        super().__init__(text="method not allowed", headers={"Allow": allow})
class _Bad(Exception):
    pass
class _HandshakeFailed(Exception):
    pass
class CIHeaders(Mapping[str, str]):
    """Case-insensitive map; iteration keeps the first spelling of each name."""
    def __init__(self, items: _H | Iterable[tuple[str, str]] = None) -> None:
        self._pairs: list[tuple[str, str]] = []
        self._map: dict[str, int] = {}
        pairs: Iterable[tuple[str, str]] = items.items() if isinstance(items, Mapping) else (items or ())
        for key, value in pairs:
            self.set(str(key), str(value))
    def set(self, key: str, value: str) -> None:
        low = key.lower()
        if low in self._map:
            self._pairs[self._map[low]] = (key, value)
        else:
            self._map[low] = len(self._pairs)
            self._pairs.append((key, value))
    def __getitem__(self, key: str) -> str:
        try:
            return self._pairs[self._map[key.lower()]][1]
        except KeyError:
            raise KeyError(key) from None
    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._pairs)
    def __len__(self) -> int:
        return len(self._pairs)
    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._pairs)
class ClientTimeout:
    def __init__(self, *, total: float | None = None, sock_read: float | None = None,
                 sock_connect: float | None = None, connect: float | None = None) -> None:
        self.total, self.sock_read = total, sock_read
        self.sock_connect = connect if sock_connect is None else sock_connect
class _Clock:
    def __init__(self, timeout: ClientTimeout | None) -> None:
        self.sock_read = None if timeout is None else timeout.sock_read
        self.sock_connect = None if timeout is None else timeout.sock_connect
        total = None if timeout is None else timeout.total
        self.deadline = None if total is None else time.monotonic() + total
    def budget(self) -> float | None:
        if self.deadline is None: return self.sock_read
        left = self.deadline - time.monotonic()
        if left <= 0: raise TimeoutError
        return self.sock_read if self.sock_read is not None and self.sock_read < left else left
    def connect_limit(self) -> float | None:
        limit = self.budget()
        if self.sock_connect is None or (limit is not None and limit < self.sock_connect): return limit
        return self.sock_connect
class _IO:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer, self.buf = reader, writer, bytearray()
    async def _read(self, n: int, clock: _Clock | None) -> bytes:
        try:
            if clock is None: return await self.reader.read(n)
            timeout = clock.budget()
            if timeout is None: return await self.reader.read(n)
            return await asyncio.wait_for(self.reader.read(n), timeout)
        except TimeoutError:
            raise
        except OSError as exc:
            raise ClientError(exc.strerror or str(exc)) from exc
    async def line(self, clock: _Clock | None) -> bytes | None:
        while b"\r\n" not in self.buf:
            if len(self.buf) > 1 << 20: raise ClientError("line too long")
            data = await self._read(4096, clock)
            if not data: return None
            self.buf += data
        idx = self.buf.find(b"\r\n")
        line = bytes(self.buf[:idx])
        del self.buf[:idx + 2]
        return line
    async def take(self, n: int, clock: _Clock | None, *, exact: bool) -> bytes:
        while len(self.buf) < n:
            data = await self._read(min(65536, max(1, n - len(self.buf))), clock)
            if not data:
                if exact: raise ClientError("truncated")
                break
            self.buf += data
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out
class ClientBody:
    def __init__(self, io: _IO, clock: _Clock, mode: str, length: int, *, max_body: int = MAX_BODY) -> None:
        self._io, self._clock, self._mode, self._left = io, clock, mode, length
        self._max_body = max_body
        self._total = 0
        self._buf, self._pos = bytearray(), 0
        self._done = mode == "empty" or (mode == "length" and length == 0)
        self._err: ClientError | None = None
    async def _pull(self) -> bytes:
        if self._mode == "empty": return b""
        if self._mode == "eof": return await self._io.take(65536, self._clock, exact=False)
        if self._mode == "length":
            if self._left <= 0: return b""
            got = await self._io.take(min(self._left, 65536), self._clock, exact=False)
            if not got: raise ClientError("truncated")
            self._left -= len(got)
            return got
        line = await self._io.line(self._clock)
        if line is None: raise ClientError("truncated")
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise ClientError("bad chunk") from None
        if size == 0:
            while await self._io.line(self._clock):
                pass
            return b""
        if not 0 < size <= self._max_body: raise ClientError("bad chunk")
        data = await self._io.take(size, self._clock, exact=True)
        if await self._io.take(2, self._clock, exact=True) != b"\r\n": raise ClientError("bad chunk")
        return data
    async def _fill(self) -> None:
        if self._done: return
        try:
            data = await self._pull()
        except ClientError as exc:
            self._err, self._done = exc, True
            return
        if data:
            self._total += len(data)
            if self._total > self._max_body:
                self._err, self._done = ClientError("body too large"), True
                return
            self._buf += data
        else: self._done = True
    async def read(self) -> bytes:
        while not self._done:
            await self._fill()
        if self._err is not None and self._pos >= len(self._buf): raise self._err
        out = bytes(self._buf[self._pos:])
        self._pos = len(self._buf)
        return out
    async def readline(self) -> bytes:
        while True:
            idx = self._buf.find(b"\n", self._pos)
            if idx >= 0:
                line = bytes(self._buf[self._pos:idx + 1])
                self._pos = idx + 1
                return line
            if self._done:
                if self._err is not None and self._pos >= len(self._buf): raise self._err
                line = bytes(self._buf[self._pos:])
                self._pos = len(self._buf)
                return line
            await self._fill()
    def __aiter__(self) -> ClientBody:
        return self
    async def __anext__(self) -> bytes:
        line = await self.readline()
        if line == b"": raise StopAsyncIteration
        return line
    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        if n <= 0: raise ClientError("bad chunk")
        while True:
            if self._pos >= len(self._buf):
                if self._done:
                    if self._err is not None: raise self._err
                    return
                await self._fill()
                continue
            end = min(len(self._buf), self._pos + n)
            piece = bytes(self._buf[self._pos:end])
            self._pos = end
            yield piece
class FormData:
    def __init__(self) -> None:
        self._fields: list[tuple[str, bytes, str | None, str | None]] = []
    def add_field(self, name: str, value: str | bytes, *, filename: str | None = None,
                  content_type: str | None = None) -> None:
        self._fields.append((name, value.encode() if isinstance(value, str) else value, filename, content_type))
    def encode(self) -> tuple[bytes, str]:
        boundary = "lean" + os.urandom(8).hex()
        chunks: list[bytes] = []
        for name, raw, filename, ctype in self._fields:
            disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename is not None else "")
            head = f"--{boundary}\r\nContent-Disposition: {disp}\r\n"
            if ctype is not None:
                head += f"Content-Type: {ctype}\r\n"
            chunks.append(head.encode("ascii") + b"\r\n" + raw + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
class _Part:
    def __init__(self, name: str, filename: str, data: bytes) -> None:
        self.name, self.filename, self._data, self._at = name, filename, data, 0
    async def text(self) -> str:
        return self._data.decode()
    async def read_chunk(self, n: int = 65536) -> bytes:
        chunk = self._data[self._at:self._at + n]
        self._at += len(chunk)
        return chunk
class _Multipart:
    def __init__(self, parts: list[_Part]) -> None:
        self._parts, self._i = parts, 0
    def __aiter__(self) -> _Multipart:
        return self
    async def __anext__(self) -> _Part:
        if self._i >= len(self._parts): raise StopAsyncIteration
        self._i += 1
        return self._parts[self._i - 1]
def _param(value: str, name: str) -> str:
    found = re.search(rf'(?:^|;)\s*{name}\s*=\s*(?:"([^"]*)"|([^;]+))', value)
    got = "" if found is None else found.group(1) if found.group(1) is not None else found.group(2)
    return (got or "").strip()
def _parse_multipart(body: bytes, header: str) -> list[_Part]:
    found = re.search(r"boundary=([^;]+)", header, re.IGNORECASE)
    if found is None: raise ValueError("multipart body has no boundary")
    marker = b"--" + found.group(1).strip().strip('"').encode("latin-1")
    parts: list[_Part] = []
    for piece in body.split(marker):
        if piece in (b"", b"\r\n") or piece.startswith(b"--"): continue
        piece = piece.removeprefix(b"\r\n").removesuffix(b"\r\n")
        head, sep, data = piece.partition(b"\r\n\r\n")
        if not sep: continue
        rows = (line.partition(":") for line in head.decode("latin-1").split("\r\n"))
        fields = {k.strip().lower(): v.strip() for k, _, v in rows if k}
        disp = fields.get("content-disposition", "")
        parts.append(_Part(_param(disp, "name"), _param(disp, "filename"), data))
    return parts
class Request:
    def __init__(self, method: str, path: str, query: str, headers: CIHeaders, keep: bool,
                 app: Application, io: _IO, remote: str, body_mode: str, body_length: int) -> None:
        self.method, self.path, self.headers, self.app = method, path, headers, app
        self.query: dict[str, str] = {}
        for name, value in parse_qsl(query, keep_blank_values=True):
            self.query.setdefault(name, value)
        self.match_info: dict[str, str] = {}
        self.transport, self.remote, self.reader, self.pending = io.writer, remote, io.reader, io.buf
        self.keep, self.started = keep, False
        self._io, self._body_mode, self._body_length = io, body_mode, body_length
        self._body: bytes | None = None
        self._json: object = None
        self._json_set = False
        self._parts: _Multipart | None = None
    @property
    def host(self) -> str:
        return self.headers.get("Host") or ""
    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    async def read(self) -> bytes:
        if self._body is None:
            # Auth checks headers first; a 401 never buffers the entity.
            self._body = await _collect(self._io, self.headers, self.app.max_body,
                                        mode=self._body_mode, length=self._body_length)
        return self._body
    async def json(self) -> Json:
        if not self._json_set:
            self._json, self._json_set = cast(Json, _loads(await self.read())), True
        return cast(Json, self._json)
    async def multipart(self) -> _Multipart:
        if self._parts is None:
            self._parts = _Multipart(_parse_multipart(await self.read(), self.headers.get("Content-Type") or ""))
        return self._parts
class StreamResponse:
    def __init__(self, *, status: int = 200, headers: _H = None) -> None:
        self.status, self.headers = status, CIHeaders(headers)
        self.prepared = self.eof = self._chunked = False
        self._writer: asyncio.StreamWriter | None = None
    async def prepare(self, request: Request) -> None:
        if self.prepared: return
        self._writer = request.transport
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if self.headers.get("Content-Length") is None and "chunked" not in te:
            self.headers.set("Transfer-Encoding", "chunked")
            te = "chunked"
        self._chunked = "chunked" in te
        _keep(request, self.headers)
        await _send(request.transport, self.status, self.headers, None)
        request.started = self.prepared = True
    async def write(self, data: bytes) -> None:
        writer = self._writer
        if not data or writer is None: return
        if writer.is_closing(): raise ConnectionError("closed")
        writer.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n" if self._chunked else data)
        await writer.drain()
    async def write_eof(self) -> None:
        if self.eof: return
        self.eof = True
        writer = self._writer
        if writer is None or writer.is_closing() or not self._chunked: return
        writer.write(b"0\r\n\r\n")
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            return
class Response(StreamResponse):
    def __init__(self, *, status: int = 200, body: bytes | str | None = None, text: str | None = None,
                 content_type: str | None = None, headers: _H = None) -> None:
        super().__init__(status=status, headers=headers)
        if text is not None or isinstance(body, str):
            raw = text if text is not None else body
            assert isinstance(raw, str)
            self.body, content_type = raw.encode(), content_type or "text/plain; charset=utf-8"
        else:
            self.body = b"" if body is None else body
            if content_type is None and body is not None and self.headers.get("Content-Type") is None:
                content_type = "application/octet-stream"
        if content_type is not None and self.headers.get("Content-Type") is None:
            self.headers.set("Content-Type", content_type)
class FileResponse(Response):
    def __init__(self, path: str | Path, *, headers: _H = None) -> None:
        file = Path(path)
        if not file.is_file():
            super().__init__(status=404, text="not found", content_type="text/plain", headers=headers)
            return
        super().__init__(status=200, body=file.read_bytes(), headers=headers,
                         content_type=_MIME.get(file.suffix.lower(), "application/octet-stream"))
class WebSocketResponse:
    def __init__(self, *, heartbeat: float | None = None, max_msg_size: int = 1 << 20, autoclose: bool = True) -> None:
        self.heartbeat, self.max_msg_size, self.autoclose = heartbeat, max_msg_size, autoclose
        self.prepared = False
        self._ws: WebSocket | None = None
    @property
    def closed(self) -> bool:
        return self._ws is None or self._ws.closed
    async def prepare(self, request: Request) -> None:
        key = request.headers.get("Sec-WebSocket-Key")
        connection = (request.headers.get("Connection") or "").lower()
        upgrade = (request.headers.get("Upgrade") or "").lower()
        ok = bool(key) and request.headers.get("Sec-WebSocket-Version") == "13"
        ok = ok and "upgrade" in connection and upgrade == "websocket"
        if not ok or key is None:
            body = b"bad websocket handshake"
            headers = CIHeaders({"Content-Type": "text/plain", "Content-Length": str(len(body)), "Connection": "close"})
            await _send(request.transport, 400, headers, body)
            request.started, request.keep = True, False
            raise _HandshakeFailed
        headers = CIHeaders((("Upgrade", "websocket"), ("Connection", "Upgrade"),
                             ("Sec-WebSocket-Accept", accept_key(key))))
        await _send(request.transport, 101, headers, None)
        pending = bytes(request.pending)
        request.pending.clear()
        self._ws = WebSocket(request.reader, request.transport, role="server", max_size=self.max_msg_size,
                             heartbeat=self.heartbeat, autoclose=self.autoclose, pending=pending)
        request.started, request.keep, self.prepared = True, False, True
    async def send_str(self, text: str) -> None:
        await self._socket().send_str(text)
    async def send_bytes(self, data: bytes) -> None:
        await self._socket().send_bytes(data)
    async def ping(self, data: bytes = b"") -> None:
        await self._socket().ping(data)
    async def receive(self, timeout: float | None = None) -> WSMessage:
        return await self._socket().receive(timeout)
    def __aiter__(self) -> WebSocket:
        return self._socket()
    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._ws is not None:
            await self._ws.close(code, reason)
    def _socket(self) -> WebSocket:
        if self._ws is None: raise RuntimeError("websocket is not prepared")
        return self._ws
def json_response(data: object, *, status: int = 200, headers: _H = None) -> Response:
    return Response(status=status, body=_json.dumps(data).encode(), content_type="application/json", headers=headers)
def _loads(raw: bytes | str) -> object:
    return cast(object, _json.loads(raw))
Handler = Callable[[Request], Awaitable[StreamResponse | WebSocketResponse]]
class RouteDef:
    def __init__(self, method: str, path: str, handler: Handler) -> None:
        self.method, self.path, self.handler = method, path, handler
def _pattern(path: str) -> re.Pattern[str]:
    parts: list[str] = []
    for seg in path.split("/"):
        if len(seg) >= 2 and seg.startswith("{") and seg.endswith("}") and seg[1:-1].isidentifier():
            parts.append(f"(?P<{seg[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(seg))
    return re.compile("/".join(parts))
class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, Handler, re.Pattern[str]]] = []
    def add_route(self, method: str, path: str, handler: Handler) -> None:
        self._routes.append((method.upper(), handler, _pattern(path)))
    def add_get(self, path: str, handler: Handler) -> None:
        self.add_route("GET", path, handler)
    def add_post(self, path: str, handler: Handler) -> None:
        self.add_route("POST", path, handler)
    def add_put(self, path: str, handler: Handler) -> None:
        self.add_route("PUT", path, handler)
    def add_delete(self, path: str, handler: Handler) -> None:
        self.add_route("DELETE", path, handler)
    def add_routes(self, routes: Iterable[RouteDef]) -> None:
        for route in routes:
            self.add_route(route.method, route.path, route.handler)
    def add_static(self, prefix: str, directory: str | Path, **_extra: object) -> None:
        root, head = Path(directory).resolve(), prefix if prefix.endswith("/") else prefix + "/"
        async def handler(request: Request) -> StreamResponse:
            rel = unquote(request.path[len(head):])
            full = (root / rel).resolve()
            bad = any(part in ("", ".", "..") for part in rel.split("/"))
            if bad or not full.is_relative_to(root) or not full.is_file(): return Response(status=404, text="not found")
            return FileResponse(full)
        self._routes.append(("GET", handler, re.compile(re.escape(head) + ".*")))
    def resolve(self, method: str, path: str) -> tuple[Handler, dict[str, str]]:
        allowed: list[str] = []
        for route_method, handler, pattern in self._routes:
            found = pattern.fullmatch(path)
            if found is None: continue
            if route_method != method:
                allowed.append(route_method)
                continue
            return handler, {key: unquote(value) for key, value in found.groupdict().items()}
        if allowed: raise _MethodNotAllowed(", ".join(dict.fromkeys(allowed)))
        raise HTTPNotFound(text="not found")
class AppKey(Generic[_T]):
    """Process-dict key. `app[AppKey[T](...)]` is T; string keys stay object."""
    def __init__(self, name: str, t: type[object] | None = None) -> None:
        self.name, self._t = name, t
class Application:
    """One process bag: sessions, stores, clients. AppKey[T] reads back as T."""
    def __init__(self, *, max_body: int = MAX_BODY) -> None:
        self._bag: dict[object, object] = {}
        self.router = Router()
        self.runner: AppRunner | None = None
        self.max_body = max_body  # hard entity cap; use MAX_BODY_UPLOAD for an upload app
        self.on_startup: list[Callable[[Application], Awaitable[None]]] = []
        self.on_cleanup: list[Callable[[Application], Awaitable[None]]] = []
    def __contains__(self, key: object) -> bool:
        return key in self._bag
    @overload
    def __getitem__(self, key: AppKey[_T]) -> _T: ...
    @overload
    def __getitem__(self, key: str) -> object: ...
    def __getitem__(self, key: object) -> object:
        return self._bag[key]
    @overload
    def __setitem__(self, key: AppKey[_T], value: _T) -> None: ...
    @overload
    def __setitem__(self, key: str, value: object) -> None: ...
    def __setitem__(self, key: object, value: object) -> None:
        self._bag[key] = value
    @overload
    def get(self, key: AppKey[_T], default: None = None) -> _T | None: ...
    @overload
    def get(self, key: str, default: object = None) -> object: ...
    def get(self, key: object, default: object = None) -> object:
        return self._bag.get(key, default)
    def add_routes(self, routes: Iterable[RouteDef]) -> None:
        self.router.add_routes(routes)
class AppRunner:
    def __init__(self, app: Application) -> None:
        self.app = app
        self.addresses: list[tuple[str, int]] = []
        self._servers: list[asyncio.Server] = []
        self._writers: set[asyncio.StreamWriter] = set()
    async def setup(self) -> None:
        self.app.runner = self
        for hook in list(self.app.on_startup):
            await hook(self.app)
    async def cleanup(self) -> None:
        for server in self._servers:
            server.close()
        for writer in list(self._writers):
            writer.close()
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
        for hook in list(self.app.on_cleanup):
            await hook(self.app)
class TCPSite:
    def __init__(self, runner: AppRunner, host: str, port: int, *,
                 ssl_context: sslmod.SSLContext | None = None) -> None:
        self.runner, self.host, self.port, self.ssl = runner, host, port, ssl_context
    async def start(self) -> None:
        app = self.runner.app
        async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await _serve(reader, writer, app)
        server = await asyncio.start_server(on_connect, self.host, self.port, ssl=self.ssl)
        self.runner._servers.append(server)
        for sock in server.sockets or ():
            got: object = sock.getsockname()
            if isinstance(got, tuple) and len(got) >= 2 and isinstance(got[0], str) and isinstance(got[1], int):
                self.runner.addresses.append((got[0], got[1]))
async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, app: Application) -> None:
    runner = app.runner
    if runner is None:
        writer.close()
        return
    io = _IO(reader, writer)
    runner._writers.add(writer)
    try:
        while True:
            try:
                request = await _read_request(io, app)
            except ClientError as exc:
                big = "too large" in str(exc)
                code, msg = (413, b"too large\n") if big else (400, b"bad request")
                await _send(writer, code, CIHeaders({"Content-Length": str(len(msg)), "Connection": "close"}), msg)
                break
            except _Bad:
                await _send(writer, 400, CIHeaders({"Content-Length": "11", "Connection": "close"}), b"bad request")
                break
            if request is None or not await _dispatch(request): break
    except (ConnectionError, OSError):
        return
    finally:
        runner._writers.discard(writer)
        _close(writer)
async def _read_head(io: _IO, clock: _Clock | None) -> list[bytes]:
    lines: list[bytes] = []
    while True:
        line = await io.line(clock)
        if line is None:
            if lines: raise ClientError("truncated")
            return []
        if line == b"": return lines
        lines.append(line)
        if len(lines) > 200: raise ClientError("headers")
def _headers_from(lines: list[bytes]) -> CIHeaders:
    items: list[tuple[str, str]] = []
    for raw in lines:
        text = raw.decode("latin-1")
        if ":" not in text: raise ClientError("bad header")
        name, value = text.split(":", 1)
        items.append((name.strip(), value.strip()))
    return CIHeaders(items)
async def _read_request(io: _IO, app: Application) -> Request | None:
    # Headers only. The body is read on demand so auth can 401 without buffering.
    clock = _Clock(ClientTimeout(sock_read=SERVER_HEADER_S, total=SERVER_HEADER_S))
    lines = await _read_head(io, clock)
    if not lines: return None
    bits = lines[0].decode("latin-1").split(" ")
    if len(bits) != 3 or bits[2] not in ("HTTP/1.0", "HTTP/1.1"): raise _Bad
    method, target, version = bits
    path, _, query = target.partition("?")
    if not path.startswith("/"): raise _Bad
    headers = _headers_from(lines[1:])
    mode, length = _request_body_mode(headers, app.max_body)
    token = (headers.get("Connection") or "").lower()
    keep = "keep-alive" in token if version == "HTTP/1.0" else "close" not in token
    peer: object = io.writer.get_extra_info("peername")
    remote = peer[0] if isinstance(peer, tuple) and peer and isinstance(peer[0], str) else ""
    return Request(method, path, query, headers, keep, app, io, remote, mode, length)

def _request_body_mode(headers: CIHeaders, max_body: int) -> tuple[str, int]:
    transfer, length = headers.get("Transfer-Encoding"), headers.get("Content-Length")
    if transfer is not None and length is not None: raise _Bad
    if transfer is not None:
        if "chunked" not in transfer.lower(): raise _Bad
        return "chunked", 0
    if length is None: return "empty", 0
    try:
        size = int(length)
    except ValueError:
        raise _Bad from None
    if size < 0: raise _Bad
    if size > max_body: raise ClientError("body too large")
    return ("empty", 0) if size == 0 else ("length", size)

async def _collect(io: _IO, headers: CIHeaders, max_body: int, *, mode: str, length: int) -> bytes:
    clock = _Clock(ClientTimeout(sock_read=SERVER_BODY_S, total=SERVER_BODY_S))
    if mode == "empty":
        return b""
    if mode == "chunked":
        return await ClientBody(io, clock, "chunked", 0, max_body=max_body).read()
    if mode == "length":
        if length > max_body: raise ClientError("body too large")
        return await io.take(length, clock, exact=True) if length else b""
    raise _Bad
async def _dispatch(request: Request) -> bool:
    try:
        handler, info = request.app.router.resolve(request.method, request.path)
        request.match_info = info
        reply = await handler(request)
    except _HandshakeFailed:
        return False
    except HTTPException as exc:
        if request.started: return False
        reply = Response(status=exc.status, text=exc.text, headers=exc.headers)
    except asyncio.CancelledError:
        raise
    except Exception:
        if request.started: return False
        reply = Response(status=500, text="internal error")
    if isinstance(reply, WebSocketResponse): return False
    if not reply.prepared:
        body = reply.body if isinstance(reply, Response) else b""
        _keep(request, reply.headers)
        reply.headers.set("Content-Length", str(len(body)))
        await _send(request.transport, reply.status, reply.headers, body)
        request.started = True
    elif not reply.eof and not request.transport.is_closing():
        await reply.write_eof()
    if request.transport.is_closing() or not request.keep: return False
    if request._body is None and request._body_mode != "empty":
        # Handler never read the entity (typical: 401 before await request.read()). Close the socket
        # instead of buffering the rest — that is the unauth-body defense.
        return False
    return "close" not in (reply.headers.get("Connection") or "").lower()
def _keep(request: Request, headers: CIHeaders) -> None:
    if not request.keep or "close" in (headers.get("Connection") or "").lower():
        headers.set("Connection", "close")
        request.keep = False
    else:
        headers.set("Connection", "keep-alive")
def _reason(status: int) -> str:
    reasons = {101: "Switching Protocols", 200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
               404: "Not Found", 405: "Method Not Allowed", 500: "Internal Server Error"}
    return reasons.get(status, "OK" if status < 400 else "Error")
def _frame(start: str, headers: CIHeaders, body: bytes) -> bytes:
    rows = [start, *(f"{name}: {value}" for name, value in headers.pairs())]
    return ("\r\n".join(rows) + "\r\n\r\n").encode("iso-8859-1", "replace") + body
async def _send(writer: asyncio.StreamWriter, status: int, headers: CIHeaders, body: bytes | None) -> None:
    writer.write(_frame(f"HTTP/1.1 {status} {_reason(status)}", headers, body or b""))
    await writer.drain()
def _close(writer: asyncio.StreamWriter) -> None:
    if not writer.is_closing():
        writer.close()
class ClientResponse:
    def __init__(self, session: ClientSession, io: _IO, status: int, headers: CIHeaders, body: ClientBody) -> None:
        self._session, self._io, self.status, self.headers, self.content = session, io, status, headers, body
        self._json: object = None
        self._raw: bytes | None = None
        self._json_set = self._released = False
    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    async def read(self) -> bytes:
        # Callers (music) read() to test emptiness, then json() the same body.
        if self._raw is None:
            self._raw = await self.content.read()
        return self._raw
    async def text(self) -> str:
        return (await self.read()).decode("utf-8", "replace")
    async def json(self, *, content_type: str | None = "application/json") -> Json:
        if not self._json_set:
            if content_type is not None and not self.content_type.startswith(content_type):
                raise ClientError(f"unexpected content type {self.content_type}")
            self._json, self._json_set = cast(Json, _loads(await self.read())), True
        return cast(Json, self._json)
    def raise_for_status(self) -> None:
        if self.status >= 400: raise ClientError(f"HTTP {self.status}")
    async def release(self) -> None:
        if not self._released:
            self._released = True
            self._session._live.discard(self)
            _close(self._io.writer)
    async def __aenter__(self) -> ClientResponse:
        return self
    async def __aexit__(self, *_exc: object) -> None:
        await self.release()
class _Releasable(Protocol):
    async def release(self) -> None: ...
_R = TypeVar("_R", bound=_Releasable)
class _Pending(Generic[_R]):
    def __init__(self, factory: Callable[[], Awaitable[_R]]) -> None:
        self._factory = factory
        self._task: asyncio.Task[_R] | None = None
    def _start(self) -> asyncio.Task[_R]:
        if self._task is None:
            self._task = asyncio.ensure_future(self._factory())
        return self._task
    def __await__(self) -> Generator[object, None, _R]:
        return cast(Generator[object, None, _R], self._start().__await__())
    async def __aenter__(self) -> _R:
        return await self._start()
    async def __aexit__(self, *_exc: object) -> None:
        if self._task is None: return
        try:
            await (await self._task).release()
        except Exception:
            return
class ClientWebSocket(WebSocket):
    def __init__(self, io: _IO, session: ClientSession, *, max_size: int, heartbeat: float | None,
                 autoclose: bool, pending: bytes) -> None:
        super().__init__(io.reader, io.writer, role="client", max_size=max_size, heartbeat=heartbeat,
                         autoclose=autoclose, pending=pending)
        self._session = session
    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._session._sockets.discard(self)
        await super().close(code, reason)
    async def release(self) -> None:
        await self.close()
ClientWebSocketResponse = ClientWebSocket
_D = FormData | bytes | str | None
class ClientSession:
    def __init__(self) -> None:
        self._live: set[ClientResponse] = set()
        self._sockets: set[ClientWebSocket] = set()
        self._closed = False
    async def __aenter__(self) -> ClientSession:
        return self
    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
    def request(self, method: str, url: str, *, headers: _H = None, params: _P = None, json: object = None,
                data: _D = None, timeout: ClientTimeout | None = None, ssl: _S = None) -> _Pending[ClientResponse]:
        return _Pending(lambda: self._exchange(method, url, headers, params, json, data, timeout, ssl))
    def get(self, url: str, *, headers: _H = None, params: _P = None, timeout: ClientTimeout | None = None,
            ssl: _S = None) -> _Pending[ClientResponse]:
        return self.request("GET", url, headers=headers, params=params, timeout=timeout, ssl=ssl)
    def post(self, url: str, *, headers: _H = None, params: _P = None, json: object = None, data: _D = None,
             timeout: ClientTimeout | None = None, ssl: _S = None) -> _Pending[ClientResponse]:
        return self.request("POST", url, headers=headers, params=params, json=json, data=data, timeout=timeout, ssl=ssl)
    def put(self, url: str, *, headers: _H = None, params: _P = None, json: object = None, data: _D = None,
            timeout: ClientTimeout | None = None, ssl: _S = None) -> _Pending[ClientResponse]:
        return self.request("PUT", url, headers=headers, params=params, json=json, data=data, timeout=timeout, ssl=ssl)
    def delete(self, url: str, *, headers: _H = None, params: _P = None, json: object = None, data: _D = None,
               timeout: ClientTimeout | None = None, ssl: _S = None) -> _Pending[ClientResponse]:
        return self.request("DELETE", url, headers=headers, params=params, json=json, data=data,
                            timeout=timeout, ssl=ssl)
    def ws_connect(self, url: str, *, headers: _H = None, heartbeat: float | None = None, max_msg_size: int = 1 << 20,
                   ssl: _S = None, autoclose: bool = True) -> _Pending[ClientWebSocket]:
        return _Pending(lambda: self._websocket(url, headers, heartbeat, max_msg_size, ssl, autoclose))
    async def close(self) -> None:
        if self._closed: return
        self._closed = True
        for resp in list(self._live):
            await resp.release()
        for ws in list(self._sockets):
            await ws.close()
        self._sockets.clear()
    async def _open(self, host: str, port: int, ctx: sslmod.SSLContext | None, clock: _Clock) -> _IO:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if ctx is not None else None),
                clock.connect_limit(),
            )
        except TimeoutError:
            raise
        except OSError as exc:
            raise ClientError(exc.strerror or str(exc)) from exc
        return _IO(reader, writer)
    async def _exchange(self, method: str, url: str, headers: _H, params: _P, payload: object, data: _D,
                        timeout: ClientTimeout | None, ssl: _S) -> ClientResponse:
        if self._closed: raise ClientError("closed")
        parts, host, port, tls = _dest(url, ("http", "https"))
        clock, hdrs = _Clock(timeout), CIHeaders(headers)
        body = _entity(method.upper(), hdrs, payload, data)
        hdrs.set("Host", _host_value(host, port, tls))
        hdrs.set("Connection", "keep-alive")
        io = await self._open(host, port, _client_ssl(tls, ssl), clock)
        try:
            io.writer.write(_frame(f"{method.upper()} {_target(parts, params)} HTTP/1.1", hdrs, body))
            await asyncio.wait_for(io.writer.drain(), clock.budget())
            lines = await _read_head(io, clock)
            if not lines: raise ClientError("empty response")
            status, got = _status(lines)
            mode, length = _body_mode(status, got)
            resp = ClientResponse(self, io, status, got, ClientBody(io, clock, mode, length))
        except BaseException:
            _close(io.writer)
            raise
        self._live.add(resp)
        return resp
    async def _websocket(self, url: str, headers: _H, heartbeat: float | None, max_msg_size: int, ssl: _S,
                         autoclose: bool) -> ClientWebSocket:
        if self._closed: raise ClientError("closed")
        parts, host, port, tls = _dest(url, ("ws", "wss", "http", "https"))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        hdrs = CIHeaders(headers)
        hdrs.set("Host", _host_value(host, port, tls))
        for name, value in (("Upgrade", "websocket"), ("Connection", "Upgrade"), ("Sec-WebSocket-Key", key),
                            ("Sec-WebSocket-Version", "13")):
            hdrs.set(name, value)
        hs = _Clock(ClientTimeout(sock_connect=10.0, sock_read=10.0, total=15.0))  # handshake
        io = await self._open(host, port, _client_ssl(tls, ssl), hs)
        try:
            io.writer.write(_frame(f"GET {_target(parts, None)} HTTP/1.1", hdrs, b""))
            await asyncio.wait_for(io.writer.drain(), hs.budget())
            lines = await _read_head(io, hs)
            if not lines: raise ClientError("empty response")
            status, got = _status(lines)
            accept_ok = got.get("Sec-WebSocket-Accept") == accept_key(key)
            if status != 101 or not accept_ok or got.get("Sec-WebSocket-Extensions"):
                raise ClientError(f"websocket HTTP {status}")
        except BaseException:
            _close(io.writer)
            raise
        pending = bytes(io.buf)
        io.buf.clear()
        ws = ClientWebSocket(io, self, max_size=max_msg_size, heartbeat=heartbeat, autoclose=autoclose, pending=pending)
        self._sockets.add(ws)
        return ws
def _entity(method: str, headers: CIHeaders, payload: object, data: _D) -> bytes:
    if payload is not None and data is not None: raise ClientError("json and data")
    if payload is not None:
        raw = _json.dumps(payload).encode()
        if headers.get("Content-Type") is None:
            headers.set("Content-Type", "application/json")
    elif isinstance(data, FormData):
        raw, ctype = data.encode()
        if headers.get("Content-Type") is None:
            headers.set("Content-Type", ctype)
    elif isinstance(data, str):
        raw = data.encode()
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif data is None:
        raw = b""
    else: raise ClientError("unsupported body")
    if raw or method in ("POST", "PUT"):
        headers.set("Content-Length", str(len(raw)))
    return raw
def _target(parts: SplitResult, params: _P) -> str:
    path = parts.path or "/"
    if params is None: return path + (("?" + parts.query) if parts.query else "")
    pairs = [*parse_qsl(parts.query, keep_blank_values=True), *((str(k), str(v)) for k, v in params.items())]
    query = urlencode(pairs, quote_via=quote)
    return path + (("?" + query) if query else "")
def _dest(url: str, schemes: tuple[str, ...]) -> tuple[SplitResult, str, int, bool]:
    parts = urlsplit(url)
    if parts.scheme not in schemes or not parts.hostname: raise ClientError(f"unsupported url {url}")
    tls = parts.scheme in ("https", "wss")
    return parts, parts.hostname, parts.port if parts.port is not None else (443 if tls else 80), tls
def _host_value(host: str, port: int, tls: bool) -> str:
    if port == (443 if tls else 80): return host
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
def _client_ssl(tls: bool, ssl: _S) -> sslmod.SSLContext | None:
    if not tls: return None
    return ssl if isinstance(ssl, sslmod.SSLContext) else sslmod.create_default_context()
def _status(lines: list[bytes]) -> tuple[int, CIHeaders]:
    bits = lines[0].decode("latin-1").split(" ", 2)
    if len(bits) < 2: raise ClientError("bad status")
    try:
        return int(bits[1]), _headers_from(lines[1:])
    except ValueError:
        raise ClientError("bad status") from None
def _body_mode(status: int, headers: CIHeaders) -> tuple[str, int]:
    # RFC 7230: no TE and no Content-Length -> body ends when the connection closes.
    if status < 200 or status in (204, 304): return "empty", 0
    if "chunked" in (headers.get("Transfer-Encoding") or "").lower(): return "chunked", 0
    length = headers.get("Content-Length")
    if length is None: return "eof", 0
    try:
        size = int(length)
    except ValueError:
        raise ClientError("bad length") from None
    if size < 0: raise ClientError("bad length")
    return "length", size
# Default host is loopback. The bridge sets SKWOOP_HOST=0.0.0.0 for LAN pods.
def run_app(app: Application, *, host: str = "127.0.0.1", port: int = 8080, **_extra: object) -> None:

    async def _go() -> None:
        runner = AppRunner(app)
        await runner.setup()
        await TCPSite(runner, host, port).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
    asyncio.run(_go())
class web:
    json_response = staticmethod(json_response)
    @staticmethod
    def get(path: str, handler: Handler) -> RouteDef:
        return RouteDef("GET", path, handler)
    @staticmethod
    def post(path: str, handler: Handler) -> RouteDef:
        return RouteDef("POST", path, handler)
    @staticmethod
    def put(path: str, handler: Handler) -> RouteDef:
        return RouteDef("PUT", path, handler)
    @staticmethod
    def delete(path: str, handler: Handler) -> RouteDef:
        return RouteDef("DELETE", path, handler)
    Request, Response, StreamResponse, FileResponse = Request, Response, StreamResponse, FileResponse
    WebSocketResponse, Application, AppRunner, TCPSite = WebSocketResponse, Application, AppRunner, TCPSite
    AppKey, RouteDef, HTTPNotFound, WSMsgType = AppKey, RouteDef, HTTPNotFound, WSMsgType
    run_app = staticmethod(run_app)
