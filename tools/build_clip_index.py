"""Bake a deterministic clips.idx for a Skwoop clip library.

Usage:
    python tools/build_clip_index.py ROOT [--out PATH] [--check]

ROOT is a library directory with manifest.json and clips/. The index is written
next to the manifest unless --out is set. --check exits 1 when the index is
missing or does not match the library. Wav files are not stored in this
repository; pass a library you have on disk.

The tables are the ones SkwoopLibrary.load fills (norm, template.format, NONVERBAL,
_put_whole). This module does not reimplement that. See bridge/docs/clips-idx.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import TypedDict

_MAGIC = b"SKIDX\x00\x00\x00"
_HEADER = 52
_KINDS = 13
_ENTRY = 16
_U32_MAX = 0xFFFFFFFF
_CHUNK = 65536


class _TemplateRow(TypedDict):
    id: str
    sentence: str
    moods: list[str]
    mode: str


class _B2Row(TypedDict):
    template: str
    slots: list[str]
    pattern: str


class BuildError(Exception):
    """The index could not be built or checked."""


class _Pool:
    """UTF-8 string pool. Identical bytes share one span. Offsets are 4-aligned."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._at: dict[bytes, tuple[int, int]] = {}

    def add(self, text: str) -> tuple[int, int]:
        raw = text.encode("utf-8")
        found = self._at.get(raw)
        if found is not None:
            return found

        off = len(self.buf)
        self.buf += raw
        self.buf += b"\x00" * ((-len(raw)) & 3)
        rec = (off, len(raw))
        self._at[raw] = rec
        return rec


def _repo_on_path() -> None:
    """Running `tools/build_clip_index.py` puts tools/ on sys.path[0]. The bridge package is its sibling."""
    bridge = str(Path(__file__).resolve().parent.parent / "bridge")
    if bridge not in sys.path:
        sys.path.insert(0, bridge)


def _sha256(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)

    return digest.digest()


def _ref(pool: _Pool, text: str) -> bytes:
    off, length = pool.add(text)
    return struct.pack("<II", off, length)


def _u32_pair(start: int, count: int) -> bytes:
    return struct.pack("<II", start, count)


def _nbytes(root: Path, rel: str) -> int:
    try:
        size = (root / rel).stat().st_size
    except OSError as exc:
        raise BuildError(f"{rel}: {exc.strerror or exc}") from exc

    if size > _U32_MAX:
        raise BuildError(f"{rel}: file longer than u32")

    return size


def _json(rows: list[_TemplateRow] | list[_B2Row]) -> bytes:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _assemble(digest: bytes, sections: list[tuple[int, int, bytes]]) -> bytes:
    if len(digest) != 32 or len(sections) != _KINDS:
        raise BuildError("internal: header fields")

    out = bytearray(_HEADER + _KINDS * _ENTRY)
    struct.pack_into("<8sII32sI", out, 0, _MAGIC, 1, 0, digest, _KINDS)
    offset = _HEADER + _KINDS * _ENTRY
    for index, (kind, record_size, body) in enumerate(sections):
        if offset % 4 != 0 or record_size <= 0 or len(body) % record_size != 0:
            raise BuildError(f"section {kind} is not 4-byte aligned")

        count = len(body) // record_size
        if offset > _U32_MAX or count > _U32_MAX:
            raise BuildError(f"section {kind} does not fit in u32")

        struct.pack_into("<IIII", out, _HEADER + index * _ENTRY, kind, offset, count, record_size)
        out += body
        offset += len(body)
        if index != _KINDS - 1:
            pad = (-offset) & 3
            if pad:
                out += b"\x00" * pad
                offset += pad

    if offset > _U32_MAX:
        raise BuildError("index does not fit in u32")

    return bytes(out)


def build_index(root: Path) -> bytes:
    """Bytes of clips.idx for root/manifest.json. Same tables as SkwoopLibrary.load."""
    _repo_on_path()
    from skwoop_lib import build_manifest_tables

    manifest = root / "manifest.json"
    try:
        digest = _sha256(manifest)
    except OSError as exc:
        raise BuildError(f"manifest unreadable: {exc.strerror or exc}") from exc

    try:
        tables = build_manifest_tables(root)
    except (OSError, ValueError) as exc:
        raise BuildError(f"manifest not loaded ({type(exc).__name__})") from exc

    parsed = tables.manifest

    pool = _Pool()
    whole = bytearray()
    pairs = bytearray()
    for key in sorted(tables.whole, key=_utf8):
        mood_ids = tables.whole[key]
        start = len(pairs) // 16
        for mood, cid in mood_ids:
            pairs += _ref(pool, mood) + _ref(pool, cid)

        whole += _ref(pool, key) + _u32_pair(start, len(mood_ids))

    kinds = bytearray()
    values = bytearray()
    for kind in sorted(tables.slot_values, key=_utf8):
        ents = tables.slot_values[kind]
        start = len(values) // 16
        for norm_value in sorted(ents, key=_utf8):
            values += _ref(pool, norm_value) + _ref(pool, ents[norm_value])

        kinds += _ref(pool, kind) + _u32_pair(start, len(ents))

    titles = bytearray()
    for key in sorted(tables.titles, key=_utf8):
        titles += _ref(pool, key) + _ref(pool, tables.titles[key])

    slots = bytearray()
    for key in sorted(parsed.slots, key=_utf8):
        slots += _ref(pool, key) + _ref(pool, parsed.slots[key])

    carriers = bytearray()
    for key in sorted(parsed.carriers, key=_utf8):
        carriers += _ref(pool, key) + _ref(pool, parsed.carriers[key])

    lines = bytearray()
    items = bytearray()
    for key in sorted(parsed.lines, key=_utf8):
        group = parsed.lines[key]
        start = len(items) // 16
        for line in group:
            items += _ref(pool, line.text) + _ref(pool, line.id)

        lines += _ref(pool, key) + _u32_pair(start, len(group))

    clips = bytearray()
    for cid in sorted(parsed.clips, key=_utf8):
        clip = parsed.clips[cid]
        clips += _ref(pool, cid) + _ref(pool, clip.file) + struct.pack("<I", _nbytes(root, clip.file))

    template_rows: list[_TemplateRow] = [
        {"id": tpl.id, "sentence": tpl.sentence, "moods": list(tpl.moods), "mode": tpl.mode}
        for tpl in parsed.templates.values()
    ]
    b2_rows: list[_B2Row] = [
        {"template": pat.template, "slots": list(pat.slots), "pattern": pat.rx.pattern} for pat in tables.b2
    ]
    if len(pool.buf) % 4 != 0:
        raise BuildError("string pool is not 4-byte aligned")

    return _assemble(digest, [
        (1, 1, bytes(pool.buf)),
        (2, 16, bytes(whole)),
        (3, 16, bytes(pairs)),
        (4, 16, bytes(kinds)),
        (5, 16, bytes(values)),
        (6, 16, bytes(titles)),
        (7, 16, bytes(slots)),
        (8, 16, bytes(carriers)),
        (9, 16, bytes(lines)),
        (10, 16, bytes(items)),
        (11, 20, bytes(clips)),
        (12, 1, _json(template_rows)),
        (13, 1, _json(b2_rows)),
    ])


def _utf8(text: str) -> bytes:
    return text.encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def check_index(root: Path, index: Path) -> str | None:
    """None when the index exists and its manifest sha256 matches. Else the reason."""
    if not index.is_file():
        return f"index missing: {index}"

    try:
        with index.open("rb") as fh:
            header = fh.read(48)
    except OSError as exc:
        return f"index unreadable: {exc.strerror or exc}"

    if len(header) < 48:
        return f"index too short: {index}"

    manifest = root / "manifest.json"
    try:
        digest = _sha256(manifest)
    except OSError as exc:
        return f"manifest unreadable: {exc.strerror or exc}"

    if header[16:48] != digest:
        return "manifest sha256 mismatch"

    return None


def _parse(argv: list[str]) -> tuple[Path, Path | None, bool]:
    root: Path | None = None
    out: Path | None = None
    check = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--check":
            check = True
        elif arg == "--out":
            index += 1
            if index >= len(argv):
                raise BuildError("usage: build_clip_index.py ROOT [--out PATH] [--check]")

            out = Path(argv[index])
        elif arg.startswith("-"):
            raise BuildError(f"unknown option: {arg}")
        elif root is None:
            root = Path(arg)
        else:
            raise BuildError(f"unexpected argument: {arg}")

        index += 1

    if root is None:
        raise BuildError("usage: build_clip_index.py ROOT [--out PATH] [--check]")

    return root, out, check


def write_clip_texts(root: Path) -> Path:
    """Slim id->text map for b2 stitch without loading the whole manifest."""
    import json
    from skwoop_lib import load_manifest
    texts = {cid: clip.text for cid, clip in load_manifest(root).clips.items() if clip.text}
    path = root / "clip-texts.json"
    write_atomic(path, json.dumps(texts, separators=(",", ":"), ensure_ascii=False).encode())
    return path


def main(argv: list[str] | None = None) -> int:
    try:
        root_arg, out_arg, check = _parse(sys.argv[1:] if argv is None else argv)
    except BuildError as exc:
        print(exc, file=sys.stderr)
        return 1

    root = root_arg.expanduser()
    out = (out_arg if out_arg is not None else root / "clips.idx").expanduser()
    if check:
        reason = check_index(root, out)
        if reason is not None:
            print(reason, file=sys.stderr)
            return 1

        return 0

    started = time.perf_counter()
    try:
        data = build_index(root)
        write_atomic(out, data)
        texts = write_clip_texts(root)
    except BuildError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"{out}: {exc.strerror or exc}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - started
    print(f"{out} {len(data)} bytes + {texts.name} {elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
