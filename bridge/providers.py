"""Skwoop bridge speech-to-text, text-to-speech and model backends.

Every secret comes from the environment; nothing is logged except provider names.
Audio contract: STT takes 16-bit mono PCM + sample rate; TTS returns 16-bit mono
PCM at TTS_RATE (24 kHz, matching the xiaozhi downlink).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import wave
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlencode, urlparse

from lean_http import ClientError, ClientSession, ClientTimeout, ClientWebSocketResponse, FormData
from lean_ws import WSMsgType

from dsp import mono, resample

if TYPE_CHECKING:
    from credit_guard import CreditGate, CreditGuard
    from skwoop_lib import SkwoopLibrary

log = logging.getLogger("skwoop.providers")

TTS_RATE = 24000
XAI_BASE = "https://api.x.ai/v1"
ELEVEN_BASE = "https://api.elevenlabs.io"
ELEVEN_MODEL = "eleven_v4_turbo"   # Eleven v4 Turbo (elevenlabs.io/docs/overview/models)
ELEVEN_VOICE = ""  # live id comes from ELEVENLABS_VOICE_ID; empty until that is set
# Delivery tags the clip library was rendered with; live replies use the same ones so
# a live sentence sounds like the clips around it. ELEVENLABS_TONE=mood (default) | tags (old happy/sad) | off.
ELEVEN_MOOD_TAGS = {"happy": "[cheerfully]", "idle": "[warmly]", "sad": "[apologetically]", "alert": "[crisply]",
                    "calm": "[softly]",
                    # emotion engine v0.1 (SKWOOP_TONE=pad) moods; Alex, Oct 4: plain stays [warmly], playful and
                    # curious are their own tags. Keyword tone never produces these three.
                    "plain": "[warmly]", "playful": "[playfully]", "curious": "[curious]"}
ELEVEN_TONE_TAGS = {"happy": "[cheerfully]", "sad": "[gently]"}  # ELEVENLABS_TONE=tags; neutral = no tag
XAI_VOICE = "eve"                  # TTS_PROVIDER=xai: the bridge's voice today
XAI_FALLBACK_VOICE = "carina"      # xAI voice when ElevenLabs fails: "soft, empathetic, soothing", closest to Skwoop


def forbidden_hosts() -> frozenset[str]:
    """Hosts named in SKWOOP_FORBIDDEN_LLM_HOSTS. Empty means the URL's host is allowed."""
    return frozenset(h.strip().lower() for h in env("SKWOOP_FORBIDDEN_LLM_HOSTS").split(",") if h.strip())


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def emotion_floor(text: str, mood: str | None) -> str | None:
    """SKWOOP_TONE=pad urgency floor (emotion.floor_mood: urgent text -> "alert", "alert" is never lowered).
    Any other SKWOOP_TONE: mood unchanged, no model code imported."""
    if env("SKWOOP_TONE", "keyword").lower() != "pad":
        return mood

    from skwoop_mood.emotion import floor_mood

    return floor_mood(text, mood)


class VoiceSettings(TypedDict):
    eleven_voice_id: str
    eleven_model: str
    eleven_stability: float | None
    xai_voice: str
    xai_fallback_voice: str


def voice_settings() -> VoiceSettings:
    """Skwoop's voice, defined in one place. Live replies (TTS below), the ack clips (render_acks.py) and the
    voice auditions all read it from here, i.e. from the env file, so they can't drift apart:
      ELEVENLABS_VOICE_ID   empty unless set (ELEVEN_VOICE is only the fallback when the env var is blank)
      ELEVENLABS_MODEL      default eleven_v4_turbo
      ELEVENLABS_STABILITY  0-1, unset = the voice's saved setting
      XAI_TTS_VOICE         TTS_PROVIDER=xai voice (default eve)
      XAI_FALLBACK_VOICE    xAI voice when ElevenLabs fails (default carina, chosen to sound like Skwoop)"""
    stability = env("ELEVENLABS_STABILITY")
    return {"eleven_voice_id": env("ELEVENLABS_VOICE_ID") or ELEVEN_VOICE,
            "eleven_model": env("ELEVENLABS_MODEL", ELEVEN_MODEL),
            "eleven_stability": float(stability) if stability else None,
            "xai_voice": env("XAI_TTS_VOICE", XAI_VOICE),
            "xai_fallback_voice": env("XAI_FALLBACK_VOICE", XAI_FALLBACK_VOICE)}


def voice_key(provider: str) -> str:
    """What a recording was spoken with. The clip manifest stores it and the bridge compares it with the live voice
    (/healthz ack_voice_ok), so a voice change without a re-render shows up."""
    v = voice_settings()
    if provider == "elevenlabs":
        return f"elevenlabs:{v['eleven_voice_id']}:{v['eleven_model']}:{env('ELEVENLABS_STABILITY')}"
    if provider == "xai":
        return f"xai:{v['xai_voice']}"
    if provider == "say":
        return f"say:{env('SAY_VOICE')}"
    return provider


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit wav")
    return (mono(frames, ch) if ch > 1 else frames), rate


# ----------------------------------------------------------------------------
# Speech to text
# ----------------------------------------------------------------------------
class STT:
    """STT_PROVIDER = xai (the only one): batch POST /stt, or the streaming websocket (XaiSttStream)."""

    def __init__(self, http: ClientSession):
        self.http = http
        self.provider = env("STT_PROVIDER", "xai")
        self.last_backend = ""
        if self.provider != "xai":
            raise SystemExit(f"STT_PROVIDER={self.provider} is gone: xai is the only STT")
        if not env("XAI_API_KEY"):
            raise SystemExit("STT_PROVIDER=xai needs XAI_API_KEY in the environment")

    def describe(self) -> dict[str, str | None]:
        return {"provider": self.provider, "last_backend": self.last_backend or None}

    def streaming_enabled(self) -> bool:
        return env("STT_STREAM", "1").lower() not in ("0", "false", "no", "off")

    async def transcribe(self, pcm: bytes, rate: int) -> str:
        self.last_backend = self.provider
        form = FormData()
        form.add_field("model", env("STT_MODEL", "grok-voice-transcribe-2.0"))
        form.add_field("language", env("STT_LANGUAGE", "en"))
        form.add_field("file", pcm_to_wav(pcm, rate), filename="utterance.wav", content_type="audio/wav")
        async with self.http.post(f"{XAI_BASE}/stt", data=form,
                                  headers={"Authorization": f"Bearer {env('XAI_API_KEY')}"},
                                  timeout=ClientTimeout(total=30)) as r:
            if r.status != 200:
                err = (await r.text())[:180].replace("\n", " ")
                raise RuntimeError(f"xAI STT HTTP {r.status}: {err}")
            data = await r.json()
        text = data.get("text") if isinstance(data, dict) else ""
        return (text if isinstance(text, str) else "").strip()

    def open_stream(self, rate: int) -> XaiSttStream:
        return XaiSttStream(self.http, rate)


def stt_event_text(event: dict[str, object]) -> tuple[str, bool]:
    """Parse one xAI streaming STT event. Returns (text, utterance_done)."""
    t = event.get("type")
    if t == "error":
        raise RuntimeError(str(event.get("message") or "stt error")[:180])
    if t == "transcript.partial":
        raw = event.get("text")
        return (raw.strip() if isinstance(raw, str) else ""), bool(event.get("speech_final"))
    if t == "transcript.done":
        raw = event.get("text")
        return (raw.strip() if isinstance(raw, str) else ""), True
    return "", False


def stt_interim_enabled() -> bool:
    """STT_INTERIM=1 (default): ask xAI for interim hypotheses (~every 500 ms). 0 = the old finals-only stream."""
    return env("STT_INTERIM", "1").lower() not in ("0", "false", "no", "off")


def stt_is_interim(event: dict[str, object]) -> bool:
    """An interim hypothesis (is_final=false): its text may still change. It never feeds the transcript or ends
    the turn; only chunk-final and utterance-final events do, exactly as with interim_results=false."""
    return (event.get("type") == "transcript.partial" and event.get("is_final") is False
            and not event.get("speech_final"))


_closing: set[asyncio.Task[None]] = set()      # STT sockets being closed in the background


class XaiSttStream:
    """Live PCM → xAI STT websocket. Ends on Smart Turn speech_final."""

    def __init__(self, http: ClientSession, rate: int):
        self.http = http
        self.rate = int(rate or 16000)
        self._ws: ClientWebSocketResponse | None = None
        self._ready = asyncio.Event()
        self._done = asyncio.Event()
        self._text = ""
        self._err: BaseException | None = None
        self._recv_task: asyncio.Task[None] | None = None
        # interim results: listening-side only (logged; on_interim for a future face mirror), never the transcript
        self.on_interim: Callable[[str], None] | None = None
        self.interims = 0
        self.interim_text = ""
        self.first_interim_s: float | None = None    # first interim, seconds after the first audio pushed
        self._t0: float | None = None

    def _url(self) -> str:
        params = {
            "model": env("STT_MODEL", "grok-voice-transcribe-2.0"),
            "sample_rate": str(self.rate),
            "encoding": "pcm",
            "language": env("STT_LANGUAGE", "en") or "en",
            "smart_turn": env("STT_SMART_TURN", "0.7"),
            "smart_turn_timeout": env("STT_SMART_TURN_TIMEOUT_MS", "3000"),
            "interim_results": "true" if stt_interim_enabled() else "false",
        }
        return f"wss://api.x.ai/v1/stt?{urlencode(params)}"

    async def start(self) -> None:
        headers = {"Authorization": f"Bearer {env('XAI_API_KEY')}"}
        self._ws = await self.http.ws_connect(self._url(), headers=headers, heartbeat=20)
        self._recv_task = asyncio.create_task(self._recv())
        await asyncio.wait_for(self._ready.wait(), 8)

    async def _recv(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        ev = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._on_event(ev)
                    if self._done.is_set():
                        break
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._err = e
        finally:
            self._ready.set()
            self._done.set()

    async def _on_event(self, ev: dict[str, object]) -> None:
        t = ev.get("type")
        if t == "transcript.created":
            self._ready.set()
            return
        if stt_is_interim(ev):
            self._interim(ev)
            return
        try:
            text, done = stt_event_text(ev)
        except Exception as e:
            self._err = e
            self._done.set()
            return
        if text:
            self._text = text
        if done:
            conf = ev.get("end_of_turn_confidence")
            log.info("stt stream final chars=%s conf=%s", len(self._text), conf)
            if self.interims:
                log.info("stt interim summary n=%d first=%.2fs final=%.2fs", self.interims, self.first_interim_s or 0,
                         time.monotonic() - self._t0 if self._t0 is not None else 0)
            try:
                if self._ws is not None and not self._ws.closed:
                    await self._ws.send_str(json.dumps({"type": "audio.done"}))
            except Exception:
                pass
            self._done.set()

    def _interim(self, ev: dict[str, object]) -> None:
        """Count, log (length only, no words) and hand off one interim hypothesis. Never raises."""
        try:
            raw = ev.get("text")
            text = raw.strip() if isinstance(raw, str) else ""
            self.interims += 1
            dt = time.monotonic() - self._t0 if self._t0 is not None else 0.0
            if self.first_interim_s is None:
                self.first_interim_s = dt
            self.interim_text = text
            log.info("stt interim #%d t=%.2fs chars=%d", self.interims, dt, len(text))
            if self.on_interim is not None and text:
                self.on_interim(text)
        except Exception as e:  # noqa: BLE001 - the listening extra must never break the turn
            log.debug("stt interim handler failed: %s", type(e).__name__)

    async def push(self, pcm: bytes) -> None:
        if not pcm or self._done.is_set():
            return
        if self._t0 is None:
            self._t0 = time.monotonic()
        ws = self._ws
        if ws is None or ws.closed:
            return
        await ws.send_bytes(pcm)

    async def finalize(self) -> None:
        ws = self._ws
        if ws is None or ws.closed or self._done.is_set():
            return
        await ws.send_str(json.dumps({"type": "finalize"}))

    async def wait(self, timeout: float = 90.0) -> str:
        await asyncio.wait_for(self._done.wait(), timeout)
        if self._err:
            raise self._err
        return self._text.strip()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    async def close(self) -> None:
        """Stop reading and close the socket in the background: xAI takes ~2 s to answer the close handshake, and
        the turn must not wait for it (the transcript is already in hand)."""
        self._done.set()
        if self._recv_task:
            self._recv_task.cancel()

        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            task = asyncio.create_task(ws.close())
            _closing.add(task)
            task.add_done_callback(_closing.discard)


# ----------------------------------------------------------------------------
# Text to speech
# ----------------------------------------------------------------------------
class TTS:
    """TTS_PROVIDER = elevenlabs (default when ELEVENLABS_API_KEY is set; falls back to TTS_FALLBACK=xai) | xai |
    say (macOS, free: the offline benches).

    synth() order for SkwoopOS speech: the SkwoopOS clip library (skwoop_lib: whole sentence -> b2 overlap join)
    -> live ElevenLabs (credit_guard: stops at ELEVEN_CREDIT_FLOOR) -> xAI TTS. One voice: every reply goes this
    way, whichever model wrote it (Grokbot, Deep, a local model); xAI speaks only below the floor or on an outage."""

    def __init__(self, http: ClientSession | None, library: SkwoopLibrary | None = None,
                 guard: CreditGuard | None = None):
        from credit_guard import CreditGuard
        from skwoop_lib import SkwoopLibrary

        self.http = http
        self.provider = env("TTS_PROVIDER") or ("elevenlabs" if env("ELEVENLABS_API_KEY") else "xai")
        self.library: SkwoopLibrary | None = library if library is not None else SkwoopLibrary()
        self.guard: CreditGuard = guard if guard is not None else CreditGuard()
        self.last_cost = 0.0
        self.fallback = ""
        self.last_backend = ""
        self.last_error = ""
        self.fallbacks = 0
        self.credits = 0          # ElevenLabs credits charged so far (the character-cost response header)
        if self.provider == "elevenlabs":
            self.fallback = env("TTS_FALLBACK", "xai")      # xai | none
            if self.fallback == "xai" and not env("XAI_API_KEY"):
                log.warning("TTS_FALLBACK=xai but no XAI_API_KEY: ElevenLabs TTS has no fallback")
                self.fallback = ""
            if self.fallback not in ("xai", ""):
                self.fallback = ""
            if not env("ELEVENLABS_API_KEY"):
                if not self.fallback:
                    raise SystemExit("TTS_PROVIDER=elevenlabs needs ELEVENLABS_API_KEY "
                                     "(or XAI_API_KEY for the xAI fallback)")
                log.warning("ELEVENLABS_API_KEY missing: every reply will use xAI TTS")
        if self.provider == "xai" and not env("XAI_API_KEY"):
            raise SystemExit("TTS_PROVIDER=xai needs XAI_API_KEY in the environment")
        if self.provider == "say" and not shutil.which("say"):
            raise SystemExit("TTS_PROVIDER=say only works on macOS")

    def describe(self) -> dict[str, object]:
        d: dict[str, object] = {"provider": self.provider, "fallback": self.fallback or None,
                                "last_backend": self.last_backend or None}
        if self.provider == "elevenlabs":
            v = voice_settings()
            d["model"] = v["eleven_model"]
            d["fallbacks"] = self.fallbacks
            d["last_error"] = self.last_error or None
            d["voice"] = v["eleven_voice_id"] or None
            d["fallback_voice"] = v["xai_fallback_voice"] if self.fallback == "xai" else None
            d["credits"] = self.guard.describe()
        if self.library is not None and self.library.enabled:
            d["library"] = self.library.describe()

        return d

    # ---------- SkwoopOS speech: library -> live (guarded ElevenLabs) -> xAI ----------
    def _from_library(self, text: str, mood: str | None) -> bytes | None:
        if self.library is None:
            return None
        if self.library.enabled and not self.library.ready:
            # Index missing/stale/corrupt: do not spend ElevenLabs.
            log.error("tts library not ready (%s): refusing live TTS for %r", self.library.error, text[:60])
            self.last_backend, self.last_error = "library-down", self.library.error or "not ready"
            return b""
        try:
            r = self.library.render(text, mood)
        except LookupError as e:
            log.error("tts library render failed (%s): refusing live TTS for %r", e, text[:60])
            self.last_backend, self.last_error = "library-error", str(e)
            return b""
        if r is None:
            return None

        self.last_backend = f"clip-{r.found.how}"
        log.info("tts %s (%s, %.1f ms%s) %r", self.last_backend, r.found.mood, r.ms,
                 ", v3 fallback" if r.v3_fallback else "", text[:80])

        return r.pcm

    async def synth_parts(self, parts: list[str], mood: str | None = None, gap_s: float = 0.12,
                          context_before: str | None = None, context_after: str | None = None) -> bytes:
        """Several sentences as one utterance: each from the library if it can be, else live. A live part gets its
        neighbours as previous_text/next_text so it is spoken as the middle of that sentence run (text-message
        bodies between "Here's your text to Mel." and "Should I send it?")."""
        said = [c for c in (clean_for_speech(p) for p in parts) if c]
        gap = b"\x00\x00" * int(TTS_RATE * gap_s)
        out: list[bytes] = []
        for i, part in enumerate(said):
            pcm = self._from_library(part, mood)
            if pcm is None:
                before = " ".join(x for x in (context_before, *said[:i]) if x)
                after = " ".join(x for x in (*said[i + 1:], context_after) if x)
                pcm = await self._live(part, mood, previous_text=before or None, next_text=after or None)

            if pcm:
                out.append(pcm)

        return gap.join(out)

    async def synth(self, text: str, mood: str | None = None, previous_text: str | None = None,
                    next_text: str | None = None) -> bytes:
        text = clean_for_speech(text)
        if not text:
            return b""

        mood = emotion_floor(text, mood)
        pcm = self._from_library(text, mood)
        if pcm is not None:
            return pcm  # includes b"" when the library is down (fail closed)

        if self.library is not None and self.library.ready:
            sents = split_sentences(text)
            if len(sents) > 1 and any(self.library.find(x, mood) for x in sents):
                return await self.synth_parts(sents, mood)

        return await self._live(text, mood, previous_text, next_text)

    async def synth_stream(self, text: str, mood: str | None = None, previous_text: str | None = None,
                           next_text: str | None = None) -> AsyncIterator[bytes]:
        """synth() as the audio arrives. Library clips and other providers come in one piece; live ElevenLabs comes
        in ~100 ms chunks from its HTTP stream. A failure before the first chunk falls back like synth(); after
        it the sentence stops where the audio did. SKWOOP_TTS_STREAM=0: always one piece."""
        clean = clean_for_speech(text)
        if not clean:
            return

        mood = emotion_floor(clean, mood)
        if (env("SKWOOP_TTS_STREAM", "1") == "0" or self.provider != "elevenlabs" or not env("ELEVENLABS_API_KEY")
                or self._library_covers(clean, mood)):
            yield await self.synth(clean, mood, previous_text, next_text)
            return
        ok, _ = self.guard.allow(clean)
        if not ok:                                   # floor reached: synth() takes the fallback path
            yield await self._live(clean, mood, previous_text, next_text)
            return
        cost: list[float] = []                       # this call's character-cost (parallel calls share self)
        got, failed = 0, None
        try:
            async for chunk in self._elevenlabs_stream(clean, mood, previous_text, next_text, cost=cost):
                if not got:
                    self.last_backend, self.last_error = "elevenlabs", ""
                got += len(chunk)
                yield chunk
        except Exception as e:  # noqa: BLE001 - any ElevenLabs failure -> fallback (before audio) or a short stop
            failed = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        finally:
            if got or cost:                          # billed: count it even when cut short (barge-in, error)
                self.guard.charge(clean, cost[0] if cost and cost[0] else None)
        if failed is None:
            log.info("tts elevenlabs stream %d chars, %.0f credits (~%.0f left) %r", len(clean), cost[0] if cost else 0,
                     self.guard.remaining() or 0, clean[:80])
            return
        self.last_error = failed
        if got:
            log.warning("ElevenLabs stream cut after %.1f s of audio: %s", got / 2 / TTS_RATE, failed)
            return
        if self.fallback != "xai":
            raise RuntimeError(f"ElevenLabs TTS failed ({failed}) and no TTS_FALLBACK")
        self.fallbacks += 1
        log.warning("ElevenLabs TTS failed (%s): using xAI TTS", failed)
        self.last_backend = "xai-fallback"
        yield await self._xai(clean, voice_settings()["xai_fallback_voice"])

    def _library_covers(self, text: str, mood: str | None) -> bool:
        """synth() would answer from the clip library (whole sentence, or some sentences of several)."""
        if self.library is None:
            return False
        sents = split_sentences(text)
        return any(self.library.find(x, mood) for x in sents)

    async def _live(self, text: str, mood: str | None = None, previous_text: str | None = None,
                    next_text: str | None = None) -> bytes:
        if self.provider == "elevenlabs":
            return await self._eleven_or_fallback(text, mood, previous_text, next_text)

        self.last_backend = self.provider
        if self.provider == "xai":
            try:
                return await self._xai(text)
            except Exception as e:
                if shutil.which("say"):
                    log.warning("xAI TTS failed (%s); using macOS say", type(e).__name__)

                    return await self._say(text)

                raise

        if self.provider == "say":
            return await self._say(text)

        raise ValueError(f"unknown TTS_PROVIDER {self.provider}")

    async def synth_live(self, text: str, mood: str | None = None, gate: CreditGate | None = None) -> bytes:
        """Skip the library: live ElevenLabs behind `gate` (default: the credit guard), else the xAI fallback."""
        clean = clean_for_speech(text)
        return await self._eleven_or_fallback(clean, emotion_floor(clean, mood), gate=gate)

    async def _guarded_eleven(self, text: str, mood: str | None, previous_text: str | None, next_text: str | None,
                              gate: CreditGate) -> bytes:
        """One live ElevenLabs call behind the credit gate. RuntimeError(why) when the gate or the key says no."""
        if not env("ELEVENLABS_API_KEY"):
            raise RuntimeError("no ELEVENLABS_API_KEY")

        ok, why = gate.allow(text)
        if not ok:
            raise RuntimeError(why)

        self.last_cost = 0.0
        try:
            pcm = await self._elevenlabs(text, mood, previous_text, next_text)
        except Exception:
            if self.last_cost:                       # billed (header seen) but unusable: still count it
                gate.charge(text, self.last_cost)

            raise

        gate.charge(text, self.last_cost or None)    # no header: the conservative estimate

        return pcm

    async def _eleven_or_fallback(self, text: str, mood: str | None = None, previous_text: str | None = None,
                                  next_text: str | None = None, gate: CreditGate | None = None) -> bytes:
        """ElevenLabs first; xAI TTS on no key, the credit floor, HTTP error, timeout or empty audio.
        gate: a stand-in for the credit guard (POST /speak-preview simulate=floor proves the fallback for free)."""
        try:
            pcm = await self._guarded_eleven(text, mood, previous_text, next_text, gate or self.guard)
        except Exception as e:  # noqa: BLE001 - any ElevenLabs failure -> fallback
            why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
            self.last_error = why
        else:
            self.last_backend, self.last_error = "elevenlabs", ""
            log.info("tts elevenlabs live %d chars, %.0f credits (~%.0f left) %r", len(text), self.last_cost,
                     self.guard.remaining() or 0, text[:80])

            return pcm

        if self.fallback != "xai":
            raise RuntimeError(f"ElevenLabs TTS failed ({why}) and no TTS_FALLBACK")

        self.fallbacks += 1
        log.warning("ElevenLabs TTS failed (%s): using xAI TTS", why)
        self.last_backend = "xai-fallback"

        return await self._xai(text, voice_settings()["xai_fallback_voice"])

    def _eleven_request(self, text: str, mood: str | None, previous_text: str | None,
                        next_text: str | None) -> tuple[str, dict[str, object]]:
        base = env("ELEVENLABS_BASE_URL", ELEVEN_BASE).rstrip("/")
        vs = voice_settings()
        voice, model = vs["eleven_voice_id"], vs["eleven_model"]
        tone_mode = env("ELEVENLABS_TONE", "mood")
        if tone_mode == "mood":                      # the try-8 library's delivery tags, by the reply's mood
            from skwoop_lib import speak_mood
            from tone import classify_tone
            m = speak_mood(mood or classify_tone(text))
            tag = ELEVEN_MOOD_TAGS.get(m)
            text = f"{tag} {text}" if tag else text
        elif tone_mode == "tags":  # light hint: v4 reads [audio tags] as delivery cues
            from tone import classify_tone
            tag = ELEVEN_TONE_TAGS.get(classify_tone(text))
            text = f"{tag} {text}" if tag else text
        settings = {"stability": vs["eleven_stability"]} if vs["eleven_stability"] is not None else None
        body: dict[str, object]
        if env("ELEVENLABS_ENDPOINT", "tts") == "dialogue":  # Text to Dialogue HTTP stream, one voice
            url = f"{base}/v1/text-to-dialogue/stream"
            body = {"inputs": [{"text": text, "voice_id": voice}], "model_id": model}
            if settings:
                body["settings"] = settings
        else:
            url = f"{base}/v1/text-to-speech/{voice}/stream"
            body = {"text": text, "model_id": model}
            if settings:
                body["voice_settings"] = settings
            if previous_text:                        # context only: not spoken, not charged
                body["previous_text"] = previous_text[-500:]
            if next_text:
                body["next_text"] = next_text[:500]
        if env("TTS_LANGUAGE"):
            body["language_code"] = env("TTS_LANGUAGE")
        return url, body

    async def _elevenlabs_stream(self, text: str, mood: str | None = None, previous_text: str | None = None,
                                 next_text: str | None = None, cost: list[float] | None = None) -> AsyncIterator[bytes]:
        """s16le mono @ TTS_RATE as ElevenLabs sends it (even-length chunks). The character-cost header goes to
        last_cost and `cost` before the first chunk."""
        url, body = self._eleven_request(text, mood, previous_text, next_text)
        http = self.http
        if http is None:
            raise RuntimeError("no HTTP session")
        async with http.post(url, params={"output_format": f"pcm_{TTS_RATE}"}, json=body,
                                  headers={"xi-api-key": env("ELEVENLABS_API_KEY")},
                                  timeout=ClientTimeout(total=float(env("ELEVENLABS_TIMEOUT_S", "10")))) as r:
            if r.status != 200:
                raise RuntimeError(f"ElevenLabs HTTP {r.status}")
            try:
                self.last_cost = float(r.headers.get("character-cost") or 0)
                self.credits += int(self.last_cost)
                if cost is not None:
                    cost.append(self.last_cost)
            except ValueError:
                pass
            carry, sent = b"", 0
            async for chunk in r.content.iter_chunked(TTS_RATE // 5):          # ~100 ms of audio
                data = carry + chunk
                if not sent and data[:4] == b"RIFF":                          # a wav container: take it whole
                    pcm, rate = wav_to_pcm(data + await r.read())
                    yield resample(pcm, rate, TTS_RATE)
                    return
                cut = len(data) // 2 * 2
                carry = data[cut:]
                if cut:
                    sent += cut
                    yield data[:cut]
            if not sent:
                raise RuntimeError("ElevenLabs returned no audio")

    async def _elevenlabs(self, text: str, mood: str | None = None, previous_text: str | None = None,
                          next_text: str | None = None) -> bytes:
        return b"".join([c async for c in self._elevenlabs_stream(text, mood, previous_text, next_text)])

    async def _xai(self, text: str, voice: str | None = None) -> bytes:
        body = {"text": text, "voice_id": voice or voice_settings()["xai_voice"], "language": env("TTS_LANGUAGE", "en"),
                "output_format": {"codec": "wav", "sample_rate": TTS_RATE}}  # wav: what the live bridge host uses
        http = self.http
        if http is None:
            raise RuntimeError("no HTTP session")
        async with http.post(f"{XAI_BASE}/tts", json=body,
                                  headers={"Authorization": f"Bearer {env('XAI_API_KEY')}"},
                                  timeout=ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError(f"xAI TTS HTTP {r.status}")
            data = await r.read()
        if data[:4] == b"RIFF":
            pcm, rate = wav_to_pcm(data)
            return resample(pcm, rate, TTS_RATE)
        return data

    async def _say(self, text: str) -> bytes:
        with tempfile.TemporaryDirectory() as d:
            aiff, wav = os.path.join(d, "o.aiff"), os.path.join(d, "o.wav")
            args = ["say", "-o", aiff]
            if env("SAY_VOICE"):
                args += ["-v", env("SAY_VOICE")]
            p = await asyncio.create_subprocess_exec(*args, text)
            await p.wait()
            p = await asyncio.create_subprocess_exec("afconvert", "-f", "WAVE", "-d", f"LEI16@{TTS_RATE}",
                                                     "-c", "1", aiff, wav)
            await p.wait()
            pcm, rate = wav_to_pcm(open(wav, "rb").read())
        return resample(pcm, rate, TTS_RATE)


def split_sentences(text: str) -> list[str]:
    """'Okay. The kitchen lights are on!' -> ['Okay.', 'The kitchen lights are on!'] (keeps 'Mr.'-free text simple)."""
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text.strip()) if p.strip()]


def _sentence_end(buf: str, min_chars: int = 20) -> int | None:
    """Index just past the first sentence end after min_chars, or None."""
    m = re.search(r"[.!?][\"')\]]*\s|\n", buf[min_chars:])
    return None if m is None else min_chars + m.end()


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_#`>|]+", "", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)  # markdown links
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------------------------------------------------------
# LLM (OpenAI-compatible deep model, or xAI direct)
# ----------------------------------------------------------------------------
class LLM:
    """LLM_MODE = deep (default when DEEP_MODEL_BASE_URL is set) | xai

    stream_sentences(fast=True) is for simple questions: it goes straight to xAI with LLM_FAST_MODEL
    and falls back to the normal route if that fails. LLM_FAST_MODEL=off turns it off."""

    def __init__(self, http: ClientSession):
        self.http = http
        self.mode = env("LLM_MODE") or ("deep" if env("DEEP_MODEL_BASE_URL") else "xai")
        self.fallback_xai = env("LLM_FALLBACK_XAI", "1") == "1" and bool(env("XAI_API_KEY"))
        fast = env("LLM_FAST_MODEL", "grok-4.20-0309-non-reasoning")
        self.fast_model = fast if fast.lower() not in ("off", "0", "none", "") and env("XAI_API_KEY") else ""
        if self.mode == "deep":
            self.base, self.key = env("DEEP_MODEL_BASE_URL").rstrip("/"), env("DEEP_MODEL_API_KEY")
            self.model = env("DEEP_MODEL_NAME", "deep")
            if not self.base or not self.key:
                raise SystemExit("LLM_MODE=deep needs DEEP_MODEL_BASE_URL and DEEP_MODEL_API_KEY")
        elif self.mode == "xai":
            self.base, self.key = XAI_BASE, env("XAI_API_KEY")
            self.model = env("XAI_MODEL", "grok-4.20-0309-non-reasoning")
            if not self.key:
                raise SystemExit("LLM_MODE=xai needs XAI_API_KEY")
        else:
            raise SystemExit(f"LLM_MODE must be deep or xai, not {self.mode!r}")
        self._check(self.base)
        log.info("LLM: mode=%s host=%s model=%s fallback_xai=%s fast=%s", self.mode, urlparse(self.base).hostname,
                 self.model, self.fallback_xai, self.fast_model or "off")

    @staticmethod
    def _check(base: str) -> None:
        host = (urlparse(base).hostname or "").lower()
        if host in forbidden_hosts():
            raise SystemExit(f"refusing LLM endpoint {host}: it is listed in SKWOOP_FORBIDDEN_LLM_HOSTS")

    async def stream_sentences(self, messages: list[dict[str, object]], fast: bool = False) -> AsyncIterator[str]:
        """Yield speakable sentences as the model streams them."""
        if fast and self.fast_model:
            said = False
            try:
                async for s in self._stream(XAI_BASE, env("XAI_API_KEY"), self.fast_model, messages):
                    said = True
                    yield s

                return
            except (TimeoutError, ClientError, RuntimeError) as e:
                if said:
                    raise

                log.warning("fast model %s failed (%s); using the normal route", self.fast_model, type(e).__name__)

        try:
            async for s in self._stream(self.base, self.key, self.model, messages):
                yield s
        except (TimeoutError, ClientError, RuntimeError) as e:
            if self.mode == "deep" and self.fallback_xai:
                log.warning("Deep voice line failed (%s); falling back to xAI direct", type(e).__name__)
                async for s in self._stream(XAI_BASE, env("XAI_API_KEY"),
                                            env("XAI_MODEL", "grok-4.20-0309-non-reasoning"), messages):
                    yield s
            else:
                raise

    async def _stream(self, base: str, key: str, model: str, messages: list[dict[str, object]]) -> AsyncIterator[str]:
        body = {"model": model, "messages": messages, "stream": True,
                "max_tokens": int(env("LLM_MAX_TOKENS", "300")), "temperature": 0.6}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        buf = ""
        async with self.http.post(f"{base}/chat/completions", json=body, headers=headers,
                                  timeout=ClientTimeout(total=60, sock_read=30)) as r:
            if r.status != 200:
                raise RuntimeError(f"LLM HTTP {r.status}")
            ctype = r.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype:  # server ignored stream=true
                data = await r.json()
                if not isinstance(data, dict):
                    raise RuntimeError(f"LLM HTTP {r.status}")
                content = data["choices"][0]["message"]["content"]
                buf = content if isinstance(content, str) else ""
            else:
                async for raw in r.content:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0].get("delta", {}).get("content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    buf += delta
                    while (cut := _sentence_end(buf)) is not None:
                        sentence, buf = buf[:cut].strip(), buf[cut:]
                        if sentence:
                            yield sentence
        for part in re.split(r"(?<=[.!?])\s+", buf.strip()):
            if part.strip():
                yield part.strip()
