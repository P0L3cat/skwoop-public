"""Skwoop bridge: a xiaozhi-protocol voice server for the pod.

One process, one port (default 8003), LAN only:
  POST /xiaozhi/ota/     xiaozhi boot check-in -> tells the pod where the WebSocket
                         and the status feed live (no activation, no firmware push)
  GET  /xiaozhi/v1/      xiaozhi WebSocket: Opus mic audio in, Opus speech out
  GET  /status ...       team status feed, read key required (see status_store.py)
  /inbox ...             voice inbox: bots queue messages, the pod plays them on tap
                         (see inbox_store.py; WebSocket command {"type":"inbox","action":...})
  POST /stt              pod audio (WAV, or raw 16-bit mono PCM with ?rate=16000) -> {"text", "backend"}
                         (same STT as a voice turn)
  POST /ack              {"kind":"light_up"[,"mood"][,"room"|"device"]} -> a pre-rendered "okay, I turned it up"
                         (Bearer STATUS_WRITE_KEY; see acks.py). GET /ack lists kinds.
  POST /say-template     {"template":"light_set","values":{"pct":50}[,"mood"][,"room"|"device"][,"dry_run"]}
                         -> stitched pieces, "Okay, setting it to... fifty percent." (live TTS if a piece
                         is missing; Bearer STATUS_WRITE_KEY; see stitch.py). GET /say-template lists templates.
  GET  /healthz

Voice turn: Opus 16 kHz -> PCM -> VAD end-of-speech (or "listen stop" for push-to-talk)
-> STT -> Grok (Deep voice line, or xAI direct) streamed by sentence -> TTS per
sentence -> Opus 24 kHz / 60 ms frames paced to the device.

Several pods (one per room) share this bridge: SKWOOP_ROOMS names them (rooms.py), the
inbox and alerts are shared (or room-targeted), and when more than one pod wakes on the same
"Hi ESP" the WakeArbiter keeps the one that heard it best and quietly closes the others.

Secrets (XAI_API_KEY, DEEP_MODEL_API_KEY, SKWOOP_DEVICE_TOKEN, STATUS_WRITE_KEY, STATUS_READ_KEY) are read
from the environment only and never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
import wave
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, cast
from urllib.parse import quote

import barge
import skwoop_mood.emotion as emotion
import harness.deep_model as deep_model
import tools.ha_service as house
import tools.messaging as messaging
import notes_pick
import opus
import router.quick as quick
import router
import harness.skwoop_turn as skwoop_turn
import status_read
from acks import ALIASES, AckLibrary
from acks import EMOTION as ACK_EMOTION
from credit_guard import BlockAll
from dsp import rms16, samples
from filler import (
    FillerClip,
    FillerPlayer,
    FillerSet,
    FillerTiming,
    fade_out,
    filler_mood,
    timing_from_env,
)
from tools.grokipedia import Grokipedia
from harness import TZ, HandOff, Harness, TurnStats
from tools.house_tools import HouseTools, ha_from_env
from inbox_store import InboxItem, InboxStore
from lean_http import (
    Application,
    ClientError,
    ClientSession,
    ClientTimeout,
    Request,
    Response,
    StreamResponse,
    WebSocketResponse,
    web,
)
from lean_ws import WSMsgType
from memory.memory_recall import MemoryRecall
from memory.memory_write import MemoryService, MemoryWriter
from tools.music import Music, Played
from harness.pod_agent import DEEP, FAST, ProxyClient
from providers import (
    LLM,
    STT,
    TTS,
    TTS_RATE,
    XaiSttStream,
    env,
    pcm_to_wav,
    split_sentences,
    voice_key,
    wav_to_pcm,
)
import auth
from tools.quick_search import PROXY_BASE, QuickSearch, SearchBudget
from tools.reminders import Reminders
from status_store import StatusStore
from stitch import SlotValue, StitchLibrary
from tone import classify_tone
from harness.toolsets import Toolset

log = logging.getLogger("skwoop.bridge")

FRAME_MS = 60
OUT_FRAME = TTS_RATE * FRAME_MS // 1000  # 1440 samples
DEVICE_TOKEN = env("SKWOOP_DEVICE_TOKEN")
ALLOWED_DEVICES = {d.strip().lower() for d in env("SKWOOP_ALLOWED_DEVICES").split(",") if d.strip()}
VAD_SILENCE_MS = int(env("VAD_SILENCE_MS", "700"))
VAD_MIN_SPEECH_MS = int(env("VAD_MIN_SPEECH_MS", "250"))
VAD_MIN_RMS = float(env("VAD_MIN_RMS", "450"))
MAX_UTTERANCE_S = float(env("MAX_UTTERANCE_S", "25"))
STT_MAX_S = float(env("STT_MAX_S", "90"))
IDLE_CLOSE_S = float(env("IDLE_CLOSE_S", "30"))
HISTORY_TURNS = int(env("HISTORY_TURNS", "6"))
HISTORY_RESET_S = float(env("SKWOOP_SESSION_IDLE_S", "3600"))
# Follow-up listen (Oct 4 2026): after a reply finishes PLAYING, keep the mic open this long for Alex to answer
# without the wake word; if he doesn't start talking, stop listening and send the pod back to idle / wake word.
# SKWOOP_FOLLOWUP_SECS (new name) wins over the old SKWOOP_FOLLOWUP_S; 0 = no follow-up listen at all.
FOLLOWUP_S = max(0.0, float(env("SKWOOP_FOLLOWUP_SECS") or env("SKWOOP_FOLLOWUP_S") or "3"))
SCRIPT_LISTEN_HOLDOFF_S = 1.2
PCM_LEAD_S = 0.24          # seconds of reply audio allowed ahead of real time
TTS_PARALLEL = max(1, int(os.environ.get("SKWOOP_TTS_PARALLEL", "2")))   # live syntheses in flight per reply
NOTES_PREPICK = os.environ.get("SKWOOP_NOTES_PREPICK", "1") != "0"    # notes_pick before a deep notes read
FILLER_LEAD_S = 0.12       # less for fillers, so a cut lands quickly
PREWARM_EVERY_S = 20.0     # "listen start" re-warms connections at most this often
GROKBOT_PROMPT = ("You are Grok on Alex's desk pod. Not Skwoop. Do not control the house. "
                  "One to three short spoken sentences. Plain text, no markdown.")
REPLY_WINDOW_S = float(env("INBOX_REPLY_WINDOW_S", "180"))  # a voice turn this soon after a message = reply
ACK_MOOD_TTL_S = float(env("ACK_MOOD_TTL_S", "120"))  # an ack without "mood" follows the pod's mood this long
# The firmware can only send {"type":"mcp"} frames without patching xiaozhi core, so the inbox
# command also arrives as an MCP notification: {"type":"mcp","payload":{"method":<one of these>,
# "params":{"action":"play"}}}. Both forms are handled identically.
INBOX_MCP_METHODS = {"notifications/pod/inbox", "pod/inbox"}
ARB_WINDOW_MS = int(env("SKWOOP_ARBITRATION_MS", "700"))    # wakes this close together = same utterance
ARB_LATE_MS = int(env("SKWOOP_ARBITRATION_LATE_MS", "1500"))  # stragglers after the decision stand down
ARB_TIE_DB = float(env("SKWOOP_ARBITRATION_TIE_DB", "2.0"))   # closer than this: first to arrive wins
ARB_SOLO_S = 3600  # only arbitrate if another pod has been seen within this many seconds
# Face tone per reply sentence: "keyword" (default) = tone.classify_tone -> happy | sad | neutral;
# "happy" = the old behaviour (one "happy" per reply), a revert switch that needs no code change;
# "pad" = emotion engine v0.1 (emotion.py, Oct 4 2026): the mood model picks each sentence's voice mood and sends its
# PAD to the face, with the urgency floor (alert / [crisply]) on top; keyword tone if the model can't load.
TONE_MODE = env("SKWOOP_TONE", "keyword").lower()
if TONE_MODE not in ("keyword", "happy", "pad"):
    log.warning("SKWOOP_TONE=%r is not keyword|happy|pad: using keyword", TONE_MODE)
    TONE_MODE = "keyword"
STT_Q_FINAL = object()
STT_Q_ABORT = object()

SYSTEM_PROMPT = env("SKWOOP_SYSTEM_PROMPT") or (
    "You are Skwoop, Alex's team agent, answering out loud through a small round desk pod. "
    "Reply in one to three short spoken sentences. No markdown, lists, emojis, or URLs. "
    "If a question needs a long answer, give the headline and offer to post details to chat."
)

_histories: dict[str, tuple[float, list[dict[str, str]]]] = {}
# device -> (when, from, text) of the last played message
_last_heard: dict[str, tuple[float, str, str]] = {}
_no_messages_pcm: bytes | None = None  # cached "No new messages." audio


def device_allowed(device_id: str, peer: str | None = None) -> bool:
    return auth.device_allowed(device_id, peer, ALLOWED_DEVICES)


def token_ok(request: Request) -> bool:
    return auth.bearer_matches(request.headers.get("Authorization", ""), DEVICE_TOKEN)


class EnergyVAD:
    """Tiny adaptive energy VAD over 60 ms frames: good enough for a desk mic behind AFE."""

    def __init__(self) -> None:
        self.noise = 200.0
        self.speech_ms = 0
        self.silence_ms = 0
        self.total_ms = 0

    def reset(self) -> None:
        self.speech_ms = self.silence_ms = self.total_ms = 0

    def push(self, pcm: bytes, ms: int) -> bool:
        """Returns True when an utterance has ended."""
        rms = rms16(samples(pcm))
        self.total_ms += ms
        if rms > max(VAD_MIN_RMS, self.noise * 3.0):
            self.speech_ms += ms
            self.silence_ms = 0
        else:
            self.noise = 0.95 * self.noise + 0.05 * rms
            if self.speech_ms:
                self.silence_ms += ms
        if self.speech_ms >= VAD_MIN_SPEECH_MS and self.silence_ms >= VAD_SILENCE_MS:
            return True
        return self.total_ms >= MAX_UTTERANCE_S * 1000

    @property
    def heard_speech(self) -> bool:
        return self.speech_ms >= VAD_MIN_SPEECH_MS


def _percentile(xs: list[float], q: float) -> float:
    """numpy.percentile (linear) of an already sorted list."""
    k = q / 100 * (len(xs) - 1)
    i = int(k)
    return xs[i] if i + 1 >= len(xs) else xs[i] + (xs[i + 1] - xs[i]) * (k - i)


def wake_score(pcm: bytes, rate: int) -> float | None:
    """How well a pod heard the wake word, from the preroll audio xiaozhi already sends.

    level = 90th-percentile 20 ms frame loudness (dBFS); floor = 10th percentile.
    score = level + (level - floor), i.e. loudness plus a signal-to-noise term, so a pod
    whose AGC boosted a far, noisy pickup doesn't beat the close one. Heuristic; tune on hardware.
    """
    x = samples(pcm)
    frame = max(1, rate // 50)
    n = len(x) // frame
    if n < 5:
        return None
    db = sorted(20 * math.log10((rms16(x[i * frame:(i + 1) * frame]) + 1e-3) / 32768.0) for i in range(n))
    level, floor = _percentile(db, 90), _percentile(db, 10)
    floor = max(floor, -90.0)
    return round(level + min(level - floor, 40.0), 2)


# Outbound websocket JSON. Same alias as house_tools.Json.
Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None


class WakeClaim(TypedDict):
    name: str
    score: float | None
    at: float
    fut: asyncio.Future[bool]


class WakeGroup(TypedDict):
    start: float
    claims: list[WakeClaim]
    decided_at: float | None


class SpokenAck(TypedDict):
    kind: str
    text: str
    requested: str
    mood: str
    emotion: str
    pcm: bytes | None
    source: str
    file: str | None


class AckSent(TypedDict):
    ok: bool
    kind: str
    text: str
    mood: str
    requested_mood: str
    emotion: str
    source: str
    room: str


class TemplateResult(TypedDict):
    ok: bool
    template: str
    text: str
    mood: str
    requested_mood: str
    emotion: str
    source: str
    pieces: int
    missing: list[str]
    audio_s: NotRequired[float]
    room: NotRequired[str]


class WakeArbiter:
    """Several pods heard one wake word: keep the best one, send the rest back to idle.

    The first wake opens a window (SKWOOP_ARBITRATION_MS). Every wake in that window joins the
    group; when it closes, the highest wake_score wins (within ARB_TIE_DB, earliest arrival
    wins; a pod without a score only wins if nobody has one). Wakes that arrive up to
    SKWOOP_ARBITRATION_LATE_MS after the decision are stood down too.
    """

    def __init__(self, window_ms: int = ARB_WINDOW_MS, late_ms: int = ARB_LATE_MS, tie_db: float = ARB_TIE_DB):
        self.window, self.late, self.tie = window_ms / 1000, late_ms / 1000, tie_db
        self.group: WakeGroup | None = None

    def claim(self, name: str, score: float | None, solo: bool = False) -> asyncio.Future[bool]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        now = time.monotonic()
        g = self.group
        if g is not None and g["decided_at"] is not None and now - g["decided_at"] <= self.late:
            log.info("[%s] wake arrived %.0f ms after the decision: standing down", name,
                     (now - g["decided_at"]) * 1000)
            fut.set_result(False)
            return fut
        if solo and (g is None or g["decided_at"] is not None):
            fut.set_result(True)  # no other pod around: no reason to wait
            return fut
        if g is None or g["decided_at"] is not None:
            g = self.group = {"start": now, "claims": [], "decided_at": None}
            loop.call_later(self.window, self._decide, g)
        g["claims"].append({"name": name, "score": score, "at": now, "fut": fut})
        return fut

    def _decide(self, g: WakeGroup) -> None:
        g["decided_at"] = time.monotonic()
        claims = g["claims"]
        scored = [c for c in claims if c["score"] is not None]
        if scored:
            best = max(c["score"] for c in scored)
            near = [c for c in scored if best - c["score"] <= self.tie]
            winner = min(near, key=lambda c: c["at"])
        else:
            winner = min(claims, key=lambda c: c["at"])
        if len(claims) > 1:
            log.info("wake arbitration: %s", ", ".join(
                f"{c['name']}={c['score']}{' WIN' if c is winner else ''}" for c in claims))
        for c in claims:
            if not c["fut"].done():
                c["fut"].set_result(c is winner)


class DeepAsk(Protocol):
    """skwoop_turn.ask: one agent turn through Deep' skwoop-voice webhook (a fake in tests and the demo)."""

    async def __call__(self, http: ClientSession | None, heard: str, room: str = "pod",
                       facts: list[str] | None = None) -> str: ...


@dataclass
class TurnProfile:
    """Where a turn's time went, logged once per turn:
    "turn profile route=fast stt=0.00s decide=0.00s first_token=0.71s ... tools=lights:0.21 usd=0.0004"."""
    t0: float
    route: str = "-"
    backend: str = "-"
    fillers: int = 0
    marks: dict[str, float] = field(default_factory=dict)
    tools: str = ""
    usd: float = 0.0
    why: str = ""
    harness: TurnStats | None = None

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, time.monotonic() - self.t0)

    def mark_at(self, name: str, at: float) -> None:
        self.marks.setdefault(name, at - self.t0)

    def line(self) -> str:
        times = " ".join(f"{k}={v:.2f}s" for k, v in self.marks.items())
        extra = (f" tools={self.tools}" if self.tools else "") + (f" usd={self.usd:.4f}" if self.usd else "")
        extra += self.harness.fields() if self.harness is not None else " harness=0"

        return f"route={self.route} {times} via={self.backend} fillers={self.fillers}{extra}"


class FramePacer:
    """One clock for a whole reply: 60 ms Opus frames sent at real time plus `lead`. A partial frame waits for the
    next chunk instead of being padded with silence, so sentences play back to back. When audio arrives after the
    client has run dry, the clock restarts at that frame."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]], lead: float = PCM_LEAD_S,
                 clock: Callable[[], float] = time.monotonic):
        self.send, self.lead, self.clock = send, lead, clock
        self.buf = bytearray()
        self.t0: float | None = None
        self.n = 0
        self.underruns = 0

    async def feed(self, pcm: bytes) -> None:
        self.buf += pcm
        step = OUT_FRAME * 2
        while len(self.buf) >= step:
            frame = bytes(self.buf[:step])
            del self.buf[:step]
            await self._frame(frame)

    async def flush(self) -> None:
        if self.buf:
            frame = bytes(self.buf) + b"\x00" * (OUT_FRAME * 2 - len(self.buf))
            self.buf.clear()
            await self._frame(frame)

    async def _frame(self, frame: bytes) -> None:
        now = self.clock()
        if self.t0 is None or self.n * FRAME_MS / 1000 < now - self.t0:
            if self.t0 is not None:
                self.underruns += 1
            self.t0, self.n = now, 0
        await self.send(frame)
        self.n += 1
        ahead = self.n * FRAME_MS / 1000 - (self.clock() - self.t0)
        if ahead > self.lead:
            await asyncio.sleep(ahead - self.lead)


@dataclass(frozen=True)
class WarmTarget:
    url: str
    headers: dict[str, str]


class Prewarm:
    """At "listen start", open (or refresh) the HTTPS connections the turn is about to need - ElevenLabs and xAI -
    with free metadata GETs, and refresh the weather cache. The shared session keeps them alive (keepalive 75 s), so
    the reply's first TTS / LLM request skips TCP + TLS setup."""

    def __init__(self, http: ClientSession, targets: list[WarmTarget], weather: quick.WeatherSource,
                 every_s: float = PREWARM_EVERY_S, house: HouseTools | None = None):
        self.http = http
        self.house = house
        self.targets = targets
        self.weather = weather
        self.every_s = every_s
        self.last = -every_s
        self.task: asyncio.Task[None] | None = None

    def kick(self) -> None:
        now = time.monotonic()
        if now - self.last < self.every_s or (self.task is not None and not self.task.done()):
            return

        self.last = now
        self.task = asyncio.create_task(self._run())

    async def _get(self, t: WarmTarget) -> None:
        try:
            async with self.http.get(t.url, headers=t.headers, timeout=ClientTimeout(total=4)) as r:
                await r.read()
        except (TimeoutError, ClientError) as e:
            log.debug("prewarm %s failed: %s", t.url, type(e).__name__)

    async def _weather(self) -> None:
        try:
            await self.weather.now()
        except quick.WeatherUnavailable as e:
            log.debug("prewarm weather: %s", e)

    async def _house(self) -> None:
        if self.house is not None:
            await self.house.refresh()

    async def _run(self) -> None:
        await asyncio.gather(*(self._get(t) for t in self.targets), self._weather(), self._house())


def warm_targets() -> list[WarmTarget]:
    out: list[WarmTarget] = []
    if env("ELEVENLABS_API_KEY"):
        out.append(WarmTarget("https://api.elevenlabs.io/v1/models", {"xi-api-key": env("ELEVENLABS_API_KEY")}))

    if env("XAI_API_KEY"):
        out.append(WarmTarget("https://api.x.ai/v1/models", {"Authorization": f"Bearer {env('XAI_API_KEY')}"}))

    return out


async def _one_sentence(sentence: str) -> AsyncIterator[str]:
    yield sentence


class Session:
    def __init__(self, ws: WebSocketResponse, device_id: str, app: Application):
        self.ws, self.device_id, self.app = ws, device_id, app
        self.session_id = uuid.uuid4().hex[:12]
        self.in_rate = 16000
        self.in_frame_ms = FRAME_MS
        self.decoder: opus.Decoder | None = None
        self.encoder = opus.Encoder(TTS_RATE, 1, "audio")
        try:
            setattr(self.encoder, "bitrate", 64000)  # noqa: B010 - field absent from the opus stub
            setattr(self.encoder, "complexity", 8)  # noqa: B010
        except Exception:
            pass
        self.mode = "auto"
        self.listening = False
        self.buf = bytearray()
        self.vad = EnergyVAD()
        self.respond_task: asyncio.Task[None] | None = None
        self.last_activity = time.monotonic()
        self.playing_id: str | None = None      # inbox item currently streaming
        self.play_progress = 0.0
        self.cancel_reason: str | None = None   # why respond_task was cancelled: abort | next | skip
        self.room = app["store"].rooms.name(device_id)
        self.preroll: deque[bytes] = deque(maxlen=60)  # decoded audio sent before "listen" (wake word)
        self.arb_task: asyncio.Task[None] | None = None
        self.arb_pending = False                # waiting for the wake arbitration verdict
        self.end_pending = False                # utterance ended while the verdict was pending
        self.woke = False                       # this turn started from the wake word
        self.mood, self.mood_at = "", 0.0       # last mood the bridge gave this pod (idle|happy|sad|...), for acks

        self.use_stream = False
        self.stt_q: asyncio.Queue[object] | None = None
        self.stt_task: asyncio.Task[None] | None = None
        self.stt_stream: XaiSttStream | None = None
        self.listen_t0 = 0.0
        self.want_followup = False
        self.followup_listen = False
        self.followup_task: asyncio.Task[None] | None = None
        self.listen_holdoff = 0.0
        self._barging = False
        self.barge_hold: list[bytes] = []       # mic audio from the barge-in until listening again
        self.server_barge = False               # hello features.barge_in == "server": the bridge listens for it
        self.barge_det = barge.BargeInDetector()
        self.barge_pre: deque[bytes] = deque(maxlen=8)  # last ~480 ms of mic audio while Skwoop talks
        self.ref = barge.Reference()            # what was sent to the pod, for echo
        self.tts_open = False                   # this turn has sent "tts start"

    def set_mood(self, mood: str) -> None:
        self.mood, self.mood_at = ALIASES.get(mood, mood), time.monotonic()

    def current_mood(self) -> str | None:
        return self.mood if self.mood and time.monotonic() - self.mood_at < ACK_MOOD_TTL_S else None

    # ---------- send helpers ----------
    async def send_json(self, obj: dict[str, Json]) -> None:
        if not self.ws.closed:
            obj.setdefault("session_id", self.session_id)
            await self.ws.send_str(json.dumps(obj))

    # ---------- main loop ----------
    async def run(self) -> None:
        watchdog = asyncio.create_task(self._idle_watchdog())
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT and isinstance(msg.data, str):
                    await self.on_text(msg.data)
                elif msg.type == WSMsgType.BINARY and isinstance(msg.data, bytes):
                    self.on_audio(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            watchdog.cancel()
            if self.respond_task:
                self.respond_task.cancel()
            if self.arb_task:
                self.arb_task.cancel()
            if self.stt_task:
                self.stt_task.cancel()
            log.info("[%s] disconnected", self.room)

    async def _idle_watchdog(self) -> None:
        """Close the channel IDLE_CLOSE_S after the last activity: sleeps until that deadline, not on an interval."""
        while not self.ws.closed:
            await asyncio.sleep(max(1.0, self.last_activity + IDLE_CLOSE_S - time.monotonic() + 0.05))
            busy = (
                (self.respond_task is not None and not self.respond_task.done())
                or (self.stt_task is not None and not self.stt_task.done())
            )
            if not busy and time.monotonic() - self.last_activity > IDLE_CLOSE_S:
                log.info("[%s] idle %ss, closing channel", self.room, IDLE_CLOSE_S)
                await self.ws.close()
                return

    async def on_text(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return
        t = msg.get("type")
        if t == "hello":
            ap = msg.get("audio_params") or {}
            self.in_rate = int(ap.get("sample_rate", 16000))
            self.in_frame_ms = int(ap.get("frame_duration", FRAME_MS))
            self.decoder = opus.Decoder(self.in_rate, 1)
            features = msg.get("features") if isinstance(msg.get("features"), dict) else {}
            self.server_barge = features.get("barge_in") == "server"
            reply: dict[str, Json] = {"type": "hello", "transport": "websocket",
                           "audio_params": {"format": "opus", "sample_rate": TTS_RATE, "channels": 1,
                                            "frame_duration": FRAME_MS}}
            if "barge_in" in features:                 # only pods that ask get the extra key
                reply["features"] = {"barge_in": True}
            await self.send_json(reply)
            log.info("[%s] hello (in %d Hz / %d ms%s)", self.room, self.in_rate, self.in_frame_ms,
                     ", server barge-in" if self.server_barge else "")
        elif t == "listen":
            state = msg.get("state")
            # Alex opened a listen himself (wake word, push-to-talk, a barge, the web pod's tap): its rules, not the
            # follow-up window. xiaozhi firmware re-sends "listen start auto" right after "tts stop" to open the
            # follow-up itself; that one keeps the window running.
            if self.followup_listen and (state == "detect" or (state == "start" and (
                    msg.get("mode") == "manual" or msg.get("barge")
                    or self.device_id.lower().startswith("web-pod-")))):
                self._cancel_followup_timer()
                self.followup_listen = False
            if state == "start":
                self.mode = msg.get("mode", "auto")
                if self.respond_task is not None and not self.respond_task.done():
                    self.begin_barge(why="pod" if msg.get("barge") else "listen")
                else:
                    self.start_listening()
            elif state == "stop" and self.listening:
                self.end_utterance(force=True)
            elif state == "detect":
                self.on_wake(msg)
        elif t == "abort":
            await self.abort()
        elif t == "inbox":
            await self.on_inbox(str(msg.get("action", "play")))
        elif t == "mcp":
            payload = msg.get("payload")
            if isinstance(payload, dict) and payload.get("method") in INBOX_MCP_METHODS:
                raw_params = payload.get("params")
                params = raw_params if isinstance(raw_params, dict) else {}
                await self.on_inbox(str(params.get("action", "play")))
        # other MCP traffic and unknown types: ignored in v1

    def start_listening(self) -> None:
        if time.monotonic() < self.listen_holdoff:
            log.info("[%s] skip listen (script holdoff)", self.room)
            return
        self.listening = True
        self.buf.clear()
        self.vad.reset()
        self.preroll.clear()
        self.last_activity = time.monotonic()
        self.listen_t0 = time.monotonic()
        self._begin_stt_stream()
        warm = self.app.get("prewarm")
        if warm is not None:
            warm.kick()

    def _cancel_followup_timer(self) -> None:
        t = self.followup_task
        if t is not None and not t.done():
            t.cancel()
        self.followup_task = None

    def _arm_followup(self, seconds: float) -> None:
        self._cancel_followup_timer()
        self.followup_listen = True
        self.followup_task = asyncio.create_task(self._followup_timeout(seconds))

    async def _followup_timeout(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            # a blip of sound right at the deadline: give it until the VAD calls it speech or silence
            while (self.listening and self.vad.speech_ms and not self.vad.heard_speech
                   and self.vad.silence_ms < VAD_SILENCE_MS):
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
        if not self.listening or not self.followup_listen:
            return
        if self.respond_task is not None and not self.respond_task.done():
            return
        stream = self.stt_stream
        interim = stream.interim_text if stream is not None else ""
        if self.vad.heard_speech or interim:
            return  # Alex is answering: STT / the VAD end this turn as usual (xAI also sends EMPTY interims on silence)
        # (Before Oct 4 this also bailed when more than 0.4 s of mic audio had arrived, which is always true with a
        # pod streaming its mic, so the window never closed and the pod sat listening until STT gave up.)
        log.info("[%s] follow-up timeout (%.0fs of silence after the reply)", self.room, FOLLOWUP_S)
        self.listening = False
        self.followup_listen = False
        self.want_followup = False
        self.followup_task = None
        self._abort_stt_stream()
        self.buf.clear()
        self.vad.reset()
        if self.ws.closed:
            return
        await self.send_json({"type": "listen", "state": "stop", "reason": "followup_timeout"})
        if not self.device_id.lower().startswith("web-pod-"):
            # xiaozhi firmware, Pod Studio and fake_pod go back to idle (wake word) on a server close; the web pod's
            # sidecar would show "Reconnecting...", and it never opens its mic for a follow-up anyway
            await self.ws.close()

    def begin_barge(self, preroll: list[bytes] | None = None, why: str = "listen") -> None:
        """Alex talks over Skwoop: stop the reply and listen. Mic audio from now until listening is held
        (with `preroll`, the speech that triggered it) and replayed into STT, so the first words aren't lost."""
        if self._barging:
            return
        self._barging = True
        self.barge_hold = list(preroll or [])
        log.info("[%s] barge-in (%s)", self.room, why)
        asyncio.create_task(self._barge_in())

    async def _barge_in(self) -> None:
        try:
            self.want_followup = False
            self.listen_holdoff = 0.0
            await self._cancel_task("abort")
            if not self.ws.closed:
                await self.send_json({"type": "tts", "state": "stop"})
            self.start_listening()
        finally:
            hold, self.barge_hold = self.barge_hold, []
            self._barging = False
        if self.listening:
            for pcm in hold:
                self._accept_pcm(pcm)

    def _begin_stt_stream(self) -> None:
        self._abort_stt_stream()
        stt = self.app.get("stt")
        if stt is None or not stt.streaming_enabled():
            self.use_stream = False
            return
        self.use_stream = True
        self.stt_q = asyncio.Queue(maxsize=2500)
        self.stt_task = asyncio.create_task(self._stt_listen())

    def _stt_put(self, item: object) -> None:
        q = self.stt_q
        if q is None:
            return
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("[%s] stt queue full", self.room)

    def _abort_stt_stream(self) -> None:
        self._stt_put(STT_Q_ABORT)
        task = self.stt_task
        if task is not None and not task.done():
            task.cancel()
        self.stt_task = None
        self.stt_q = None
        self.stt_stream = None
        self.use_stream = False

    async def _stt_listen(self) -> None:
        stt: STT = self.app["stt"]
        q = self.stt_q
        if q is None:
            return
        try:
            while self.arb_pending:
                await asyncio.sleep(0.02)
            if not self.listening and not self.end_pending:
                return
            stream = stt.open_stream(self.in_rate)
            self.stt_stream = stream
            await stream.start()
            while True:
                item = await q.get()
                if item is STT_Q_ABORT:
                    await stream.close()
                    return
                if item is STT_Q_FINAL:
                    await stream.finalize()     # end of speech: nothing more goes in, only the final text comes back
                    break
                await stream.push(cast(bytes, item))
                if stream.finished:
                    break
            text = await stream.wait(timeout=STT_MAX_S + 15)
        except asyncio.CancelledError:
            live = self.stt_stream
            if live is not None:
                await live.close()
            raise
        except Exception as e:
            log.warning("[%s] stt stream failed (%s); batch fallback", self.room, type(e).__name__)
            self.use_stream = False
            pcm = bytes(self.buf)
            if pcm:
                self.end_utterance(force=True)
            return
        finally:
            live = self.stt_stream
            if live is not None:
                try:
                    await live.close()
                except Exception:
                    pass
            self.stt_stream = None
        text = (text or "").strip()
        self.stt_task = None
        self.stt_q = None
        if not text:
            log.info("[%s] stt stream empty", self.room)
            self.listening = False
            self.followup_listen = False
            self.want_followup = False
            return
        self.listening = False
        self.buf.clear()
        self._cancel_followup_timer()
        self.respond_task = asyncio.create_task(self.respond(b"", text=text))

    # ---------- wake word + multi-pod arbitration ----------
    def on_wake(self, msg: dict[str, object]) -> None:
        self.last_activity = time.monotonic()
        if self.respond_task is not None and not self.respond_task.done():
            self.begin_barge(why="wake word")
            return
        # a firmware may report its own level; otherwise score the preroll it sent
        score = msg.get("wake_score")
        if not isinstance(score, (int, float)):
            score = wake_score(b"".join(self.preroll), self.in_rate) if self.preroll else None
        self.preroll.clear()
        store = self.app["store"]
        others = store.active_devices(ARB_SOLO_S) - {self.device_id.lower()}
        log.info("[%s] wake word %r (score %s)", self.room, msg.get("text", ""), score)
        self.woke = True
        self.arb_pending = True
        fut = self.app["arbiter"].claim(self.room, score, solo=not others)
        self.arb_task = asyncio.create_task(self._await_verdict(fut))

    async def _await_verdict(self, fut: asyncio.Future[bool]) -> None:
        won = await fut
        self.arb_pending = False
        if not won:
            log.info("[%s] another pod heard it better: standing down", self.room)
            self.listening = False
            self.buf.clear()
            self._abort_stt_stream()
            await self.ws.close()  # xiaozhi goes back to idle silently on a server close
            return
        if self.end_pending:
            self.end_pending = False
            self.end_utterance(force=True)

    def _decode(self, packet: bytes) -> bytes | None:
        dec = self.decoder
        if dec is None:
            return None
        try:
            return dec.decode(packet, self.in_rate * self.in_frame_ms // 1000)
        except opus.OpusError:
            return None

    def _watch_barge(self, pcm: bytes) -> None:
        """Server-side barge-in (pods with AEC that keep streaming the mic while Skwoop talks)."""
        now = time.monotonic()
        self.barge_pre.append(pcm)
        if not self.ref.active(now):                # Skwoop is silent (thinking): talk now is not a barge-in
            return
        if self.barge_det.push(barge.rms_db(pcm), self.ref.db(now), self.in_frame_ms):
            self.begin_barge(list(self.barge_pre), why="voice over playback")

    def on_audio(self, packet: bytes) -> None:
        if self.decoder is None:
            return
        busy = self.respond_task is not None and not self.respond_task.done()
        if self._barging:                           # stopping the reply: keep what Alex is saying
            if (pcm := self._decode(packet)) is not None and len(self.barge_hold) < 100:
                self.barge_hold.append(pcm)
            return
        if not self.listening:
            if busy and self.server_barge:
                if (pcm := self._decode(packet)) is not None:
                    self._watch_barge(pcm)
            elif not busy:                          # xiaozhi sends the wake word audio before "listen detect"
                if (pcm := self._decode(packet)) is not None:
                    self.preroll.append(pcm)
            return
        if busy:
            return  # echo while speaking; barge-in is wake/PTT via listen start, or _watch_barge
        if (pcm := self._decode(packet)) is not None:
            self._accept_pcm(pcm)

    def _accept_pcm(self, pcm: bytes) -> None:
        self.buf.extend(pcm)
        if self.use_stream:
            if self.followup_listen:
                self.vad.push(pcm, self.in_frame_ms)    # speech-or-not only, for the follow-up timeout
            self.last_activity = time.monotonic()
            self._stt_put(pcm)
            if self.listen_t0 and (time.monotonic() - self.listen_t0) > STT_MAX_S:
                self._stt_put(STT_Q_FINAL)
                self.listen_t0 = 0.0
            return
        ended = self.vad.push(pcm, self.in_frame_ms)
        if self.vad.speech_ms:
            self.last_activity = time.monotonic()
        if ended and self.mode != "manual":
            if self.arb_pending:
                self.end_pending = True  # decide who answers first
                self.listening = False
            else:
                self.end_utterance()
        elif len(self.buf) > self.in_rate * 2 * MAX_UTTERANCE_S:
            self.end_utterance(force=True)

    def end_utterance(self, force: bool = False) -> None:
        if self.arb_pending:  # push-to-talk "stop" during arbitration: wait for the verdict
            self.end_pending, self.listening = True, False
            return
        if self.use_stream:
            self._stt_put(STT_Q_FINAL)
            return
        pcm = bytes(self.buf)
        self.buf.clear()
        heard = self.vad.heard_speech or (force and len(pcm) > self.in_rate * 2 * 0.4)
        self.vad.reset()
        if not heard:
            return  # nothing said; keep listening in auto mode
        self.listening = False
        self._cancel_followup_timer()
        self.respond_task = asyncio.create_task(self.respond(pcm))

    async def _cancel_task(self, reason: str) -> None:
        task = self.respond_task
        if task is None or task.done():
            return
        self.cancel_reason = reason
        task.cancel()
        await asyncio.wait([task])
        self.cancel_reason = None

    async def abort(self) -> None:
        self._abort_stt_stream()
        await self._cancel_task("abort")
        await self.send_json({"type": "tts", "state": "stop"})

    # ---------- voice inbox ----------
    async def on_inbox(self, action: str) -> None:
        """play: next unread | next: skip the one playing, play the following | skip: mark played, stop."""
        self.listening = False  # the device opened the mic only to get a channel; drop it
        self.buf.clear()
        self.vad.reset()
        self._abort_stt_stream()
        self.last_activity = time.monotonic()
        inbox: InboxStore | None = self.app["store"].inbox
        playing = self.playing_id is not None and self.respond_task is not None and not self.respond_task.done()
        log.info("[%s] inbox %s (playing=%s)", self.room, action, playing)
        if action == "play":
            if playing:
                return
            await self._cancel_task("abort")
            self.respond_task = asyncio.create_task(self.play_inbox(continuing=False))
        elif action == "next":
            await self._cancel_task("next" if playing else "abort")
            self.respond_task = asyncio.create_task(self.play_inbox(continuing=playing))
        elif action == "skip":
            if playing:
                await self._cancel_task("skip")
                await self.send_json({"type": "tts", "state": "stop"})
            else:
                if inbox is not None and (item := inbox.next_unread()):
                    inbox.mark_played(item["id"])
                # every inbox command ends in "tts stop" so the device always returns to idle
                await self.send_json({"type": "tts", "state": "start"})
                await self.send_json({"type": "tts", "state": "sentence_start", "text": "Skipped."})
                await self.send_json({"type": "tts", "state": "stop"})
        else:
            log.warning("[%s] unknown inbox action %r", self.room, action)

    async def play_inbox(self, continuing: bool) -> None:
        """Stream the oldest unread message exactly like a TTS answer, then mark it played.

        continuing=True: the device is already in its speaking state (a "next" mid-playback),
        so no second tts start is sent.
        """
        inbox: InboxStore | None = self.app["store"].inbox
        item, busy_owner = inbox.claim(self.room, self.session_id) if inbox is not None else (None, None)
        started, cancelled = continuing, False
        try:
            if not started:
                await self.send_json({"type": "tts", "state": "start"})
                started = True
            if item is None:
                busy_room = self.app["sessions"].get(busy_owner) if busy_owner else None
                if busy_room:  # the only unread message is playing on another pod right now
                    line = f"That message is playing on the {busy_room} pod."
                    await self.send_json({"type": "tts", "state": "sentence_start", "text": line})
                    audio = await self._say_quietly(line)
                else:
                    await self.send_json({"type": "tts", "state": "sentence_start", "text": "No new messages."})
                    audio = await self._no_messages_audio() if not continuing else b""
                if audio:
                    await self.send_pcm(audio)
                return
            self.playing_id, self.play_progress = item["id"], 0.0
            if inbox is None:
                return
            pcm = inbox.load_pcm(item["id"])
            await self.send_json({"type": "llm", "emotion": "happy", "text": ""})
            await self.send_json({"type": "tts", "state": "sentence_start",
                                  "text": f"{item['from']}: {item['text'] or '(voice message)'}"})
            await self.send_pcm(pcm, progress=lambda f: setattr(self, "play_progress", f))
            self._heard(item)
            log.info("[%s] played inbox %s", self.room, item["id"])
        except asyncio.CancelledError:
            cancelled = True
            if item is not None and (self.cancel_reason in ("next", "skip") or self.play_progress >= 0.5):
                self._heard(item)
            raise  # whoever cancelled decides what the device hears next
        except Exception as e:
            log.error("[%s] inbox playback failed: %s", self.room, e)
        finally:
            if item is not None and inbox is not None:
                inbox.release(item["id"], self.session_id)
            self.playing_id = None
            self.last_activity = time.monotonic()
            if started and not cancelled and not self.ws.closed:
                await self.send_json({"type": "tts", "state": "stop"})

    def _heard(self, item: InboxItem) -> None:
        self.app["store"].inbox.mark_played(item["id"])
        _last_heard[self.device_id] = (time.time(), item["from"], item["text"])

    async def _say_quietly(self, text: str) -> bytes:
        try:
            return cast(bytes, await self.app["tts"].synth(text))
        except Exception:
            return b""

    async def _no_messages_audio(self) -> bytes:
        global _no_messages_pcm
        if _no_messages_pcm is None:
            try:
                _no_messages_pcm = await self.app["tts"].synth("No new messages.")
            except Exception:
                return b""  # try again next time
        return _no_messages_pcm

    # ---------- action acknowledgements ----------
    def start_ack(self, ack: SpokenAck) -> None:
        """Play a picked ack (acks.AckLibrary.pick) after whatever this pod is saying now."""
        prev = self.respond_task
        self.last_activity = time.monotonic()
        self.respond_task = asyncio.create_task(self.play_ack(ack, prev))

    async def play_ack(self, ack: SpokenAck, prev: asyncio.Task[None] | None = None) -> None:
        """Stream one ack exactly like a TTS answer: tts start, emotion, sentence_start, Opus, tts stop."""
        if prev is not None and not prev.done():
            await asyncio.wait([prev], timeout=30)  # let the current answer finish first
        started = cancelled = False
        try:
            pcm = ack["pcm"]
            if pcm is None:  # no rendered clip for this kind: say its first line with live TTS
                pcm = await self.app["tts"].synth(ack["text"])
            await self.send_json({"type": "tts", "state": "start"})
            started = True
            await self.send_json({"type": "llm", "emotion": ack["emotion"], "text": ""})
            self.set_mood(ack["mood"])
            await self.send_json({"type": "tts", "state": "sentence_start", "text": ack["text"]})
            await self.send_pcm(pcm)
            log.info("[%s] ack %s/%s %r via %s", self.room, ack["kind"], ack["mood"], ack["text"], ack["source"])
        except asyncio.CancelledError:
            cancelled = True
            raise  # abort() sends its own tts stop
        except Exception as e:  # noqa: BLE001 - keep the device usable
            log.error("[%s] ack %s failed: %s", self.room, ack["kind"], e)
        finally:
            self.last_activity = time.monotonic()
            if started and not cancelled and not self.ws.closed:
                await self.send_json({"type": "tts", "state": "stop"})

    async def _ask_skwoop(self, heard: str, facts: list[str] | None = None) -> str:
        """Wait for skwoop-voice. Keep thinking face; do not hold TTS/speaking."""
        ask: DeepAsk = self.app["ask_deep"]
        extra = {"facts": facts} if facts else {}
        task = asyncio.create_task(ask(self.app.get("http"), heard, room=self.room, **extra))
        try:
            while not task.done():
                self.last_activity = time.monotonic()
                done, _ = await asyncio.wait({task}, timeout=8)
                if done or self.ws.closed:
                    break
                try:
                    if not self.ws.closed:
                        await self.ws.ping()
                    await self.send_json({"type": "llm", "emotion": "thinking", "text": ""})
                except Exception:
                    break
            return await task
        except asyncio.CancelledError:
            task.cancel()
            raise

    # ---------- speaking a reply ----------
    async def _open_tts(self) -> None:
        if not self.tts_open:
            self.tts_open = True
            self.barge_det.reset()
            await self.send_json({"type": "tts", "state": "start"})

    async def filler_begin(self, clip: FillerClip) -> None:
        """FillerOut: a thinking filler starts. The face stays 'thinking' while it plays (filler: true)."""
        await self._open_tts()
        await self.send_json({"type": "llm", "emotion": "thinking", "text": ""})
        await self.send_json({"type": "tts", "state": "sentence_start", "text": clip.text, "filler": True})
        log.info("[%s] filler %s %r", self.room, clip.id, clip.text)

    async def filler_audio(self, pcm: bytes, stop: asyncio.Event) -> bool:
        return await self._stream_pcm(pcm, stop=stop, lead=FILLER_LEAD_S)

    def _start_filler(self, t0: float) -> FillerPlayer:
        player = FillerPlayer(self.app["fillers"], self, filler_mood(self.current_mood()), t0,
                              timing=self.app["filler_timing"])
        player.start()

        return player

    async def _speak_sentences(self, sentences: AsyncIterator[str], prof: TurnProfile,
                               player: FillerPlayer | None) -> str:
        """Speak sentences as they arrive. Each sentence's synthesis starts when its text is in (TTS_PARALLEL live
        at once) and its audio streams out while it is still being synthesized; one FramePacer carries the reply,
        so sentences play back to back. A running filler is cut (short fade, then a landing line if one was heard)
        the moment the first reply audio is ready."""
        tts: TTS = self.app["tts"]
        stream = getattr(tts, "synth_stream", None)
        jobs: asyncio.Queue[tuple[str, asyncio.Queue[bytes | None], asyncio.Task[None],
                                  emotion.SentenceMood | None] | None] = asyncio.Queue(maxsize=2)
        limit = asyncio.Semaphore(TTS_PARALLEL)
        tasks: list[asyncio.Task[None]] = []
        said: list[str] = []

        async def synth_one(sentence: str, before: str | None, out: asyncio.Queue[bytes | None],
                            sm: emotion.SentenceMood | None = None) -> None:
            try:
                async with limit:
                    mood = sm.mood if sm is not None else classify_tone(sentence)
                    if stream is None:
                        out.put_nowait(await tts.synth(sentence, mood=mood, previous_text=before))
                    else:
                        async for chunk in stream(sentence, mood=mood, previous_text=before):
                            out.put_nowait(chunk)
            finally:
                out.put_nowait(None)

        async def produce() -> None:
            try:
                async for sentence in sentences:
                    prof.mark("first_text")
                    before = " ".join(said) or None
                    said.append(sentence)
                    out: asyncio.Queue[bytes | None] = asyncio.Queue()
                    sm = emotion.engine().sentence(sentence) if TONE_MODE == "pad" else None
                    if sm is not None:
                        log.info("[%s] mood %s (%s) voice=%s pad=%s %.2f ms", self.room, sm.source, sm.tone, sm.mood,
                                 sm.pad_json(), sm.ms)
                    task = asyncio.create_task(synth_one(sentence, before, out, sm))
                    tasks.append(task)
                    await jobs.put((sentence, out, task, sm))
            finally:
                await jobs.put(None)

        async def send(frame: bytes) -> None:
            if not self.ws.closed:
                self.ref.sent(time.monotonic(), frame)
                await self.ws.send_bytes(self.encoder.encode(frame, OUT_FRAME))

        pacer = FramePacer(send)
        producer = asyncio.create_task(produce())
        first = True
        try:
            while (job := await jobs.get()) is not None:
                sentence, out, task, sm = job
                started = False
                while (chunk := await out.get()) is not None:
                    if not chunk:
                        continue
                    if first:
                        first = False
                        prof.mark("reply_audio")
                        prof.backend = tts.last_backend or tts.provider
                        if player is not None:
                            await player.finish()

                        await self._open_tts()
                        if TONE_MODE == "happy":
                            await self.send_json({"type": "llm", "emotion": "happy", "text": ""})
                            self.set_mood("happy")
                    if not started:
                        started = True
                        if TONE_MODE == "keyword":
                            tone = classify_tone(sentence)
                            await self.send_json({"type": "llm", "emotion": tone, "text": ""})
                            self.set_mood(tone)
                        elif sm is not None:          # SKWOOP_TONE=pad: keyword tone for old pods, PAD for the face
                            await self.send_json({"type": "llm", "emotion": sm.tone, "text": "", "pad": sm.pad_json()})
                            self.set_mood(sm.status_mood())
                        await self.send_json({"type": "tts", "state": "sentence_start", "text": sentence})
                    await pacer.feed(chunk)
                await task                               # surface synthesis errors
            await pacer.flush()
            await producer                               # surface producer errors
        finally:
            producer.cancel()
            for t in tasks:
                t.cancel()
            if pacer.underruns:
                log.info("[%s] reply audio ran dry %d time(s)", self.room, pacer.underruns)
            if player is not None:
                player.cancel()
                prof.fillers = len(player.played)
                if player.first_at is not None:
                    prof.marks.setdefault("filler", player.first_at - prof.t0)

        return " ".join(said)

    async def _deep_sentences(self, heard: str, prof: TurnProfile,
                                facts: list[str] | None = None) -> AsyncIterator[str]:
        stream = self.app.get("ask_deep_stream")
        if stream is not None:
            spoke = False
            keepalive = asyncio.create_task(self._keep_thinking())
            try:
                extra = {"facts": facts} if facts else {}
                async for sentence in stream(self.app.get("http"), heard, room=self.room, **extra):
                    if not spoke:
                        spoke = True
                        keepalive.cancel()
                        prof.mark("deep")
                    yield sentence
                return
            except deep_model.StreamUnavailable as e:
                if spoke:
                    return
                log.warning("[%s] Deep stream unavailable (%s): webhook path", self.room, e)
            finally:
                keepalive.cancel()
        reply = await self._ask_skwoop(heard=heard, facts=facts)
        prof.mark("deep")
        for sentence in split_sentences(reply):
            yield sentence

    async def _notes_facts(self, heard: str, why: str) -> list[str] | None:
        """A notes read ("what's in my notes about X"): search the notes here so Deep starts with the result."""
        if why != "deep-docs" or not NOTES_PREPICK:
            return None

        found = await asyncio.to_thread(notes_pick.facts_for, heard)
        if found is not None:
            log.info("[%s] notes pre-pick: %s", self.room, found[0][:200])

        return found

    async def _keep_thinking(self) -> None:
        """While Deep works on a streamed turn: keep the socket and the thinking face alive (as _ask_skwoop does)."""
        while not self.ws.closed:
            await asyncio.sleep(8)
            self.last_activity = time.monotonic()
            try:
                await self.ws.ping()
                await self.send_json({"type": "llm", "emotion": "thinking", "text": ""})
            except Exception:  # noqa: BLE001 - the turn notices a dead socket itself
                return

    async def _music(self, heard: str) -> Played | None:
        """Music asks done by the bridge itself (music.py); None hands the turn to Deep as before."""
        music: Music | None = self.app.get("music")
        if music is None:
            return None

        return await music.handle(heard)

    def _harness_pick(self, r: router.Route, text: str) -> Toolset | None:
        """The harness toolset for this turn, or None: Deep (harness off, music, a note write)."""
        harness: Harness | None = self.app.get("harness")

        return None if harness is None else harness.pick(self.device_id, r, text)

    async def _harness_sentences(self, toolset: Toolset, heard: str, prof: TurnProfile) -> AsyncIterator[str]:
        """One harness turn. A failure before the first sentence (HandOff) re-runs the turn on Deep with what was
        already looked up, while the filler keeps playing."""
        harness: Harness = self.app["harness"]
        stats = prof.harness = TurnStats()
        ts, hist = _histories.get(self.device_id, (0.0, []))
        history = hist if time.time() - ts <= HISTORY_RESET_S else []
        try:
            async for sentence in harness.turn(toolset, heard, self.device_id, self.room, history, stats):
                yield sentence
        except HandOff as e:
            prof.route += ">deep"
            prof.why = e.reason
            async for sentence in self._deep_sentences(heard, prof, facts=e.facts or None):
                yield sentence
        finally:
            if stats.reads_at is not None:
                prof.mark_at("prefill", stats.reads_at)
            if stats.first_token_at is not None:
                prof.mark_at("first_token", stats.first_token_at)
            prof.tools = ",".join(f"{n}:{t:.2f}" for n, t in stats.tools)
            prof.usd = stats.cost_ticks / 1e10

    async def _say_now(self, text: str, mood: str, prof: TurnProfile) -> None:
        """An instant reply (house script, quick intent): one clip, no filler."""
        tts: TTS = self.app["tts"]
        mood = emotion.floor_mood(text, mood) or mood     # SKWOOP_TONE=pad: an urgent line is spoken and shown as alert
        audio = await tts.synth(text, mood=mood)
        prof.mark("reply_audio")
        prof.backend = tts.last_backend or tts.provider
        await self._open_tts()
        await self.send_json({"type": "llm", "emotion": ACK_EMOTION.get(mood, "happy"), "text": ""})
        self.set_mood(mood)
        await self.send_json({"type": "tts", "state": "sentence_start", "text": text})
        if audio:
            await self.send_pcm(audio)

    async def _say_hit(self, hit: house.Hit, heard: str, prof: TurnProfile) -> None:
        tts: TTS = self.app["tts"]
        await self._open_tts()
        await self.send_json({"type": "llm", "emotion": "happy", "text": ""})
        await self.send_json({"type": "tts", "state": "sentence_start", "text": hit.text})
        audio = await house.ack_pcm(tts, hit)
        prof.mark("reply_audio")
        prof.backend = tts.last_backend or tts.provider
        if audio:
            await self.send_pcm(audio)

        self._remember(heard, hit.text)
        log.info("[%s] script %s (no llm)%s", self.room, hit.ack_id,
                 f" ({hit.mode} {hit.deeplink or ''})" if hit.mode else "")
        self.listen_holdoff = time.monotonic() + SCRIPT_LISTEN_HOLDOFF_S

    async def _quick(self, heard: str, prof: TurnProfile) -> bool:
        """Weather, time, day/date and timers straight from Home Assistant / templates: no router, no Deep."""
        answers: quick.QuickAnswers = self.app["quick"]
        intent = answers.match(heard, self.device_id)
        if intent is None:
            return False

        reply = await answers.answer(intent, self.device_id, self.room)
        if reply is None:
            return False

        prof.route = f"quick-{type(intent).__name__}"
        await self._say_now(reply.text, reply.mood, prof)
        self._remember(heard, reply.text)
        if reply.expects_answer:
            self.want_followup = True
        else:
            self.listen_holdoff = time.monotonic() + SCRIPT_LISTEN_HOLDOFF_S

        return True

    async def _confirm_text(self, heard: str, prof: TurnProfile) -> bool:
        """A pending "Text Mom: ... Send it?" takes the next utterance first: yes sends, no drops it."""
        texts: messaging.Texts | None = self.app.get("texts")
        if texts is None or texts.waiting(self.device_id) is None:
            return False
        reply = await texts.answer(self.device_id, heard)
        if reply is None:          # not a yes or a no: the pending text is dropped and the turn goes on as usual
            return False
        prof.route = "instant-text"
        await self._say_now(reply, "happy", prof)
        self._remember(heard, reply)
        return True

    async def _route_turn(self, text: str, prof: TurnProfile) -> None:
        """INSTANT (quick templates, house scripts) / the pod harness (models + house tools) / Deep skwoop-voice."""
        if await self._confirm_text(text, prof):
            return

        answers: quick.QuickAnswers = self.app["quick"]
        r = router.route(text, lambda t: answers.match(t, self.device_id),
                         lambda t: house.match(t, show=self.app["show"]))
        prof.mark("decide")
        prof.why = r.why
        log.info("[%s] route %s (%s)", self.room, r.tier, r.why)
        if r.tier == "instant" and r.intent is not None:
            reply = await answers.answer(r.intent, self.device_id, self.room)
            if reply is not None:
                prof.route = f"instant-{type(r.intent).__name__}"
                await self._say_now(reply.text, reply.mood, prof)
                self._remember(text, reply.text)
                if reply.expects_answer:
                    self.want_followup = True
                else:
                    self.listen_holdoff = time.monotonic() + SCRIPT_LISTEN_HOLDOFF_S
                return
            r = router.Route("fast", "quick answer unavailable")   # e.g. Home Assistant weather down
        elif r.tier == "instant" and r.hit is not None:
            prof.route = "instant-script"
            await house._fire(self.app.get("http"), r.hit)
            await self._say_hit(r.hit, text, prof)
            return

        llm: LLM = self.app["llm"]
        await self.send_json({"type": "llm", "emotion": "thinking", "text": ""})
        player = self._start_filler(prof.t0)
        if house.endpoint() == "grokbot":           # the Grokbot endpoint is plain chat by design
            prof.route = "chat"
            messages: list[dict[str, object]] = [
                {"role": "system", "content": GROKBOT_PROMPT}, {"role": "user", "content": text}]
            spoken = await self._speak_sentences(llm.stream_sentences(messages, fast=True), prof, player)
        elif r.why == "deep-music" and (played := await self._music(text)) is not None:
            prof.route = f"music-{played.how}"           # Spotify straight from the bridge, no Deep round trip
            spoken = await self._speak_sentences(_one_sentence(played.say), prof, player)
        elif (picked := self._harness_pick(r, text)) is not None:
            prof.route = f"harness-{picked.kind}"
            spoken = await self._speak_sentences(self._harness_sentences(picked, text, prof), prof, player)
        else:
            prof.route = "deep"
            facts = await self._notes_facts(text, r.why)
            if facts is not None:
                prof.route = "deep-notes"
            spoken = await self._speak_sentences(self._deep_sentences(text, prof, facts=facts), prof, player)

        self._remember(text, spoken)
        log.info("[%s] %s turn %d chars", self.room, prof.route, len(spoken))
        self.want_followup = True

    # ---------- one voice turn ----------
    async def respond(self, pcm: bytes, text: str | None = None) -> None:
        stt: STT = self.app["stt"]
        llm: LLM = self.app["llm"]
        prof = TurnProfile(time.monotonic())
        self.tts_open = False
        self.want_followup = False
        try:
            await self.send_json({"type": "llm", "emotion": "thinking", "text": ""})
            if not text:
                text = await stt.transcribe(pcm, self.in_rate)

            prof.mark("stt")
            log.info("[%s] stt %.2fs via %s %r", self.room, time.monotonic() - prof.t0,
                     stt.last_backend or stt.provider, text[:120])
            if not text:
                return

            await self.send_json({"type": "stt", "text": text})
            if llm.mode == "stub":                     # offline / tests without a model: the old local path
                if await self._quick(text, prof):
                    return
                hit = await house.apply(self.app.get("http"), text, show=self.app["show"])
                if hit:
                    prof.route = "script"
                    await self._say_hit(hit, text, prof)
                    return
                prof.route = "llm"
                player = self._start_filler(prof.t0)
                messages = cast(list[dict[str, object]], self._messages(text))
                reply = await self._speak_sentences(llm.stream_sentences(messages), prof, player)
                self._remember(text, reply)
                self.want_followup = True
                return

            await self._route_turn(text, prof)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep the device usable whatever broke
            log.error("[%s] turn failed: %s", self.room, e)
            if not self.tts_open:
                await self.send_json({"type": "alert", "status": "Error",
                                      "message": "Voice line hiccup - try again", "emotion": "sad"})
        finally:
            log.info("[%s] turn profile %s", self.room, prof.line())
            barge = self._barging
            if self.tts_open and not self.ws.closed and not barge:
                await self.send_json({"type": "tts", "state": "stop"})
            self.last_activity = time.monotonic()
            self.woke = False
            if not barge and self.want_followup and self.mode != "manual" and FOLLOWUP_S > 0:
                self.start_listening()
                # the last reply frame went out up to PCM_LEAD_S ahead of real time: count from when it stops playing
                self._arm_followup(FOLLOWUP_S + PCM_LEAD_S)
                log.info("[%s] follow-up listen %.0fs", self.room, FOLLOWUP_S)

    async def send_pcm(self, pcm: bytes, progress: Callable[[float], None] | None = None) -> None:
        """Encode to 60 ms Opus frames and pace them at ~real time (small lead)."""
        await self._stream_pcm(pcm, progress=progress)

    async def _stream_pcm(self, pcm: bytes, stop: asyncio.Event | None = None, lead: float = PCM_LEAD_S,
                          progress: Callable[[float], None] | None = None) -> bool:
        """True when `stop` cut it short: the frame after the stop is faded to silence and nothing more is sent."""
        step = OUT_FRAME * 2
        start = time.monotonic()
        for i, off in enumerate(range(0, len(pcm), step)):
            if progress is not None:
                progress(off / max(1, len(pcm)))

            chunk = pcm[off:off + step]
            if len(chunk) < step:
                chunk += b"\x00" * (step - len(chunk))

            if self.ws.closed:
                return False

            cut = stop is not None and stop.is_set()
            self.ref.sent(time.monotonic(), chunk)
            await self.ws.send_bytes(self.encoder.encode(fade_out(chunk) if cut else chunk, OUT_FRAME))
            if cut:
                return True

            ahead = (i + 1) * FRAME_MS / 1000 - (time.monotonic() - start)
            if ahead > lead:
                await asyncio.sleep(ahead - lead)

        return False

    # ---------- context ----------
    def _messages(self, user_text: str) -> list[dict[str, str]]:
        feed = self.app["store"].feed(self.room)
        team = "; ".join(f"{b['name']}: {b['state']}" + (f" ({b['detail']})" if b.get("detail") else "")
                         for b in feed["bots"]) or "no bots reporting"
        alert = feed["alert"]["text"] if feed.get("alert") else "none"
        unread = (feed.get("inbox") or {}).get("unread", 0)
        system = f"{SYSTEM_PROMPT}\nCurrent team status: {team}. Current alert: {alert}. " \
                 f"Unread voice messages on the pod: {unread}. Local time: {time.strftime('%a %H:%M')}.\n" \
                 f"This was asked from the {self.room} pod" + \
                 (" (hands-free, by wake word)." if self.woke else ".")
        heard = _last_heard.pop(self.device_id, None)
        if heard and time.time() - heard[0] < REPLY_WINDOW_S:
            _, sender, said = heard
            system += (f"\nThe user is replying to a message from {sender}. The message just heard was: "
                       f"\"{said or '(a recorded voice message)'}\". Treat what they say next as their reply to "
                       f"{sender}: acknowledge it briefly and carry out or relay what they ask.")
        ts, hist = _histories.get(self.device_id, (0.0, []))
        if time.time() - ts > HISTORY_RESET_S:
            hist = []
        return [{"role": "system", "content": system}, *hist, {"role": "user", "content": user_text}]

    def _remember(self, user_text: str, reply: str) -> None:
        _, hist = _histories.get(self.device_id, (0.0, []))
        hist = (hist + [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])
        _histories[self.device_id] = (time.time(), hist[-HISTORY_TURNS * 2:])


# ----------------------------------------------------------------------------
# HTTP handlers
# ----------------------------------------------------------------------------
async def ota(request: Request) -> Response:
    device_id = request.headers.get("Device-Id", "")
    if not device_allowed(device_id):
        log.warning("OTA from unknown device %s rejected", device_id or "?")
        return web.json_response({"error": "unknown device"}, status=403)
    port = request.app["port"]
    host = env("SKWOOP_ADVERTISE_HOST") or request.host.split(":")[0]
    tz_min = int(time.localtime().tm_gmtoff // 60)
    store = request.app["store"]
    # The feed needs the read key (Oct 3 2026); the firmware polls status_url as given and appends &since=...
    status_url = f"http://{host}:{port}/status"
    if store.read_key:
        status_url += "?key=" + quote(store.read_key, safe="")
    ws = {"url": f"ws://{host}:{port}/xiaozhi/v1/", "version": 1, "status_url": status_url}
    if DEVICE_TOKEN:
        ws["token"] = DEVICE_TOKEN  # LAN device token, not an API key
    if device_id:
        store.device_seen[device_id.lower()] = time.time()
    log.info("OTA check-in from %s (room %s) -> %s", device_id or "?",
             store.rooms.name(device_id) if device_id else "?", ws["url"])
    return web.json_response({
        "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": tz_min},
        "websocket": ws,
    })


async def ws_handler(request: Request) -> StreamResponse | WebSocketResponse:
    device_id = request.headers.get("Device-Id", "unknown")
    if not device_allowed(device_id, request.remote) or not token_ok(request):
        log.warning("WebSocket from %s rejected", device_id)
        return Response(status=401)
    ws = WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
    await ws.prepare(request)
    store = request.app["store"]
    store.device_seen[device_id.lower()] = time.time()
    session = Session(ws, device_id, request.app)
    log.info("[%s] connected (%s)", session.room, device_id)
    request.app["sessions"][session.session_id] = session.room
    request.app["live"][session.session_id] = session
    try:
        await session.run()
    finally:
        request.app["sessions"].pop(session.session_id, None)
        request.app["live"].pop(session.session_id, None)
    return ws


async def healthz(request: Request) -> Response:
    app = request.app
    stt = app["stt"]
    body = {"ok": True, "llm": app["llm"].mode, "stt": stt.provider,
            "tts": app["tts"].provider, "tone": TONE_MODE}
    if TONE_MODE == "pad":
        body["emotion"] = emotion.engine().describe()       # ready, floor, version, error (keyword tone if not ready)
    harness: Harness | None = app.get("harness")
    if harness is not None:                                 # SKWOOP_HARNESS=1 only: config and the proxy's own health
        body["harness"] = {"base": PROXY_BASE, "fast": FAST.model, "deep": DEEP.model,
                           "deep_fallback": harness.deep_fallback, "reminders": harness.reminders is not None,
                           "proxy": await proxy_health(app["http"], PROXY_BASE)}
    tts = app["tts"]
    if tts.provider == "elevenlabs":
        d = tts.describe()
        body["tts_model"] = d["model"]                  # eleven_v4_turbo unless ELEVENLABS_MODEL says otherwise
        body["tts_fallback"] = d["fallback"]            # xai | null
        body["tts_last_backend"] = d["last_backend"]    # elevenlabs | xai-fallback | null (nothing spoken yet)
        body["tts_fallbacks"] = d["fallbacks"]          # replies that used the fallback since start
        body["tts_last_error"] = d["last_error"]        # e.g. "ElevenLabs HTTP 401"; null after a good reply
        body["tts_voice"] = d["voice"]                  # ELEVENLABS_VOICE_ID (providers.voice_settings)
        body["tts_fallback_voice"] = d["fallback_voice"]  # XAI_FALLBACK_VOICE (default carina) | null
        body["tts_credits"] = d["credits"]              # credit_guard: remaining_est, floor, live_allowed, blocked
    lib = tts.library
    if lib is not None:
        d = lib.describe()
        body["library"] = {k: d[k] for k in ("ready", "voice", "clips", "sentences", "join", "hits", "error")}
        if lib.enabled and not lib.ready:
            body["ok"] = False  # clips.idx missing/stale/corrupt
    if "acks" in app:
        a = app["acks"].describe()
        body["ack_kinds"], body["ack_buckets"], body["ack_lines"] = a["kinds"], a["buckets"], a["lines"]
        body["ack_recordings"], body["ack_clips"] = a["recordings"], a["clips"]   # asked for; rendered
        # the clips must be in the live voice: false = they were rendered with another voice
        body["ack_voice"] = a["voice"]
        body["ack_voice_ok"] = (a["voice"] == voice_key(tts.provider)) if a["clips"] else None
        if a.get("error"):
            body["ack_error"] = a["error"]
    if "stitch" in app:
        d = app["stitch"].describe()
        body["stitch_templates"], body["stitch_clips"] = d["templates"], d["clips"]   # stitched speech pieces
    return web.json_response(body)


async def proxy_health(http: ClientSession, base: str) -> str:
    """The deep proxy's GET /health: "ok" (authenticated), "unauthenticated", or why it didn't answer."""
    url = base.rstrip("/").removesuffix("/v1") + "/health"
    try:
        async with http.get(url, timeout=ClientTimeout(total=1.0)) as r:
            raw: object = await r.json(content_type=None)
    except (ClientError, TimeoutError, ValueError) as e:
        return f"down ({type(e).__name__})"

    if not isinstance(raw, dict):
        return "bad answer"

    return "ok" if raw.get("authenticated") is True else "unauthenticated"


async def send_ack(app: Application, kind: str, room: str | None = None, device: str | None = None,
                   mood: str | None = None) -> AckSent:
    """Play an ack on a connected pod: the given device or room, else the one used most recently.

    mood: one of acks.MOODS (or "neutral"); default = the pod's current mood if this kind has it, else the
    kind's default mood. KeyError: unknown kind. ValueError: unknown mood. LookupError: no pod connected."""
    app["acks"].resolve_mood(kind, mood)  # validate kind and mood before looking for a pod
    live = [s for s in app["live"].values() if not s.ws.closed
            and (device is None or s.device_id.lower() == device.lower())
            and (room is None or s.room == room)]
    if not live:
        raise LookupError("no pod connected" + (f" for {device or room}" if device or room else ""))
    session = max(live, key=lambda s: s.last_activity)
    ack = app["acks"].pick(kind, mood, session.current_mood())
    lib = app["tts"].library
    if lib is not None and lib.ready and lib.has_lines(kind):   # a library line beats a stitched clip
        want = mood or session.current_mood() or ack["mood"]
        sk = lib.pick_line(kind, want)
        if sk is not None:
            emo = {**ACK_EMOTION, "calm": "neutral"}.get(sk["mood"], "neutral")
            ack = dict(ack, text=sk["text"], mood=sk["mood"], emotion=emo, pcm=sk["pcm"], source="skwoop-clip",
                       file=sk["id"])
    session.start_ack(ack)
    return {"ok": True, "kind": kind, "text": ack["text"], "mood": ack["mood"],
            "requested_mood": ack["requested"], "emotion": ack["emotion"], "source": ack["source"],
            "room": session.room}


async def ack_handler(request: Request) -> Response:
    if not request.app["store"]._authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response({"error": "expected JSON {\"kind\": ...}"}, status=400)
    kind = str(body.get("kind", ""))
    try:
        return web.json_response(await send_ack(request.app, kind, body.get("room"), body.get("device"),
                                                body.get("mood")))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except KeyError:
        return web.json_response({"error": f"unknown kind {kind!r}",
                                  "kinds": sorted(request.app["acks"].kinds)}, status=404)
    except LookupError as e:
        return web.json_response({"error": str(e)}, status=409)


async def ack_list(request: Request) -> Response:
    acks = request.app["acks"]
    acks.refresh()
    return web.json_response({**acks.describe(), "kinds_list": {
        k: {"default": s["default"], "moods": {
            m: {"tags": b["tags"], "takes": b["takes"], "lines": b["lines"], "clips": len(acks.clips(k, m))}
            for m, b in s["moods"].items()}}
        for k, s in acks.kinds.items()}})


async def send_template(app: Application, template: str, values: dict[str, SlotValue] | None = None,
                        room: str | None = None, device: str | None = None, mood: str | None = None,
                        dry_run: bool = False) -> TemplateResult:
    """Say a stitched template on a pod (stitch.py): "light_set" + {"pct": 50} -> "Okay, setting it to... fifty
    percent." from rendered pieces; live TTS if a piece is missing. mood: default = the pod's current mood if the
    template has it, else the template's default. dry_run: just return what it would say (no pod needed).
    KeyError: unknown template. ValueError: bad value or mood. LookupError: no pod connected."""
    st = app["stitch"]
    st.refresh()
    if template not in st.templates:
        raise KeyError(template)
    if mood is not None:
        from acks import norm_mood
        norm_mood(mood)                                  # ValueError for an unknown mood
    session = None
    if not dry_run:
        live = [s for s in app["live"].values() if not s.ws.closed
                and (device is None or s.device_id.lower() == device.lower())
                and (room is None or s.room == room)]
        if not live:
            raise LookupError("no pod connected" + (f" for {device or room}" if device or room else ""))
        session = max(live, key=lambda s: s.last_activity)
        if mood is None and session.current_mood() in (st.templates[template].get("moods") or []):
            mood = session.current_mood()
    said = st.say(template, values or {}, mood)
    out = {"ok": True, "template": template, "text": said["text"], "mood": said["mood"],
           "requested_mood": said["requested"], "emotion": said["emotion"], "source": said["source"],
           "pieces": said["pieces"], "missing": said["missing"]}
    if said["pcm"] is not None:
        out["audio_s"] = round(len(said["pcm"]) / 2 / TTS_RATE, 2)
    if session is not None:
        session.start_ack(said)
        out["room"] = session.room
    return out


@dataclass(frozen=True)
class PreviewRequest:
    text: str
    mood: str | None
    parts: tuple[str, ...]          # non-empty = speak these as one utterance (live parts get context)
    simulate_floor: bool            # true = the credit gate refuses: proves the xAI fallback, spends nothing


def parse_preview_request(raw: object) -> PreviewRequest:
    """The /speak-preview JSON body. ValueError with a short reason when it isn't one."""
    if not isinstance(raw, dict):
        raise ValueError("expected a JSON object")

    text, mood, parts, simulate = raw.get("text", ""), raw.get("mood"), raw.get("parts", []), raw.get("simulate")
    if not isinstance(text, str) or not (mood is None or isinstance(mood, str)):
        raise ValueError("text and mood must be strings")

    if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
        raise ValueError("parts must be a list of strings")

    if simulate not in (None, "floor"):
        raise ValueError('simulate must be "floor" or absent')

    if not text.strip() and not parts:
        raise ValueError("text or parts required")

    return PreviewRequest(text[:600], mood, tuple(p[:600] for p in parts[:8]), simulate == "floor")


async def speak_preview_handler(request: Request) -> Response:
    """POST /speak-preview {"text": ..., "mood"?, "parts"?: [...], "simulate"?: "floor"} (Bearer STATUS_WRITE_KEY)
    -> audio/wav of exactly what a pod would hear, with X-Skwoop-Backend (clip-whole | clip-b2 | elevenlabs |
    xai-fallback) and X-Skwoop-Credits headers. Nothing is sent to a pod. Used by the deploy smoke test and the sim."""
    if not request.app["store"]._authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        req = parse_preview_request(await request.json())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        return web.json_response({"error": str(e) or "bad JSON"}, status=400)

    tts: TTS = request.app["tts"]
    t0 = time.monotonic()
    try:
        if req.simulate_floor:
            pcm = await tts.synth_live(req.text, req.mood, gate=BlockAll())
        elif req.parts:
            pcm = await tts.synth_parts(list(req.parts), req.mood)
        else:
            pcm = await tts.synth(req.text, mood=req.mood)
    except Exception as e:  # noqa: BLE001 - report, keep serving
        log.warning("/speak-preview failed: %s", type(e).__name__)

        return web.json_response({"error": "tts failed", "detail": type(e).__name__}, status=502)

    g = tts.guard.describe()
    headers = {"X-Skwoop-Backend": tts.last_backend or tts.provider,
               "X-Skwoop-Ms": str(round((time.monotonic() - t0) * 1000)),
               "X-Skwoop-Seconds": str(round(len(pcm) / 2 / TTS_RATE, 2)),
               "X-Skwoop-Credits": (f"remaining_est={g['remaining_est']} floor={g['floor']} calls={g['calls']} "
                                  f"blocked={g['blocked']}")}

    return Response(body=pcm_to_wav(pcm, TTS_RATE), content_type="audio/wav", headers=headers)


async def say_template_handler(request: Request) -> Response:
    """POST /say-template {"template": "light_set", "values": {"pct": 50}, "room"?, "device"?, "mood"?,
    "dry_run"?} (Bearer STATUS_WRITE_KEY)."""
    if not request.app["store"]._authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return web.json_response({"error": "expected JSON {\"template\": ..., \"values\": {...}}"}, status=400)
    template = str(body.get("template", ""))
    values = body.get("values") or {}
    if not isinstance(values, dict):
        return web.json_response({"error": "values must be an object"}, status=400)
    try:
        return web.json_response(await send_template(request.app, template, values, body.get("room"),
                                                     body.get("device"), body.get("mood"),
                                                     bool(body.get("dry_run"))))
    except KeyError:
        return web.json_response({"error": f"unknown template {template!r}",
                                  "templates": sorted(request.app["stitch"].templates)}, status=404)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except LookupError as e:
        return web.json_response({"error": str(e)}, status=409)


async def say_template_list(request: Request) -> Response:
    st = request.app["stitch"]
    return web.json_response({**st.describe(), "templates_list": {
        name: {"parts": t["parts"], "moods": t.get("moods"), "default": t.get("default")}
        for name, t in sorted(st.templates.items())}})


MAX_STT_BYTES = 2 * 1024 * 1024  # ~60 s of 16 kHz mono PCM


async def stt_handler(request: Request) -> Response:
    """Transcribe one utterance from a pod (after its wake word). Same STT as a voice turn."""
    if not token_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.read()
    if not data:
        return web.json_response({"error": "empty body"}, status=400)
    if len(data) > MAX_STT_BYTES:
        return web.json_response({"error": "audio too long"}, status=413)
    try:
        if data[:4] == b"RIFF":
            pcm, rate = wav_to_pcm(data)
        else:
            pcm, rate = data, int(request.query.get("rate", "16000"))
    except (ValueError, wave.Error) as e:
        return web.json_response({"error": f"bad audio: {e}"}, status=400)
    stt = request.app["stt"]
    t0 = time.monotonic()
    try:
        text = await stt.transcribe(pcm, rate)
    except Exception as e:  # noqa: BLE001
        log.warning("/stt failed: %s", type(e).__name__)
        return web.json_response({"error": "stt failed", "detail": type(e).__name__}, status=502)
    return web.json_response({"text": text, "backend": stt.last_backend or stt.provider,
                              "stt_s": round(time.monotonic() - t0, 3), "audio_s": round(len(pcm) / 2 / rate, 2)})


async def no_weather() -> object:
    raise quick.WeatherUnavailable("bridge not started")


async def ring_timer(app: Application, device: str, room: str, seconds: int) -> None:
    """A quick timer went off: say it on that pod if it is connected, else post it as the pod's alert."""
    text = quick.timer_done_sentence(seconds)
    live = [s for s in app["live"].values() if not s.ws.closed and s.device_id.lower() == device.lower()]
    if not live:
        store: StatusStore = app["store"]
        try:
            store.set_alert(text, level="info", ttl_s=900, room=room)
        except ValueError:          # a room the status store doesn't know (a web pod): the house-wide alert
            store.set_alert(text, level="info", ttl_s=900)

        return

    pcm = await app["tts"].synth(text, mood="alert")
    live[0].start_ack({"kind": "timer_done", "text": text, "mood": "alert", "requested": "alert",
                       "emotion": ACK_EMOTION["alert"], "pcm": pcm, "source": "quick-timer"})


def wire_house(app: Application) -> None:
    """The house tools (Home Assistant, timers, texts, search on the subscription proxy, memory recall). Without
    HA_TOKEN there are none, and every model turn goes to Deep."""
    contacts = messaging.load_contacts()
    app["texts"] = messaging.Texts(contacts, messaging.reliable_sender())
    ha = ha_from_env(app["http"])
    if not ha.token:
        log.warning("no HA_TOKEN: no house tools, no pod harness; model turns go to Deep")
        return

    store: StatusStore = app["store"]
    app["house_tools"] = HouseTools(ha, app["quick"].timers, app["quick"], app["texts"],
                                    QuickSearch(app["http"], FAST.model, SearchBudget()),
                                    status_read.StatusSources(), app["show"], app["http"],
                                    pod_bots=lambda: list(store.feed()["bots"]),
                                    recall=MemoryRecall.from_env(app["http"]))
    store.next_item = app["house_tools"].next_event_lazy   # /status "next"; a stale prefetch refreshes behind it
    log.info("house tools: %d contacts", len(contacts))


def wire_harness(app: Application, environ: Mapping[str, str] = os.environ) -> None:
    """SKWOOP_HARNESS=1: the pod harness on Alex's subscription via the Deep proxy. Off builds nothing: every model
    turn goes to Deep. SKWOOP_DEEP_FALLBACK=0: no hand-off to Deep. SKWOOP_REMINDERS=0: no remind tool."""
    if environ.get("SKWOOP_HARNESS", "0").strip() != "1":
        log.info("pod harness off (SKWOOP_HARNESS=0): model turns go to Deep")
        return

    house_tools: HouseTools | None = app.get("house_tools")
    if house_tools is None:
        log.warning("pod harness needs the house tools (HA_TOKEN): model turns go to Deep")
        return

    http, store = app["http"], app["store"]
    fallback = environ.get("SKWOOP_DEEP_FALLBACK", "1").strip() != "0"
    reminders = Reminders(house_tools.ha.service) if environ.get("SKWOOP_REMINDERS", "1").strip() != "0" else None
    writer = MemoryWriter(MemoryService(http), lambda: datetime.now(TZ))
    app["harness"] = Harness(ProxyClient(http), house_tools, writer,
                             Grokipedia(http), reminders, fallback,
                             alert=lambda text: store.set_alert(text, level="warn", ttl_s=6 * 3600))
    log.info("pod harness ON via %s: fast %s, deep %s, Deep fallback %s, reminders %s", PROXY_BASE, FAST.model,
             DEEP.model, "on" if fallback else "off", "on (calendar.work, 60 s hold)" if reminders else "off")


async def on_startup(app: Application) -> None:
    app["http"] = ClientSession()
    app["stt"] = STT(app["http"])
    app["tts"] = TTS(app["http"])
    app["llm"] = LLM(app["http"])
    app["store"].inbox.synth = app["tts"].synth  # messages are rendered once, at queue time
    app["store"].inbox.synth_parts = app["tts"].synth_parts   # bodies get previous_text/next_text
    app["show"] = house.show_context(app["tts"].library)        # "play <title>": Apple TV links + title casing
    weather = quick.weather_from_env(app["http"])
    app["quick"] = quick.QuickAnswers(weather, app["quick"].timers)
    app["fillers"] = FillerSet.load()
    app["filler_timing"] = timing_from_env()
    wire_house(app)
    wire_harness(app)
    app["music"] = Music.from_env(app["http"])   # off: touch ~/.skwoop/music-fast.off (no restart)
    app["prewarm"] = Prewarm(app["http"], warm_targets(), weather, house=app.get("house_tools"))
    app["prewarm"].kick()
    if (tools := app.get("house_tools")) is not None:
        tools.kick_calendars()                     # once now; later only when a turn or a /status read finds it stale
    if (h := app.get("harness")) is not None and h.reminders is not None:
        h.reminders.start()                        # one timer at the next due reminder (restarts included), else none
    try:
        await house.warm(app["tts"])
    except Exception as e:
        log.warning("ack warm failed: %s", type(e).__name__)


async def on_cleanup(app: Application) -> None:
    if (h := app.get("harness")) is not None and h.reminders is not None:
        h.reminders.stop()
    await app["http"].close()


def build_app(port: int) -> Application:
    app = Application()
    app["port"] = port
    app["store"] = store = StatusStore()
    if not store.read_key:
        log.warning("STATUS_READ_KEY not set: GET /status and /face.png answer 401 (pods and the widget go blank)")
    store.inbox = InboxStore(rooms=store.rooms, on_change=store.changed)
    app["arbiter"] = WakeArbiter()
    app["sessions"] = {}  # session id -> room, for "that message is playing on the kitchen pod"
    app["live"] = {}      # session id -> Session, for POST /ack
    app["show"] = house.show_context()   # replaced at startup with the library-aware one
    # replaced at startup: no fillers, and no Home Assistant weather until the HTTP session exists
    app["fillers"] = FillerSet((), Path())
    app["ask_deep"] = skwoop_turn.ask
    app["ask_deep_stream"] = skwoop_turn.ask_stream if deep_model.configured() else None  # OFF_FILE: live off
    app["filler_timing"] = FillerTiming()
    app["quick"] = quick.QuickAnswers(quick.WeatherSource(no_weather), quick.TimerBook(
        ring=lambda d, r, s: ring_timer(app, d, r, s)))
    app["acks"] = AckLibrary()
    app["stitch"] = StitchLibrary(whole_lines=[app["acks"]])   # a recorded ack with the same words beats stitching
    app.add_routes([
        web.get("/xiaozhi/ota/", ota), web.post("/xiaozhi/ota/", ota),
        web.get("/xiaozhi/v1/", ws_handler), web.get("/xiaozhi/v1", ws_handler),
        web.get("/healthz", healthz),
        web.post("/stt", stt_handler),
        web.post("/ack", ack_handler), web.get("/ack", ack_list),
        web.post("/say-template", say_template_handler), web.get("/say-template", say_template_list),
        web.post("/speak-preview", speak_preview_handler),
        *store.routes(),
        *store.inbox.routes(),
    ])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    port = int(env("SKWOOP_PORT", "8003"))
    if not DEVICE_TOKEN:
        log.warning("SKWOOP_DEVICE_TOKEN not set: any LAN client can open a voice session")
    log.info("reply tone: %s (SKWOOP_TONE)", TONE_MODE)
    if TONE_MODE == "pad":
        emotion.engine()                                    # load the model before the first turn
    # SKWOOP_HOST defaults to 0.0.0.0 so LAN pods can reach the bridge; lean_http itself defaults to loopback.
    web.run_app(build_app(port), host=env("SKWOOP_HOST", "0.0.0.0"), port=port, access_log=None)


if __name__ == "__main__":
    main()
