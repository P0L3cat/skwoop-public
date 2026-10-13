"""Thin ctypes binding for dsp.c: the numeric kernels the bridge once used numpy for (audio conversion, resampling,
correlation, DTW, sparse linear layers). Build once per checkout, before the bridge or the tests start:

    .venv/bin/python dsp.py build       # clang -arch arm64 -O2 ... on macOS, cc -O2 ... on Linux

Arrays cross into C by address (array.buffer_info); every length is checked here before the call.
"""
from __future__ import annotations

import ctypes
import math
import subprocess
import sys
from array import array
from collections.abc import Iterable, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "dsp.c"
LIB = HERE / "_dsp.so"
CFLAGS = ["-O2", "-std=c11", "-shared", "-fPIC", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror"]


def build() -> None:
    # macOS ships clang as cc, but the harness wants the arch explicit. Linux tests keep plain cc.
    compiler = ["clang", "-arch", "arm64"] if sys.platform == "darwin" else ["cc"]
    subprocess.run([*compiler, *CFLAGS, "-o", str(LIB), str(SRC), "-lm"], check=True)


def _load() -> ctypes.CDLL:
    # Tests and a fresh checkout have no prebuilt library. Build it beside the source.
    if not LIB.is_file() or LIB.stat().st_mtime < SRC.stat().st_mtime:
        build()
    lib = ctypes.CDLL(str(LIB))
    p, n, d = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_double
    sigs = {
        "dsp_i16_to_f64": ([p, n, p], None), "dsp_f64_to_i16_round": ([p, n, d, p], None),
        "dsp_f64_to_i16_trunc": ([p, n, d, p], None), "dsp_scale": ([p, n, d], None), "dsp_mul": ([p, p, n], None),
        "dsp_dot": ([p, p, n], d), "dsp_frame_peaks": ([p, n, n, p], None), "dsp_sumsq_i16": ([p, n], d),
        "dsp_resample_i16": ([p, n, p, n], None), "dsp_mono_i16": ([p, n, n, p], None),
        "dsp_fade_out_i16": ([p, n, p], None), "dsp_correlate": ([p, n, p, n, p], None),
        "dsp_xcorr_norm": ([p, n, p, n, p], None), "dsp_dtw": ([p, n, p, n, n, p, p], n),
        "dsp_sparse_dot": ([p, n, n, n, p, p, n], d),
    }
    for name, (args, res) in sigs.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


if __name__ == "__main__":  # before loading, so a missing or stale library can be built
    if sys.argv[1:] != ["build"]:
        sys.exit("usage: python dsp.py build")
    build()
    sys.exit()

_C = _load()


def _a(x: array[int] | array[float]) -> int:
    # SAFETY: the address stays valid for the call: x is referenced by the caller and never resized meanwhile.
    return x.buffer_info()[0]


def samples(pcm: bytes) -> array[int]:
    """Little-endian 16-bit PCM -> array('h'). A trailing odd byte is dropped."""
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        a.byteswap()
    return a


def to_pcm(a: array[int]) -> bytes:
    if sys.byteorder != "little":
        a = array("h", a)
        a.byteswap()
    return a.tobytes()


def floats(x: Iterable[float]) -> array[float]:
    """Floats as array('d') (a copy)."""
    return array("d", x)


def to_floats(pcm: bytes) -> array[float]:
    """int16 PCM -> array('d') in [-1, 1): s / 32768."""
    s = samples(pcm)
    out = array("d", bytes(8 * len(s)))
    if s:
        _C.dsp_i16_to_f64(_a(s), len(s), _a(out))
    return out


def from_floats(x: array[float], gain: float = 32768.0) -> bytes:
    """array('d') * gain -> int16 PCM, rounded half to even and clipped (numpy.round, numpy.clip)."""
    out = array("h", bytes(2 * len(x)))
    if x:
        _C.dsp_f64_to_i16_round(_a(x), len(x), gain, _a(out))
    return to_pcm(out)


def from_floats_trunc(x: array[float], gain: float) -> bytes:
    """array('d') * gain -> int16 PCM, truncated toward zero and clipped (numpy astype)."""
    out = array("h", bytes(2 * len(x)))
    if x:
        _C.dsp_f64_to_i16_trunc(_a(x), len(x), gain, _a(out))
    return to_pcm(out)


def scale(x: array[float], g: float) -> array[float]:
    """A copy of array('d') x times g."""
    y = array("d", x)
    if y:
        _C.dsp_scale(_a(y), len(y), g)
    return y


def mul(x: array[float], g: array[float]) -> array[float]:
    """A copy of x times g, element by element (same length)."""
    if len(x) != len(g):
        raise ValueError(f"mul: {len(x)} != {len(g)}")
    y = array("d", x)
    if y:
        _C.dsp_mul(_a(y), _a(g), len(y))
    return y


def dot(a: array[float], b: array[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"dot: {len(a)} != {len(b)}")
    return float(_C.dsp_dot(_a(a), _a(b), len(a))) if a else 0.0


def frame_peaks(x: array[float], win: int) -> array[float]:
    """Peak |x| of each whole win-sample frame."""
    k = len(x) // win if win > 0 else 0
    out = array("d", bytes(8 * k))
    if k:
        _C.dsp_frame_peaks(_a(x), len(x), win, _a(out))
    return out


def rms16(a: array[int]) -> float:
    """RMS of int16 samples (array('h')), in sample units."""
    return (float(_C.dsp_sumsq_i16(_a(a), len(a))) / len(a)) ** 0.5 if a else 0.0


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    """Linear interpolation from src to dst Hz (output length len * dst / src), as numpy.interp did."""
    if src == dst or not pcm:
        return pcm
    x = samples(pcm)
    n = int(len(x) * dst / src)
    if n <= 0 or not x:
        return b""
    out = array("h", bytes(2 * n))
    _C.dsp_resample_i16(_a(x), len(x), _a(out), n)
    return to_pcm(out)


def mono(frames: bytes, channels: int) -> bytes:
    """Interleaved 16-bit frames -> mono: the channel mean, truncated toward zero."""
    s = samples(frames)
    n = len(s) // channels
    out = array("h", bytes(2 * n))
    if n:
        _C.dsp_mono_i16(_a(s), n, channels, _a(out))
    return to_pcm(out)


def fade_out(pcm: bytes) -> bytes:
    """16-bit PCM ramped linearly down to silence (float32 and truncated, as numpy did)."""
    s = samples(pcm)
    out = array("h", bytes(2 * len(s)))
    if s:
        _C.dsp_fade_out_i16(_a(s), len(s), _a(out))
    return to_pcm(out)


def correlate(seg: array[float], tmpl: array[float]) -> array[float]:
    """numpy.correlate(seg, tmpl, "valid")."""
    k = len(seg) - len(tmpl) + 1
    out = array("d", bytes(8 * max(0, k)))
    if k > 0 and tmpl:
        _C.dsp_correlate(_a(seg), len(seg), _a(tmpl), len(tmpl), _a(out))
    return out


def xcorr_norm(seg: array[float], ref: array[float]) -> array[float]:
    """Normalized cross-correlation of ref against every window of seg."""
    k = len(seg) - len(ref) + 1
    out = array("d", bytes(8 * max(0, k)))
    if k > 0 and ref:
        _C.dsp_xcorr_norm(_a(seg), len(seg), _a(ref), len(ref), _a(out))
    return out


def dtw_path(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[tuple[int, int]]:
    """DTW (Euclidean, steps (1,0) (0,1) (1,1)) between two frame sequences -> [(i, j)] from (0, 0)."""
    if not a or not b:
        return []
    dim = len(a[0])
    if any(len(r) != dim for r in (*a, *b)):
        raise ValueError("dtw: frames of different sizes")
    fa, fb = array("d", [v for r in a for v in r]), array("d", [v for r in b for v in r])
    acc = array("d", bytes(8 * (len(a) + 1) * (len(b) + 1)))
    path = array("i", bytes(4 * 2 * (len(a) + len(b))))
    n = _C.dsp_dtw(_a(fa), len(a), _a(fb), len(b), dim, _a(acc), _a(path))
    return [(path[2 * k], path[2 * k + 1]) for k in range(n)]


def sparse_dot(w: array[float], stride: int, col: int, idx: array[int], vals: array[float]) -> float:
    """sum(vals[k] * w[idx[k] * stride + col]): one output of a hashed sparse linear layer (w is array('f'),
    idx array('q'), vals array('d')). ValueError if an index is outside w."""
    if w.typecode != "f" or idx.typecode != "q" or vals.typecode != "d" or len(idx) != len(vals):
        raise ValueError("sparse_dot: want w 'f', idx 'q', vals 'd' of one length")
    s = float(_C.dsp_sparse_dot(_a(w), len(w), stride, col, _a(idx), _a(vals), len(idx))) if idx else 0.0
    if math.isnan(s):
        raise ValueError("sparse_dot: index outside the weights")
    return s
