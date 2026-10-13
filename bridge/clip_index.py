"""Read-only mmap of a clips.idx file. Lookups binary-search the section table.

Open keeps the mapping, thirteen section descriptors, and the small template and b2
lists. It does not build a clip object per row. See docs/clips-idx.md.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import struct
from dataclasses import dataclass
from typing import Literal, NamedTuple, cast

_MAGIC = b"SKIDX\x00\x00\x00"
_HEADER = 52
_KINDS = 13
_ENTRY = 16
_CHUNK = 65536
_SIZES = (0, 1, 16, 16, 16, 16, 16, 16, 16, 16, 16, 20, 1, 1)
# kind, number of leading string refs, value-range target kind (0 = none)
_CHECKED = (
    (2, 1, 3),
    (3, 2, 0),
    (4, 1, 5),
    (5, 2, 0),
    (6, 2, 0),
    (7, 2, 0),
    (8, 2, 0),
    (9, 1, 10),
    (10, 2, 0),
    (11, 2, 0),
)


class IndexMissing(Exception):
    """The index path does not exist."""


class IndexStale(Exception):
    """The stored manifest sha256 does not match manifest.json."""


class IndexCorrupt(Exception):
    """The index bytes are not a version-1 clips.idx."""


@dataclass(frozen=True, slots=True)
class _Section:
    offset: int
    count: int
    record_size: int


class Template(NamedTuple):
    id: str
    sentence: str
    moods: tuple[str, ...]
    mode: Literal["b2", "whole"]


class B2Pattern(NamedTuple):
    template: str
    slots: tuple[str, ...]
    pattern: str


def _lt(left: memoryview, right: memoryview) -> bool:
    """Bytewise order. memoryview has no `<` (3.14); this reads the mapped bytes."""
    n = min(len(left), len(right))
    for i in range(n):
        a = left[i]
        b = right[i]
        if a != b:
            return a < b

    return len(left) < len(right)


def _sha256(path: str) -> bytes | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return None

    return digest.digest()


def _json_value(data: bytes) -> object:
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndexCorrupt("json") from exc

    # SAFETY: json.loads is typed as Any; callers narrow with isinstance.
    return cast(object, loaded)


def _obj(raw: object, what: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise IndexCorrupt(what)

    out: dict[str, object] = {}
    for key, val in raw.items():
        if not isinstance(key, str):
            raise IndexCorrupt(what)

        # SAFETY: JSON object values are untyped until the field readers narrow them.
        out[key] = cast(object, val)

    return out


def _as_str(raw: object, what: str) -> str:
    if not isinstance(raw, str):
        raise IndexCorrupt(what)

    return raw


def _str_tuple(raw: object, what: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise IndexCorrupt(what)

    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise IndexCorrupt(what)

        out.append(item)

    return tuple(out)


def _mode(raw: object) -> Literal["b2", "whole"]:
    if raw == "b2":
        return "b2"

    if raw == "whole":
        return "whole"

    raise IndexCorrupt("mode")


def _array(data: bytes, what: str) -> tuple[dict[str, object], ...]:
    raw = _json_value(data)
    if not isinstance(raw, list):
        raise IndexCorrupt(what)

    return tuple(_obj(item, what) for item in raw)


def _template(row: dict[str, object]) -> Template:
    return Template(
        _as_str(row.get("id"), "template"),
        _as_str(row.get("sentence"), "template"),
        _str_tuple(row.get("moods"), "moods"),
        _mode(row.get("mode")),
    )


def _b2(row: dict[str, object]) -> B2Pattern:
    return B2Pattern(
        _as_str(row.get("template"), "b2"),
        _str_tuple(row.get("slots"), "slots"),
        _as_str(row.get("pattern"), "b2"),
    )


class ClipIndex:
    """Read-only view of one clips.idx mapping."""

    templates: tuple[Template, ...]
    b2: tuple[B2Pattern, ...]

    def __init__(self, mm: mmap.mmap) -> None:
        self._closed = False
        self._mm = mm
        self._view: memoryview | None = memoryview(mm)
        self._sec: dict[int, _Section] = {}
        self.templates = ()
        self.b2 = ()

    @classmethod
    def open(cls, idx_path: str | os.PathLike[str], manifest_path: str | os.PathLike[str]) -> ClipIndex:
        """Map idx_path and check it against manifest_path. Raises IndexMissing, IndexCorrupt, or IndexStale."""
        path = os.fspath(idx_path)
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError as exc:
            raise IndexMissing(path) from exc
        except OSError as exc:
            raise IndexCorrupt(path) from exc

        try:
            if os.fstat(fd).st_size < _HEADER:
                raise IndexCorrupt("short")

            try:
                mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError, BufferError) as exc:
                raise IndexCorrupt(path) from exc
        finally:
            os.close(fd)

        idx = cls(mm)
        try:
            idx._validate()
            idx._check_manifest(os.fspath(manifest_path))
            idx._load_small()
        except Exception:
            idx.close()
            raise

        return idx

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        view = self._view
        self._view = None
        if view is not None:
            view.release()

        self._release_mmap()

    def _release_mmap(self) -> None:
        """Drop the mapping. A lookup slice may still export it while an exception unwinds."""
        try:
            self._mm.close()
        except (BufferError, ValueError):
            return

    def __del__(self) -> None:
        try:
            self.close()
            self._release_mmap()
        except Exception:
            return

    def __enter__(self) -> ClipIndex:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def whole(self, key: str) -> tuple[tuple[str, str], ...] | None:
        """(mood, clip id) pairs in _put_whole order, or None."""
        mid = self._find(2, key.encode(), 0, self._sec[2].count)
        if mid is None:
            return None

        start, count = self._range(self._at(2, mid) + 8)
        base = self._sec[3].offset
        return tuple((self._text(base + i * 16), self._text(base + i * 16 + 8)) for i in range(start, start + count))

    def slot_value(self, kind: str, norm_value: str) -> str | None:
        """Library casing for one slot kind and norm(value), or None."""
        mid = self._find(4, kind.encode(), 0, self._sec[4].count)
        if mid is None:
            return None

        start, count = self._range(self._at(4, mid) + 8)
        ent = self._find(5, norm_value.encode(), start, count)
        if ent is None:
            return None

        return self._text(self._at(5, ent) + 8)

    def title(self, key: str) -> str | None:
        """Library casing for norm(title), or None."""
        return self._paired(6, key.encode())

    def slot(self, key: str) -> str | None:
        """Clip id for a manifest slot key, or None."""
        return self._paired(7, key.encode())

    def carrier(self, key: str) -> str | None:
        """Clip id for a tid|part|mood carrier key, or None."""
        return self._paired(8, key.encode())

    def lines(self, key: str) -> tuple[tuple[str, str], ...] | None:
        """(text, id) items in manifest order for a kind|mood key, or None. Includes non-verbal lines."""
        mid = self._find(9, key.encode(), 0, self._sec[9].count)
        if mid is None:
            return None

        start, count = self._range(self._at(9, mid) + 8)
        base = self._sec[10].offset
        return tuple((self._text(base + i * 16), self._text(base + i * 16 + 8)) for i in range(start, start + count))

    def has_lines(self, kind: str) -> bool:
        """True when any kind|mood key has this kind. '|' is 0x7C, so ack2 is not ack."""
        prefix = (kind + "|").encode()
        sec = self._sec[9]
        lo = 0
        hi = sec.count
        needle = memoryview(prefix)
        while lo < hi:
            mid = (lo + hi) // 2
            if _lt(self._key(9, mid), needle):
                lo = mid + 1
            else:
                hi = mid

        if lo >= sec.count:
            return False

        key = self._key(9, lo)
        n = len(prefix)

        return len(key) >= n and key[:n] == prefix

    def clip(self, clip_id: str) -> tuple[str, int] | None:
        """(relative path, byte length) for a clip id, or None."""
        mid = self._find(11, clip_id.encode(), 0, self._sec[11].count)
        if mid is None:
            return None

        rec = self._at(11, mid)
        view = self._buf()
        nbytes = struct.unpack_from("<I", view, rec + 16)[0]

        return self._text(rec + 8), nbytes

    def template(self, template_id: str) -> Template | None:
        for row in self.templates:
            if row.id == template_id:
                return row

        return None

    def b2_pattern(self, template_id: str) -> B2Pattern | None:
        for row in self.b2:
            if row.template == template_id:
                return row

        return None

    @property
    def n_clips(self) -> int:
        return self._sec[11].count

    @property
    def n_sentences(self) -> int:
        return self._sec[2].count

    def _buf(self) -> memoryview:
        view = self._view
        if view is None:
            raise IndexCorrupt("closed")

        return view

    def _validate(self) -> None:
        view = self._buf()
        size = len(view)
        if size < _HEADER:
            raise IndexCorrupt("short")

        magic, version, flags, _digest, section_count = struct.unpack_from("<8sII32sI", view, 0)
        if size < _HEADER + section_count * _ENTRY:
            raise IndexCorrupt("short")

        if magic != _MAGIC:
            raise IndexCorrupt("magic")

        if version != 1:
            raise IndexCorrupt("version")

        if flags != 0:
            raise IndexCorrupt("flags")

        if section_count != _KINDS:
            raise IndexCorrupt("section_count")

        found: dict[int, _Section] = {}
        for i in range(_KINDS):
            kind, offset, count, record_size = struct.unpack_from("<IIII", view, _HEADER + i * _ENTRY)
            if not (1 <= kind <= _KINDS) or kind in found or record_size != _SIZES[kind]:
                raise IndexCorrupt("section")

            if offset & 3 or offset > size:
                raise IndexCorrupt("offset")

            nbytes = count * record_size
            end = offset + nbytes
            if end < offset or end > size:
                raise IndexCorrupt("span")

            found[kind] = _Section(offset, count, record_size)

        if set(found) != set(range(1, _KINDS + 1)):
            raise IndexCorrupt("kinds")

        spans = sorted((sec.offset, sec.offset + sec.count * sec.record_size) for sec in found.values() if sec.count)
        prev = 0
        for start, end in spans:
            if start < prev:
                raise IndexCorrupt("overlap")

            prev = end

        self._sec = found
        pool_len = found[1].count
        for kind, nrefs, target in _CHECKED:
            sec = found[kind]
            target_count = found[target].count if target else 0
            for rec_i in range(sec.count):
                rec = sec.offset + rec_i * sec.record_size
                for n in range(nrefs):
                    self._check_ref(view, rec + n * 8, pool_len)

                if target:
                    start, count = struct.unpack_from("<II", view, rec + nrefs * 8)
                    if start + count > target_count:
                        raise IndexCorrupt("range")

    def _check_ref(self, view: memoryview, at: int, pool_len: int) -> None:
        off, length = struct.unpack_from("<II", view, at)
        end = off + length
        if off & 3 or end < off or end > pool_len:
            raise IndexCorrupt("ref")

    def _check_manifest(self, path: str) -> None:
        digest = _sha256(path)
        if digest is None or digest != self._buf()[16:48]:
            raise IndexStale(path)

    def _load_small(self) -> None:
        self.templates = tuple(_template(row) for row in _array(self._blob(12), "templates"))
        self.b2 = tuple(_b2(row) for row in _array(self._blob(13), "b2"))

    def _blob(self, kind: int) -> bytes:
        sec = self._sec[kind]

        return self._buf()[sec.offset : sec.offset + sec.count].tobytes()

    def _at(self, kind: int, index: int) -> int:
        sec = self._sec[kind]

        return sec.offset + index * sec.record_size

    def _key(self, kind: int, index: int) -> memoryview:
        view = self._buf()
        koff, klen = struct.unpack_from("<II", view, self._at(kind, index))
        pool = self._sec[1].offset

        return view[pool + koff : pool + koff + klen]

    def _text(self, at: int) -> str:
        return bytes(self._key_at(at)).decode("utf-8")

    def _key_at(self, at: int) -> memoryview:
        view = self._buf()
        off, length = struct.unpack_from("<II", view, at)
        pool = self._sec[1].offset

        return view[pool + off : pool + off + length]

    def _range(self, at: int) -> tuple[int, int]:
        start, count = struct.unpack_from("<II", self._buf(), at)

        return start, count

    def _find(self, kind: int, probe: bytes, start: int, count: int) -> int | None:
        lo = start
        hi = start + count
        needle = memoryview(probe)
        while lo < hi:
            mid = (lo + hi) // 2
            key = self._key(kind, mid)
            if key == needle:
                return mid

            if _lt(key, needle):
                lo = mid + 1
            else:
                hi = mid

        return None

    def _paired(self, kind: int, probe: bytes) -> str | None:
        mid = self._find(kind, probe, 0, self._sec[kind].count)
        if mid is None:
            return None

        return self._text(self._at(kind, mid) + 8)
