"""RFC 6455 framing checks for lean_ws. Sync pytest; async via asyncio.run."""
from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from lean_ws import (
    OP_BINARY,
    OP_CLOSE,
    OP_CONT,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    ProtocolError,
    WSMsgType,
    WebSocket,
    accept_key,
    decode_frame,
    encode_frame,
    mask_payload,
)

_KEY = b"abcd"
_MAX = 1 << 20


def test_mask_roundtrip() -> None:
    data = bytes(range(256)) + b"websocket"
    masked = mask_payload(data, _KEY)
    assert masked != data
    assert mask_payload(masked, _KEY) == data
    assert mask_payload(b"", _KEY) == b""


def test_encode_decode_lengths() -> None:
    payloads = (b"hi", b"x" * 200, b"\x00\xff" * 35000)
    assert len(payloads[2]) == 70000
    for payload in payloads:
        raw = encode_frame(OP_BINARY, payload, mask_key=_KEY)
        kind = raw[1] & 0x7F
        if len(payload) < 126:
            assert kind == len(payload)
        elif len(payload) < 65536:
            assert kind == 126
        else:
            assert kind == 127
        found = decode_frame(raw, _MAX, "server")
        assert found is not None
        frame, used = found
        assert used == len(raw)
        assert frame.fin is True
        assert frame.opcode == OP_BINARY
        assert frame.masked is True
        assert frame.payload == payload
        plain = encode_frame(OP_BINARY, payload, mask_key=None)
        found_plain = decode_frame(plain, _MAX, "client")
        assert found_plain is not None
        assert found_plain[0].payload == payload
        assert found_plain[0].masked is False


def test_unmasked_client_frame_rejected() -> None:
    raw = encode_frame(OP_TEXT, b"hi", mask_key=None)

    with pytest.raises(ProtocolError) as exc:
        decode_frame(raw, _MAX, "server")
    assert exc.value.code == 1002

    async def run() -> None:
        ws, writer = await _server()
        writer.write(raw)
        await writer.drain()
        msg = await asyncio.wait_for(ws.receive(), 2)
        assert msg.type == WSMsgType.CLOSE
        assert msg.code == 1002
        await _drop(ws, writer)

    asyncio.run(run())


def test_close_code_and_reason() -> None:
    async def run() -> None:
        ws, writer = await _server()
        body = struct.pack("!H", 1001) + b"going away"
        writer.write(encode_frame(OP_CLOSE, body, mask_key=_KEY))
        await writer.drain()
        msg = await asyncio.wait_for(ws.receive(), 2)
        assert msg.type == WSMsgType.CLOSE
        assert msg.code == 1001
        assert msg.data == "going away"
        empty = encode_frame(OP_CLOSE, b"", mask_key=_KEY)
        # Peer already closed; a second socket covers the empty-body default.
        await _drop(ws, writer)
        ws2, writer2 = await _server()
        writer2.write(empty)
        await writer2.drain()
        msg2 = await asyncio.wait_for(ws2.receive(), 2)
        assert msg2.code == 1000
        assert msg2.data == ""
        await _drop(ws2, writer2)

    asyncio.run(run())


def test_ping_pong() -> None:
    async def run() -> None:
        ws, reader, writer = await _ends()
        pending = asyncio.create_task(ws.receive())
        writer.write(encode_frame(OP_PING, b"xyz", mask_key=_KEY))
        await writer.drain()
        buf = bytearray()
        frame = None
        while frame is None:
            chunk = await asyncio.wait_for(reader.read(256), 2)
            assert chunk
            buf += chunk
            found = decode_frame(buf, _MAX, "client")
            if found is not None:
                frame, used = found
                del buf[:used]
        assert frame.opcode == OP_PONG
        assert frame.payload == b"xyz"
        assert frame.masked is False
        writer.write(encode_frame(OP_TEXT, b"done", mask_key=_KEY))
        await writer.drain()
        msg = await asyncio.wait_for(pending, 2)
        assert msg.type == WSMsgType.TEXT
        assert msg.data == "done"
        await _drop(ws, writer)

    asyncio.run(run())


def test_fragmented_text() -> None:
    async def run() -> None:
        ws, writer = await _server()
        writer.write(encode_frame(OP_TEXT, b"caf\xc3", mask_key=_KEY, fin=False))
        writer.write(encode_frame(OP_CONT, b"\xa9", mask_key=_KEY, fin=True))
        await writer.drain()
        msg = await asyncio.wait_for(ws.receive(), 2)
        assert msg.type == WSMsgType.TEXT
        assert msg.data == "caf\u00e9"
        await _drop(ws, writer)

    asyncio.run(run())


def test_oversize_1009() -> None:
    raw = encode_frame(OP_BINARY, b"x" * 100, mask_key=_KEY)
    with pytest.raises(ProtocolError) as exc:
        decode_frame(raw, 50, "server")
    assert exc.value.code == 1009

    async def run() -> None:
        ws, writer = await _server(max_size=50)
        writer.write(raw)
        await writer.drain()
        msg = await asyncio.wait_for(ws.receive(), 2)
        assert msg.type == WSMsgType.CLOSE
        assert msg.code == 1009
        await _drop(ws, writer)

    asyncio.run(run())


def test_accept_key_rfc_example() -> None:
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


async def _ends() -> tuple[WebSocket, asyncio.StreamReader, asyncio.StreamWriter]:
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    server_reader, server_writer = await asyncio.open_connection(sock=left)
    client_reader, client_writer = await asyncio.open_connection(sock=right)
    return WebSocket(server_reader, server_writer, role="server"), client_reader, client_writer


async def _server(max_size: int = _MAX) -> tuple[WebSocket, asyncio.StreamWriter]:
    ws, _reader, writer = await _ends()
    ws.max_size = max_size
    return ws, writer


async def _drop(ws: WebSocket, writer: asyncio.StreamWriter) -> None:
    ws._dead = True
    await ws._stop_ping()
    writer.close()
    ws._writer.close()
    await asyncio.gather(writer.wait_closed(), ws._writer.wait_closed(), return_exceptions=True)
