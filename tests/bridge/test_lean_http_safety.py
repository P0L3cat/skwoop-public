"""Mutation-resistant checks for lean_http body/auth defenses (review findings 1, 4, 6).

Each test fails if the matching safety check is deleted.
"""
from __future__ import annotations

import asyncio
import os
import resource

import pytest

from lean_http import (
    MAX_BODY,
    Application,
    AppRunner,
    ClientSession,
    Response,
    TCPSite,
    _body_mode,
    CIHeaders,
)


def _rss_mb() -> int:
    # Linux: VmRSS kB; macOS: ru_maxrss is bytes on some, KB on others — prefer /proc.
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) // 1024


def test_unauth_chunked_body_never_buffers_megabytes() -> None:
    asyncio.run(_unauth_chunked_body_never_buffers_megabytes())


async def _unauth_chunked_body_never_buffers_megabytes() -> None:
    """Auth runs before read(); unread body closes the socket — RSS stays bounded (review #1)."""
    app = Application()
    saw: list[int] = []

    async def handler(req):  # type: ignore[no-untyped-def]
        if req.headers.get("Authorization") != "Bearer ok":
            return Response(status=401, text="no")
        saw.append(len(await req.read()))
        return Response(text="ok")

    app.router.add_post("/status", handler)
    runner = AppRunner(app)
    await runner.setup()
    site = TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0]
    before = _rss_mb()
    rd, wr = await asyncio.open_connection(host, port)
    wr.write(b"POST /status HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n")
    chunk = os.urandom(1 << 20)  # 1 MiB
    closed_early = False
    for _ in range(80):  # would be 80 MiB if buffered
        try:
            wr.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
            await wr.drain()
        except (ConnectionError, BrokenPipeError, OSError):
            closed_early = True
            break
    after = _rss_mb()
    wr.close()
    await runner.cleanup()
    assert saw == []
    assert closed_early or after - before < 16  # never approached 80 MiB
    assert after - before < 32


def test_max_body_is_hard_cap_not_32mib() -> None:
    assert MAX_BODY <= 4 * 1024 * 1024


def test_content_length_over_cap_rejected_before_read() -> None:
    from lean_http import _request_body_mode, ClientError as CE
    with pytest.raises(CE, match="too large"):
        _request_body_mode(CIHeaders({"Content-Length": str(MAX_BODY + 1)}), MAX_BODY)


def test_missing_content_length_is_eof_not_empty() -> None:
    """review #6: a 200 without Content-Length is read to connection close, not treated as empty."""
    mode, length = _body_mode(200, CIHeaders({"Content-Type": "application/json"}))
    assert mode == "eof" and length == 0


def test_client_reads_body_without_content_length() -> None:
    asyncio.run(_client_reads_body_without_content_length())


async def _client_reads_body_without_content_length() -> None:
    """stdlib server, no Content-Length: client must still return the JSON body."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"state":"on"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a: object) -> None:
            return

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        async with ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/x") as r:
                assert r.status == 200
                assert await r.read() == b'{"state":"on"}'
    finally:
        srv.shutdown()
