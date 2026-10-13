"""Pretend to be the pod: exercise the bridge without hardware.

  python fake_pod.py --server http://127.0.0.1:8003 --wav question.wav --out reply.wav
  python fake_pod.py --server http://<bridge-host>:8003 --say "what is the team doing"  # uses `say`
  python fake_pod.py --inbox play --out message.wav      # play the next unread voice message
  python fake_pod.py --pods 3 --wake                     # 3 pods hear one "Hi ESP": who answers?

Does the same OTA check-in + WebSocket handshake as xiaozhi firmware, streams the
wav (any rate, mono/stereo 16-bit) as 16 kHz/60 ms Opus, then saves the spoken reply.
--inbox mirrors the firmware tap: listen start (manual) to get a channel, then the inbox
command, sent MCP-wrapped like the firmware does (--wrap plain sends {"type":"inbox"}).
--wake mirrors xiaozhi's wake-word path: wake-word audio first, then listen detect, then
listen start (realtime), then the question. --device picks the fake MAC (-> room via SKWOOP_ROOMS).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import uuid

if sys.platform == "darwin":
    for _p in ("/opt/homebrew/lib", "/usr/local/lib"):
        if os.path.exists(os.path.join(_p, "libopus.dylib")):
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = _p
            break

import opus
from lean_http import ClientError, ClientSession
from lean_ws import WSMsgType

from providers import pcm_to_wav, resample, wav_to_pcm

IN_RATE, FRAME_MS = 16000, 60
DEVICE_ID = "aa:bb:cc:00:00:01"


def inbox_frame(sid: str, action: str, wrap: str = "mcp") -> str:
    """The inbox command exactly as the firmware sends it (see pod_face.cc SendInbox)."""
    if wrap == "plain":
        return json.dumps({"session_id": sid, "type": "inbox", "action": action})
    return json.dumps({"session_id": sid, "type": "mcp",
                       "payload": {"jsonrpc": "2.0", "method": "notifications/pod/inbox",
                                   "params": {"action": action}}})


def device_mac(i: int) -> str:
    """Fake MAC for pod number i (1-based): aa:bb:cc:00:00:01, ...:02, ..."""
    return f"aa:bb:cc:00:00:{i:02x}"


def tone(seconds: float, amplitude: float, freq: float = 220.0, rate: int = IN_RATE) -> bytes:
    return b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
                    for i in range(int(rate * seconds)))


async def check_in(http: ClientSession, server: str, device: str = DEVICE_ID) -> dict:
    async with http.post(f"{server.rstrip('/')}/xiaozhi/ota/", json={"application": {"version": "0.0.0"}},
                         headers={"Device-Id": device, "Client-Id": str(uuid.uuid4())}) as r:
        return (await r.json())["websocket"]


async def connect(http: ClientSession, server: str, device: str = DEVICE_ID):
    """OTA check-in + WebSocket hello. Returns (ws, session_id, downlink sample rate)."""
    ws_cfg = await check_in(http, server, device)
    headers = {"Protocol-Version": "1", "Device-Id": device, "Client-Id": str(uuid.uuid4())}
    if ws_cfg.get("token"):
        headers["Authorization"] = f"Bearer {ws_cfg['token']}"
    ws = await http.ws_connect(ws_cfg["url"], headers=headers)
    await ws.send_str(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                                  "features": {"mcp": True},
                                  "audio_params": {"format": "opus", "sample_rate": IN_RATE,
                                                   "channels": 1, "frame_duration": FRAME_MS}}))
    hello = json.loads((await ws.receive()).data)
    return ws, hello.get("session_id", ""), hello["audio_params"]["sample_rate"]


async def run_inbox(args) -> dict:
    """Tap-to-play, as the firmware does it. Returns {"pcm", "texts", "events", "rate"}."""
    async with ClientSession() as http:
        ws, sid, out_rate = await connect(http, args.server, getattr(args, "device", None) or DEVICE_ID)
        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "manual"}))
        await ws.send_str(inbox_frame(sid, args.inbox, getattr(args, "wrap", "mcp")))
        dec = opus.Decoder(out_rate, 1)
        pcm, texts, events = bytearray(), [], []
        next_after = getattr(args, "next_after", None)  # seconds of audio, then tap again ("next")
        sent_next = False
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=args.timeout)
            if msg.type == WSMsgType.BINARY:
                pcm.extend(dec.decode(msg.data, out_rate * FRAME_MS // 1000))
                if next_after and not sent_next and len(pcm) / 2 / out_rate >= next_after:
                    sent_next = True
                    await ws.send_str(inbox_frame(sid, "next", getattr(args, "wrap", "mcp")))
                    events.append("sent next")
            elif msg.type == WSMsgType.TEXT:
                m = json.loads(msg.data)
                print("  <-", {k: v for k, v in m.items() if k != "session_id"})
                events.append(f"{m.get('type')}:{m.get('state', m.get('emotion', ''))}")
                if m.get("type") == "tts" and m.get("state") == "sentence_start":
                    texts.append(m.get("text", ""))
                if m.get("type") == "tts" and m.get("state") == "stop":
                    break
            else:
                break
        await ws.close()
    if pcm and getattr(args, "out", None):
        open(args.out, "wb").write(pcm_to_wav(bytes(pcm), out_rate))
        print(f"message audio {len(pcm) / 2 / out_rate:.1f}s -> {args.out}")
    return {"pcm": bytes(pcm), "texts": texts, "events": events, "rate": out_rate}


async def run_wake(server: str, device: str, level: float, delay: float = 0.0, timeout: float = 10,
                   phrase: str = "Hi ESP") -> dict:
    """One pod hearing the wake word and a question, the way xiaozhi sends it.

    level = loudness of what this pod heard (0..1 of full scale): the closer pod is louder.
    Returns {"device","answered","stood_down","texts","audio_s"}.
    """
    enc = opus.Encoder(IN_RATE, 1, "voip")
    step = IN_RATE * FRAME_MS // 1000 * 2
    amp = 32767 * level

    def frames(pcm: bytes):
        for off in range(0, len(pcm), step):
            yield enc.encode(pcm[off:off + step].ljust(step, b"\x00"), step // 2)

    async with ClientSession() as http:
        ws, sid, out_rate = await connect(http, server, device)
        await asyncio.sleep(delay)
        # 1) the cached wake-word audio (room noise, then the phrase), 2) detect, 3) listen start
        noise = b"".join(struct.pack("<h", int(amp * 0.02 * math.sin(i * 1.7))) for i in range(IN_RATE // 4))
        for f in frames(noise + tone(0.8, amp, 330)):
            await ws.send_bytes(f)
        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": phrase}))
        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "realtime"}))
        # the question, then silence so the server's VAD ends the turn
        for f in frames(tone(1.2, amp) + b"\x00" * (IN_RATE * 2 * 2)):
            if ws.closed:
                break
            try:
                await ws.send_bytes(f)
            except (ConnectionError, ClientError):
                break
            await asyncio.sleep(FRAME_MS / 1000 * 0.2)
        dec = opus.Decoder(out_rate, 1)
        texts, audio, stood_down = [], bytearray(), False
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
            except TimeoutError:
                break
            if msg.type == WSMsgType.BINARY:
                audio.extend(dec.decode(msg.data, out_rate * FRAME_MS // 1000))
            elif msg.type == WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("type") == "tts" and m.get("state") == "sentence_start":
                    texts.append(m.get("text", ""))
                if m.get("type") == "tts" and m.get("state") == "stop":
                    break
            else:  # server closed the socket: that's the "stand down" signal
                stood_down = not texts
                break
        await ws.close()
    return {"device": device, "answered": bool(texts), "stood_down": stood_down, "texts": texts,
            "audio_s": len(audio) / 2 / out_rate}


async def run_pods(args) -> int:
    """N pods wake on the same phrase; pod 1 is closest (loudest). Only one should answer."""
    levels = [0.5 / (i + 1) for i in range(args.pods)]
    results = await asyncio.gather(*(
        run_wake(args.server, device_mac(i + 1), levels[i], delay=0.05 * (args.pods - 1 - i),
                 timeout=args.timeout)
        for i in range(args.pods)))
    for r in results:
        what = "ANSWERED " + " / ".join(r["texts"]) if r["answered"] else (
            "stood down" if r["stood_down"] else "no reply")
        print(f"{r['device']}: {what}")
    return 0 if sum(r["answered"] for r in results) == 1 else 1


async def run(args) -> int:
    if args.say:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        subprocess.run(["say", "-o", tmp, "--data-format=LEI16@16000", args.say], check=True)
        args.wav = tmp
    pcm, rate = wav_to_pcm(open(args.wav, "rb").read())
    pcm = resample(pcm, rate, IN_RATE)

    device = getattr(args, "device", None) or DEVICE_ID
    async with ClientSession() as http:
        ws_cfg = await check_in(http, args.server, device)
        print("OTA ->", ws_cfg["url"], "| status:", ws_cfg.get("status_url"))
        headers = {"Protocol-Version": "1", "Device-Id": device, "Client-Id": str(uuid.uuid4())}
        if ws_cfg.get("token"):
            headers["Authorization"] = f"Bearer {ws_cfg['token']}"

        async with http.ws_connect(ws_cfg["url"], headers=headers) as ws:
            await ws.send_str(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                                          "features": {"mcp": True},
                                          "audio_params": {"format": "opus", "sample_rate": IN_RATE,
                                                           "channels": 1, "frame_duration": FRAME_MS}}))
            hello = json.loads((await ws.receive()).data)
            out_rate = hello["audio_params"]["sample_rate"]
            sid = hello.get("session_id", "")
            print("hello <-", hello["audio_params"])

            await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": args.mode}))
            enc = opus.Encoder(IN_RATE, 1, "voip")
            step = IN_RATE * FRAME_MS // 1000 * 2
            if args.mode != "manual":
                pcm += b"\x00" * (IN_RATE * 2 * 2)  # trailing silence so the server VAD fires
            for off in range(0, len(pcm), step):
                chunk = pcm[off:off + step].ljust(step, b"\x00")
                await ws.send_bytes(enc.encode(chunk, step // 2))
                await asyncio.sleep(FRAME_MS / 1000 * (0.2 if args.fast else 1.0))
            if args.mode == "manual":
                await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))
            t_end = time.monotonic()
            print(f"sent {len(pcm) / 2 / IN_RATE:.1f}s of audio")

            dec = opus.Decoder(out_rate, 1)
            reply = bytearray()
            first_audio = None
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout=args.timeout)
                if msg.type == WSMsgType.BINARY:
                    if first_audio is None:
                        first_audio = time.monotonic() - t_end
                    reply.extend(dec.decode(msg.data, out_rate * FRAME_MS // 1000))
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    print("  <-", {k: v for k, v in m.items() if k != "session_id"})
                    if m.get("type") == "tts" and m.get("state") == "stop":
                        break
                    if m.get("type") == "alert":
                        break
                else:
                    print("socket closed:", msg.type)
                    break
            if reply:
                open(args.out, "wb").write(pcm_to_wav(bytes(reply), out_rate))
                print(f"reply {len(reply) / 2 / out_rate:.1f}s -> {args.out}; first audio "
                      f"{first_audio:.2f}s after end of speech")
            await ws.close()
    return 0 if reply else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8003")
    ap.add_argument("--wav", help="question audio (16-bit wav)")
    ap.add_argument("--say", help="macOS only: synthesize the question with `say`")
    ap.add_argument("--mode", default="manual", choices=["manual", "auto"])
    ap.add_argument("--out", default="reply.wav")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--fast", action="store_true", help="send audio faster than real time")
    ap.add_argument("--inbox", choices=["play", "next", "skip"], help="voice inbox command instead of a question")
    ap.add_argument("--wrap", default="mcp", choices=["mcp", "plain"], help="inbox frame format")
    ap.add_argument("--next-after", type=float, help="with --inbox play: send 'next' after N s of audio")
    ap.add_argument("--device", help=f"fake device id / MAC (default {DEVICE_ID})")
    ap.add_argument("--pods", type=int, default=1, help="with --wake: simulate N pods in earshot")
    ap.add_argument("--wake", action="store_true", help="start by wake word instead of a tap")
    args = ap.parse_args()
    if args.wake:
        sys.exit(asyncio.run(run_pods(args)))
    if args.inbox:
        res = asyncio.run(run_inbox(args))
        sys.exit(0 if res["events"] else 1)
    if not args.wav and not args.say:
        ap.error("--wav or --say is required")
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
