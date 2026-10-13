"""RFC 6455 frames and one asyncio socket. No extensions. Client frames are masked."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
from dataclasses import dataclass
from enum import Enum

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA
_OPS = frozenset({OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG})


class ProtocolError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class WSMsgType(Enum):
    TEXT, BINARY, ERROR, CLOSED = 1, 2, 3, 4
    CLOSE, PING, PONG = 8, 9, 10


@dataclass(frozen=True)
class WSMessage:
    type: WSMsgType
    data: str | bytes
    code: int = 0


@dataclass(frozen=True)
class Frame:
    fin: bool
    opcode: int
    payload: bytes
    masked: bool


def accept_key(sec_key: str) -> str:
    digest = hashlib.sha1((sec_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask_payload(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[i & 3] for i, byte in enumerate(data))


def encode_frame(opcode: int, payload: bytes, *, mask_key: bytes | None, fin: bool = True) -> bytes:
    head = bytearray(((0x80 if fin else 0) | (opcode & 0x0F),))
    bit = 0x80 if mask_key is not None else 0
    size = len(payload)
    if size < 126:
        head.append(bit | size)
    elif size < 65536:
        head.append(bit | 126)
        head += struct.pack("!H", size)
    else:
        head.append(bit | 127)
        head += struct.pack("!Q", size)
    if mask_key is not None:
        head += mask_key
        payload = mask_payload(payload, mask_key)
    return bytes(head) + payload


def decode_frame(buf: bytes | bytearray, max_size: int, role: str) -> tuple[Frame, int] | None:
    """One frame, or None if the buffer is short. Raises ProtocolError once the header is invalid."""
    if len(buf) < 2:
        return None
    first, second = buf[0], buf[1]
    if first & 0x70:
        raise ProtocolError(1002, "extension")
    fin = (first & 0x80) != 0
    opcode = first & 0x0F
    masked = (second & 0x80) != 0
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buf) < 4:
            return None
        length = struct.unpack_from("!H", buf, 2)[0]
        offset = 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length = struct.unpack_from("!Q", buf, 2)[0]
        if length > 0x7FFFFFFFFFFFFFFF:
            raise ProtocolError(1002, "length")
        offset = 10
    if opcode not in _OPS:
        raise ProtocolError(1002, "opcode")
    if opcode >= 8 and (not fin or length > 125):
        raise ProtocolError(1002, "control")
    if length > max_size:
        raise ProtocolError(1009, "too big")
    if role == "server" and not masked:
        raise ProtocolError(1002, "unmasked")
    if role == "client" and masked:
        raise ProtocolError(1002, "masked server")
    if masked:
        if len(buf) < offset + 4:
            return None
        key = bytes(buf[offset:offset + 4])
        offset += 4
    else:
        key = b""
    if len(buf) < offset + length:
        return None
    payload = bytes(buf[offset:offset + length])
    if key:
        payload = mask_payload(payload, key)
    return Frame(fin, opcode, payload, masked), offset + length


def _close_parts(payload: bytes) -> tuple[int, str]:
    if not payload:
        return 1000, ""
    if len(payload) < 2:
        raise ProtocolError(1002, "close")
    code = struct.unpack("!H", payload[:2])[0]
    try:
        reason = payload[2:].decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError(1002, "close") from None
    if code < 1000 or code in (1004, 1005, 1006, 1015) or 1016 <= code <= 2999 or code > 4999:
        raise ProtocolError(1002, "close code")
    return code, reason


class WebSocket:
    """One open socket. role is 'client' (mask) or 'server' (require mask)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, role: str,
                 max_size: int = 1 << 20, heartbeat: float | None = None, autoclose: bool = True,
                 pending: bytes = b"") -> None:
        self.role = role
        self.max_size = max_size
        self.heartbeat = heartbeat
        self.autoclose = autoclose
        self._reader = reader
        self._writer = writer
        self._pending = bytearray(pending)
        self._lock = asyncio.Lock()
        self._frag: bytearray | None = None
        self._frag_op = 0
        self._dead = False
        self._sent_close = False
        self._peer_closed = False
        self._tcp = False
        self._reading = False
        self._close_task: asyncio.Task[None] | None = None
        self._ping: asyncio.Task[None] | None = None
        self._awaiting_pong = False
        self._pong = asyncio.Event()
        if heartbeat is not None and heartbeat > 0:
            self._ping = asyncio.create_task(self._beat())

    @property
    def closed(self) -> bool:
        return self._dead

    async def send_str(self, text: str) -> None:
        if self._dead:
            raise ConnectionError("closed")
        await self._send(OP_TEXT, text.encode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        if self._dead:
            raise ConnectionError("closed")
        await self._send(OP_BINARY, data)

    async def ping(self, data: bytes = b"") -> None:
        if self._dead:
            raise ConnectionError("closed")
        await self._send(OP_PING, data)

    async def receive(self, timeout: float | None = None) -> WSMessage:
        if self._dead and self._peer_closed:
            return WSMessage(WSMsgType.CLOSED, b"")
        # close() must not start a second read while this one is waiting
        self._reading = True
        try:
            return await self._receive(timeout)
        finally:
            self._reading = False

    async def _receive(self, timeout: float | None) -> WSMessage:
        try:
            if timeout is None:
                return await self._next()
            return await asyncio.wait_for(self._next(), timeout)
        except TimeoutError:
            raise
        except ProtocolError as exc:
            await self._fail(exc.code, exc.reason)
            return WSMessage(WSMsgType.CLOSE, exc.reason, exc.code)
        except (EOFError, ConnectionError, OSError):
            self._dead = True
            self._peer_closed = True
            await self._stop_ping()
            return WSMessage(WSMsgType.CLOSED, b"")

    def __aiter__(self) -> WebSocket:
        return self

    async def __anext__(self) -> WSMessage:
        if self._peer_closed and self._dead:
            raise StopAsyncIteration
        msg = await self.receive()
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
            self._dead = True
            self._peer_closed = True
        return msg

    async def close(self, code: int = 1000, reason: str = "") -> None:
        # the relay and ClientSession.close() both close the same socket
        task = self._close_task
        if task is None:
            task = asyncio.create_task(self._finish_close(code, reason))
            self._close_task = task
        await task

    async def _finish_close(self, code: int, reason: str) -> None:
        if self._tcp:
            return
        self._dead = True
        await self._stop_ping()
        if not self._sent_close:
            self._sent_close = True
            body = struct.pack("!H", code) + reason.encode("utf-8")[:123]
            try:
                await self._send(OP_CLOSE, body)
            except (ConnectionError, OSError, RuntimeError):
                pass
        reading = self._reading
        if not self._peer_closed and not reading:
            try:
                await asyncio.wait_for(self._until_peer(), 10)
            except (TimeoutError, EOFError, ConnectionError, OSError, ProtocolError, RuntimeError):
                pass
        self._tcp = True
        transport = self._writer.transport
        self._writer.close()
        if reading and transport is not None:
            transport.abort()

    async def release(self) -> None:
        await self.close()

    async def _beat(self) -> None:
        """Ping on the heartbeat interval; close if the peer skips a pong."""
        assert self.heartbeat is not None
        try:
            while not self._dead:
                await asyncio.sleep(self.heartbeat)
                if self._dead:
                    return
                self._pong.clear()
                self._awaiting_pong = True
                await self._send(OP_PING, b"")
                try:
                    await asyncio.wait_for(self._pong.wait(), self.heartbeat)
                except TimeoutError:
                    await self.close(1001, "pong timeout")
                    return
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, RuntimeError):
            return

    async def _stop_ping(self) -> None:
        task = self._ping
        self._ping = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _send(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        key = os.urandom(4) if self.role == "client" else None
        data = encode_frame(opcode, payload, mask_key=key, fin=fin)
        async with self._lock:
            self._writer.write(data)
            await self._writer.drain()

    async def _fail(self, code: int, reason: str) -> None:
        self._dead = True
        await self._stop_ping()
        if not self._sent_close:
            self._sent_close = True
            try:
                await self._send(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:123])
            except (ConnectionError, OSError, RuntimeError):
                pass
        self._writer.close()

    async def _read_frame(self) -> Frame:
        while True:
            found = decode_frame(self._pending, self.max_size, self.role)
            if found is not None:
                frame, used = found
                del self._pending[:used]
                return frame
            chunk = await self._reader.read(65536)
            if not chunk:
                raise EOFError
            self._pending += chunk

    async def _next(self) -> WSMessage:
        while True:
            frame = await self._read_frame()
            if frame.opcode == OP_PING:
                await self._send(OP_PONG, frame.payload)
                continue
            if frame.opcode == OP_PONG:
                self._awaiting_pong = False
                self._pong.set()
                continue
            if frame.opcode == OP_CLOSE:
                return await self._on_close(frame)
            if frame.opcode == OP_CONT:
                if self._frag is None:
                    raise ProtocolError(1002, "continuation")
                self._grow(frame.payload)
                if frame.fin:
                    return self._end_frag()
                continue
            if self._frag is not None:
                raise ProtocolError(1002, "fragment")
            if not frame.fin:
                self._frag = bytearray()
                self._frag_op = frame.opcode
                self._grow(frame.payload)
                continue
            return self._message(frame.opcode, frame.payload)

    def _grow(self, payload: bytes) -> None:
        assert self._frag is not None
        if len(self._frag) + len(payload) > self.max_size:
            raise ProtocolError(1009, "too big")
        self._frag += payload

    def _end_frag(self) -> WSMessage:
        payload = bytes(self._frag or b"")
        opcode = self._frag_op
        self._frag = None
        return self._message(opcode, payload)

    def _message(self, opcode: int, payload: bytes) -> WSMessage:
        if opcode == OP_TEXT:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                raise ProtocolError(1007, "utf-8") from None
            return WSMessage(WSMsgType.TEXT, text)
        return WSMessage(WSMsgType.BINARY, payload)

    async def _on_close(self, frame: Frame) -> WSMessage:
        code, reason = _close_parts(frame.payload)
        self._peer_closed = True
        if self.autoclose and not self._sent_close:
            self._sent_close = True
            body = frame.payload or struct.pack("!H", 1000)
            try:
                await self._send(OP_CLOSE, body)
            except (ConnectionError, OSError, RuntimeError):
                pass
            self._dead = True
            await self._stop_ping()
        return WSMessage(WSMsgType.CLOSE, reason, code)

    async def _until_peer(self) -> None:
        while not self._peer_closed:
            frame = await self._read_frame()
            if frame.opcode == OP_CLOSE:
                self._peer_closed = True
                return
            if frame.opcode == OP_PING:
                await self._send(OP_PONG, frame.payload)
