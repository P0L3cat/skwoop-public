"""Test signals without numpy: float lists at TTS_RATE, WAV files, levels."""
import math
import random
import wave
from array import array

from providers import TTS_RATE


def tone(f=440.0, amp=0.2, s=0.5, rate=TTS_RATE):
    return [amp * math.sin(2 * math.pi * f * i / rate) for i in range(int(s * rate))]


def noise(n, sd, seed):
    r = random.Random(seed)
    return [r.gauss(0.0, sd) for _ in range(n)]


def pcm16(x, gain=32767.0):
    """Floats -> 16-bit PCM bytes: clipped to [-1, 1], scaled, truncated toward zero."""
    return array("h", (int(max(-1.0, min(1.0, v)) * gain) for v in x)).tobytes()


def floats(pcm):
    return [v / 32768 for v in array("h", pcm)]


def write_wav(path, x, rate=TTS_RATE):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(pcm16(x))


def rms(x):
    return math.sqrt(sum(v * v for v in x) / len(x))


def frame_db(x, n):
    """Mean power of each whole n-sample frame, in dB."""
    return [10 * math.log10(sum(v * v for v in x[i:i + n]) / n + 1e-12) for i in range(0, len(x) // n * n, n)]
