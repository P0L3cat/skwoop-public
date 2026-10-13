"""Local HTTP/1.1 coverage for lean_http. Binds 127.0.0.1 only."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from lean_http import (
    Application,
    AppRunner,
    ClientError,
    ClientSession,
    ClientTimeout,
    FormData,
    Request,
    Response,
    StreamResponse,
    TCPSite,
    WSMsgType,
    WebSocketResponse,
    json_response,
    web,
)


@asynccontextmanager
async def _serving(app: Application) -> AsyncIterator[str]:
    runner = AppRunner(app)
    await runner.setup()
    await TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@asynccontextmanager
async def _session() -> AsyncIterator[ClientSession]:
    session = ClientSession()
    try:
        yield session
    finally:
        await session.close()


async def _read_raw(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
    status_line = await asyncio.wait_for(reader.readline(), 2)
    status = int(status_line.split()[1])
    headers: dict[str, str] = {}
    while True:
        raw = await asyncio.wait_for(reader.readline(), 2)
        if raw in (b"\r\n", b""):
            break
        key, value = raw.decode("latin-1").split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = await asyncio.wait_for(reader.readexactly(length), 2) if length else b""
    return status, headers, body


def test_route_query_json() -> None:
    asyncio.run(_route_query_json())


async def _route_query_json() -> None:
    app = Application()

    async def echo(request: Request) -> Response:
        body = await request.json()
        assert isinstance(body, dict)
        return json_response({
            "x": request.query.get("x"),
            "y": request.query["y"],
            "n": body.get("n"),
            "id": request.match_info["id"],
        })

    app.add_routes([web.get("/item/{id}", echo), web.post("/item/{id}", echo)])
    async with _serving(app) as root, _session() as session:
        async with session.post(root + "/item/abc", params={"x": "1", "y": "hi"}, json={"n": 3}) as resp:
            resp.raise_for_status()
            got = await resp.json()
        assert got == {"x": "1", "y": "hi", "n": 3, "id": "abc"}


def test_status_401_404_405() -> None:
    asyncio.run(_status_401_404_405())


async def _status_401_404_405() -> None:
    app = Application()

    async def secret(request: Request) -> Response:
        if request.headers.get("Authorization") != "ok":
            return Response(status=401, text="unauthorized")
        return Response(text="yes")

    app.router.add_get("/secret", secret)
    async with _serving(app) as root, _session() as session:
        async with session.get(root + "/secret") as resp:
            assert resp.status == 401
            with pytest.raises(ClientError):
                resp.raise_for_status()
        async with session.get(root + "/secret", headers={"Authorization": "ok"}) as resp:
            assert resp.status == 200
            assert await resp.text() == "yes"
        async with session.get(root + "/missing") as resp:
            assert resp.status == 404
        async with session.post(root + "/secret", data=b"") as resp:
            assert resp.status == 405
            assert "GET" in (resp.headers.get("Allow") or "")


def test_keep_alive() -> None:
    asyncio.run(_keep_alive())


async def _keep_alive() -> None:
    app = Application()

    async def ping(_request: Request) -> Response:
        return Response(text="pong")

    app.router.add_get("/ping", ping)
    async with _serving(app) as root:
        port = int(root.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            req = b"GET /ping HTTP/1.1\r\nHost: localhost\r\n\r\n"
            writer.write(req)
            await writer.drain()
            status, headers, body = await _read_raw(reader)
            assert status == 200 and body == b"pong"
            assert headers.get("connection") == "keep-alive"
            writer.write(req)
            await writer.drain()
            status2, _headers2, body2 = await _read_raw(reader)
            assert status2 == 200 and body2 == b"pong"
        finally:
            writer.close()
            await writer.wait_closed()


def test_chunked() -> None:
    asyncio.run(_chunked())


async def _chunked() -> None:
    app = Application()

    async def chunks(_request: Request) -> StreamResponse:
        resp = StreamResponse(headers={"Content-Type": "text/plain"})
        await resp.prepare(_request)
        await resp.write(b"abc")
        await resp.write(b"def")
        return resp

    app.router.add_get("/chunks", chunks)
    async with _serving(app) as root, _session() as session:
        async with session.get(root + "/chunks") as resp:
            assert resp.status == 200
            assert "chunked" in (resp.headers.get("Transfer-Encoding") or "").lower()
            parts = [part async for part in resp.content.iter_chunked(2)]
        assert b"".join(parts) == b"abcdef"


def test_sse() -> None:
    asyncio.run(_sse())


async def _sse() -> None:
    app = Application()

    async def events(_request: Request) -> StreamResponse:
        resp = StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(_request)
        await resp.write(b"data: one\n\n")
        await resp.write(b"data: two\n\n")
        return resp

    app.router.add_get("/events", events)
    async with _serving(app) as root, _session() as session:
        async with session.get(root + "/events") as resp:
            lines = [line async for line in resp.content]
        assert lines == [b"data: one\n", b"\n", b"data: two\n", b"\n"]


def test_multipart_and_form() -> None:
    asyncio.run(_multipart_and_form())


async def _multipart_and_form() -> None:
    app = Application()

    async def upload(request: Request) -> Response:
        fields: list[dict[str, str]] = []
        async for part in await request.multipart():
            chunk = await part.read_chunk()
            rest = await part.read_chunk()
            fields.append({
                "name": part.name,
                "filename": part.filename,
                "text": await part.text(),
                "chunk": chunk.decode(),
                "rest": rest.decode(),
            })
        return json_response(fields)

    app.router.add_post("/upload", upload)
    raw = (
        b"--bound\r\n"
        b'Content-Disposition: form-data; name="note"\r\n\r\n'
        b"hello\r\n"
        b"--bound\r\n"
        b'Content-Disposition: form-data; name="file"; filename="a.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        b"xyz\r\n"
        b"--bound--\r\n"
    )
    form = FormData()
    form.add_field("note", "hello")
    form.add_field("file", b"xyz", filename="a.txt", content_type="text/plain")
    async with _serving(app) as root, _session() as session:
        async with session.post(
            root + "/upload", data=raw, headers={"Content-Type": "multipart/form-data; boundary=bound"},
        ) as resp:
            parsed = await resp.json()
        assert parsed == [
            {"name": "note", "filename": "", "text": "hello", "chunk": "hello", "rest": ""},
            {"name": "file", "filename": "a.txt", "text": "xyz", "chunk": "xyz", "rest": ""},
        ]
        async with session.post(root + "/upload", data=form) as resp:
            resp.raise_for_status()
            posted = await resp.json()
        assert posted == parsed


def test_ws_echo() -> None:
    asyncio.run(_ws_echo())


async def _ws_echo() -> None:
    app = Application()

    async def echo(request: Request) -> WebSocketResponse:
        ws = WebSocketResponse()
        await ws.prepare(request)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and isinstance(msg.data, str):
                    await ws.send_str(msg.data)
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            await ws.close()
        return ws

    app.router.add_get("/ws", echo)
    async with _serving(app) as root, _session() as session:
        async with session.ws_connect(root.replace("http://", "ws://") + "/ws") as ws:
            await ws.send_str("hi")
            msg = await ws.receive()
        assert msg.type == WSMsgType.TEXT
        assert msg.data == "hi"


def test_client_timeout() -> None:
    asyncio.run(_client_timeout())


async def _client_timeout() -> None:
    app = Application()

    async def slow(_request: Request) -> Response:
        await asyncio.sleep(1)
        return Response(text="late")

    app.router.add_get("/slow", slow)
    async with _serving(app) as root, _session() as session:
        with pytest.raises(TimeoutError):
            async with session.get(root + "/slow", timeout=ClientTimeout(sock_read=0.2, total=0.2)) as resp:
                await resp.read()
