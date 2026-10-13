"""Read the .npz weight files without numpy: a zip of .npy arrays (format 1.0/2.0, C order, little endian).

Only the dtypes the weight files use: <f4, <f8, <i8, |b1 and <U (fixed-width UTF-32). Anything else raises
ValueError. Format reference: numpy.lib.format (NEP 1).
"""

from __future__ import annotations

import ast
import math
import struct
import sys
import zipfile
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

_MAGIC = b"\x93NUMPY"
_ARRAY_CODES = {"<f4": "f", "<f8": "d", "<i8": "q"}

# Flat C-order payload: float32/float64, int64, bools, or fixed-width unicode.
ArrayData = array[float] | array[int] | tuple[bool, ...] | tuple[str, ...]


def is_f32(data: ArrayData) -> TypeGuard[array[float]]:
    """True when data is the float32 array('f') the weight files store."""
    return isinstance(data, array) and data.typecode == "f"


@dataclass(frozen=True)
class Npy:
    shape: tuple[int, ...]
    data: ArrayData  # flat, C order

    def scalar(self) -> float | int | bool | str:
        if self.shape != ():
            raise ValueError(f"expected a scalar, got shape {self.shape}")
        return self.data[0]


def _parse(raw: bytes, name: str) -> Npy:
    if raw[:6] != _MAGIC:
        raise ValueError(f"{name}: not a .npy array")
    major = raw[6]
    hlen_size = 2 if major == 1 else 4
    (hlen,) = struct.unpack("<H" if hlen_size == 2 else "<I", raw[8:8 + hlen_size])
    start = 8 + hlen_size + hlen
    header = ast.literal_eval(raw[8 + hlen_size:start].decode("latin1"))
    descr, fortran, shape = header["descr"], header["fortran_order"], tuple(header["shape"])
    if fortran:
        raise ValueError(f"{name}: Fortran order is not supported")
    n = math.prod(shape)
    body = raw[start:]
    if descr in _ARRAY_CODES:
        a = array(_ARRAY_CODES[descr])
        a.frombytes(body[:n * a.itemsize])
        if sys.byteorder != "little":
            a.byteswap()
        if len(a) != n:
            raise ValueError(f"{name}: {len(a)} values, expected {n}")
        return Npy(shape, a)
    if descr == "|b1":
        return Npy(shape, tuple(b != 0 for b in body[:n]))
    if descr.startswith("<U"):
        width = int(descr[2:]) * 4
        return Npy(shape, tuple(body[i * width:(i + 1) * width].decode("utf-32-le").rstrip("\x00") for i in range(n)))
    raise ValueError(f"{name}: dtype {descr} is not supported")


def load(path: Path) -> dict[str, Npy]:
    """{array name: Npy} for every array in the .npz (no pickles: object arrays are rejected)."""
    with zipfile.ZipFile(path) as z:
        return {info.filename.removesuffix(".npy"): _parse(z.read(info), info.filename) for info in z.infolist()}
