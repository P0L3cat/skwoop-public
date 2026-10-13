"""The slice of libopus the bridge uses (16-bit encode and decode), through ctypes. Replaces the opuslib package
(BSD-3, 1.2k lines) with the four calls it wrapped; output is byte-identical because the codec is the same libopus.
libopus comes from the OS package manager."""
from __future__ import annotations

import ctypes
import ctypes.util
from pathlib import Path
from typing import Final

APPLICATIONS: Final = {"voip": 2048, "audio": 2049, "restricted_lowdelay": 2051}
MAX_PACKET: Final = 4000   # bytes: libopus's recommended ceiling for one packet


class OpusError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(_LIB.opus_strerror(code).decode())
        self.code = code


def _load() -> ctypes.CDLL:
    found = ctypes.util.find_library("opus")
    for path in (found, "/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"):
        if path and (path == found or Path(path).is_file()):
            lib = ctypes.CDLL(path)
            break
    else:
        raise ImportError("libopus not found (Mac: brew install opus; Debian: apt install libopus0)")
    p, i, ip = ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
    for name, args, res in (
        ("opus_encoder_create", [ctypes.c_int32, i, i, ip], p), ("opus_encoder_destroy", [p], None),
        ("opus_encode", [p, ctypes.c_char_p, i, ctypes.c_char_p, ctypes.c_int32], ctypes.c_int32),
        ("opus_decoder_create", [ctypes.c_int32, i, ip], p), ("opus_decoder_destroy", [p], None),
        ("opus_decode", [p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_char_p, i, i], i),
        ("opus_strerror", [i], ctypes.c_char_p),
    ):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


_LIB: Final = _load()


class Encoder:
    def __init__(self, rate: int, channels: int, application: str) -> None:
        err = ctypes.c_int(0)
        self.channels = channels
        self._st = _LIB.opus_encoder_create(rate, channels, APPLICATIONS[application], ctypes.byref(err))
        if err.value < 0 or not self._st:
            raise OpusError(err.value)

    def encode(self, pcm: bytes, frame_size: int) -> bytes:
        """One frame of interleaved 16-bit PCM (frame_size samples per channel) -> one Opus packet."""
        # SAFETY: libopus reads frame_size * channels samples from pcm with no length of its own
        if len(pcm) < frame_size * self.channels * 2:
            raise ValueError(f"encode: {len(pcm)} bytes < one {frame_size}-sample frame")
        out = ctypes.create_string_buffer(MAX_PACKET)
        n = _LIB.opus_encode(self._st, pcm, frame_size, out, MAX_PACKET)
        if n < 0:
            raise OpusError(n)
        return out.raw[:n]

    def __del__(self) -> None:
        if getattr(self, "_st", None):
            _LIB.opus_encoder_destroy(self._st)


class Decoder:
    def __init__(self, rate: int, channels: int) -> None:
        err = ctypes.c_int(0)
        self.channels = channels
        self._st = _LIB.opus_decoder_create(rate, channels, ctypes.byref(err))
        if err.value < 0 or not self._st:
            raise OpusError(err.value)

    def decode(self, packet: bytes, frame_size: int) -> bytes:
        """One Opus packet -> interleaved 16-bit PCM, at most frame_size samples per channel."""
        # SAFETY: libopus writes at most frame_size * channels samples into out
        out = ctypes.create_string_buffer(frame_size * self.channels * 2)
        n = _LIB.opus_decode(self._st, packet, len(packet), out, frame_size, 0)
        if n < 0:
            raise OpusError(n)
        return out.raw[:n * self.channels * 2]

    def __del__(self) -> None:
        if getattr(self, "_st", None):
            _LIB.opus_decoder_destroy(self._st)
