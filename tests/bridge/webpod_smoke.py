"""One browser turn against the web pod socket (hello, listen, 16 kHz PCM, listen stop) on lean_ws.

The live tree ships without tests/, so this client lives here and is imported as webpod_smoke.
Do not add webpod/smoke.py. Same signatures the A/B harness uses:

    TurnResult = await one_turn(url, ca, device, key, read_pcm16k(wav), timeout_s)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lean_http import ClientSession
from lean_ws import WSMsgType

from webpod.server import IN_RATE, OUT_RATE


@dataclass(frozen=True)
class Heard:
    """One thing the page got, at t seconds after listen stop: a sentence or audio bytes."""

    t: float
    kind: str  # "say" | "audio"
    text: str = ""
    filler: bool = False
    nbytes: int = 0


@dataclass
class TurnResult:
    device: str = ""
    heard: str = ""
    said: list[str] = field(default_factory=list)
    pcm: bytearray = field(default_factory=bytearray)
    first_audio_s: float | None = None
    first_reply_s: float | None = None
    reply_gaps_s: float = 0.0
    reply_gap_max_s: float = 0.0
    reply_sentences: int = 0
    timeline: list[Heard] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def read_pcm16k(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (IN_RATE, 1, 2):
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit, got {w.getframerate()} Hz x{w.getnchannels()}")

        raw = w.readframes(w.getnframes())

        return raw if isinstance(raw, bytes) else bytes(raw)


async def one_turn(url: str, ca: Path | None, device: str, key: str, pcm: bytes, timeout_s: float) -> TurnResult:
    ctx: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca)) if ca else True
    out = TurnResult()
    play_end = 0.0
    async with ClientSession() as http, http.ws_connect(url, ssl=ctx) as ws:
        await ws.send_str(json.dumps({"type": "hello", "device": device, "key": key}))
        await ws.send_str(json.dumps({"type": "listen", "state": "start", "mode": "manual"}))
        for off in range(0, len(pcm), 1920):
            await ws.send_bytes(pcm[off:off + 1920])
            await asyncio.sleep(0.06)

        await ws.send_str(json.dumps({"type": "listen", "state": "stop", "mode": "manual"}))
        sent_at = time.monotonic()
        deadline = sent_at + timeout_s
        while time.monotonic() < deadline:
            try:
                msg = await ws.receive(timeout=deadline - time.monotonic())
            except TimeoutError:
                out.errors.append("timed out")

                break

            now = time.monotonic() - sent_at
            if msg.type == WSMsgType.BINARY and isinstance(msg.data, bytes):
                if out.first_audio_s is None:
                    out.first_audio_s = now

                last_say = next((h for h in reversed(out.timeline) if h.kind == "say"), None)
                if out.first_reply_s is None and last_say is not None and not last_say.filler:
                    out.first_reply_s = now

                if out.first_reply_s is not None:
                    if play_end and now > play_end:
                        gap = now - play_end
                        out.reply_gaps_s += gap
                        out.reply_gap_max_s = max(out.reply_gap_max_s, gap)

                    play_end = max(play_end, now) + len(msg.data) / 2 / OUT_RATE

                out.pcm += msg.data
                out.timeline.append(Heard(now, "audio", nbytes=len(msg.data)))

                continue

            if msg.type != WSMsgType.TEXT or not isinstance(msg.data, str):
                break

            event = json.loads(msg.data)
            if not isinstance(event, dict):
                break

            kind = event.get("type")
            if kind == "webpod":
                if event.get("error"):
                    out.errors.append(str(event["error"]))

                    break

                out.device = str(event.get("device", ""))
            elif kind == "stt":
                out.heard = str(event.get("text", ""))
            elif kind == "tts" and event.get("state") == "sentence_start":
                filler = event.get("filler") is True
                text = str(event.get("text", ""))
                out.said.append(("~ " if filler else "") + text)
                out.reply_sentences += 0 if filler else 1
                out.timeline.append(Heard(now, "say", text, filler))
            elif kind == "tts" and event.get("state") == "stop":
                break

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="wss://127.0.0.1:8443/pod")
    ap.add_argument("--ca", type=Path, default=Path(os.path.expanduser("~/.skwoop/webpod-tls/ca.crt")))
    ap.add_argument("--device", default="smoke")
    ap.add_argument("--key", default=os.environ.get("WEBPOD_KEY", ""))
    ap.add_argument("--wav", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--timeline", type=Path)
    args = ap.parse_args()
    res = asyncio.run(one_turn(
        args.url, args.ca if args.url.startswith("wss:") else None, args.device, args.key,
        read_pcm16k(args.wav), args.timeout))
    with wave.open(str(args.out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(bytes(res.pcm))

    seconds = len(res.pcm) / 2 / OUT_RATE
    first = f"{res.first_audio_s:.2f}s" if res.first_audio_s is not None else "-"
    reply = f"{res.first_reply_s:.2f}s" if res.first_reply_s is not None else "-"
    if args.timeline is not None:
        args.timeline.write_text(json.dumps({"heard": res.heard, "events": [asdict(h) for h in res.timeline]}))

    print(json.dumps({
        "device": res.device, "heard": res.heard, "said": res.said, "reply_s": round(seconds, 2),
        "first_audio_after_stop": first, "first_reply_after_stop": reply,
        "reply_gaps": f"{res.reply_gaps_s:.3f}s", "reply_gap_max": f"{res.reply_gap_max_s:.3f}s",
        "reply_sentences": res.reply_sentences, "errors": res.errors}))
    sys.exit(0 if seconds > 0.3 and not res.errors else 1)


if __name__ == "__main__":
    main()
