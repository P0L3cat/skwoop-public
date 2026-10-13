"""ClipIndex lookups match SkwoopLibrary.load on the sample fixture, and open fails closed."""

import hashlib
import subprocess
import sys
from pathlib import Path

from synth_audio import library_root

FIX = library_root()
BUILD = Path(__file__).resolve().parents[2] / "tools" / "build_clip_index.py"

import pytest  # noqa: E402

from clip_index import ClipIndex, IndexCorrupt, IndexMissing, IndexStale  # noqa: E402
from skwoop_lib import build_manifest_tables  # noqa: E402


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(BUILD), *args], capture_output=True, text=True)


def _build(out: Path) -> None:
    proc = _run([str(FIX), "--out", str(out)])
    assert proc.returncode == 0, proc.stderr


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("clip-index") / "clips.idx"
    _build(out)

    return out


def test_lookups_match_library(built: Path) -> None:
    tables = build_manifest_tables(FIX)
    manifest = tables.manifest
    assert tables.whole and tables.slot_values and tables.titles and tables.b2
    assert manifest.slots and manifest.carriers and manifest.lines and manifest.clips and manifest.templates
    with ClipIndex.open(built, FIX / "manifest.json") as idx:
        for key, pairs in tables.whole.items():
            assert idx.whole(key) == pairs

        assert idx.whole("not a sentence in the library") is None
        for kind, ents in tables.slot_values.items():
            assert ents
            for norm_value, value in ents.items():
                assert idx.slot_value(kind, norm_value) == value

            assert idx.slot_value(kind, "no such slot value") is None

        assert idx.slot_value("no-such-kind", "x") is None
        for key, value in tables.titles.items():
            assert idx.title(key) == value

        assert idx.title("no such title") is None
        for key, cid in manifest.slots.items():
            assert idx.slot(key) == cid

        assert idx.slot("no|such|slot|key") is None
        for key, cid in manifest.carriers.items():
            assert idx.carrier(key) == cid

        assert idx.carrier("no|0|idle") is None
        for key, items in manifest.lines.items():
            assert idx.lines(key) == tuple((it.text, it.id) for it in items)

        assert idx.lines("nope|idle") is None
        kinds = {key.split("|", 1)[0] for key in manifest.lines}
        for kind in kinds:
            assert idx.has_lines(kind) is True

        assert idx.has_lines("not_a_kind") is False
        for cid, clip in manifest.clips.items():
            assert idx.clip(cid) == (clip.file, (FIX / clip.file).stat().st_size)

        assert idx.clip("deadbeefdead") is None
        got_templates = [(row.id, row.sentence, row.moods, row.mode) for row in idx.templates]
        exp_templates = [(row.id, row.sentence, row.moods, row.mode) for row in manifest.templates.values()]
        assert got_templates == exp_templates
        assert idx.template("no-such-template") is None
        got_b2 = [(row.template, row.slots, row.pattern) for row in idx.b2]
        exp_b2 = [(row.template, row.slots, row.rx.pattern) for row in tables.b2]
        assert got_b2 == exp_b2
        assert idx.b2_pattern("no-such-template") is None


def test_stale_manifest(tmp_path: Path, built: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes((FIX / "manifest.json").read_bytes() + b"\n")
    with pytest.raises(IndexStale):
        ClipIndex.open(built, manifest)

    with pytest.raises(IndexStale):
        ClipIndex.open(built, tmp_path / "absent.json")


def test_truncated_is_corrupt(tmp_path: Path, built: Path) -> None:
    raw = built.read_bytes()
    assert len(raw) > 400
    for n in (10, len(raw) // 2):
        dest = tmp_path / f"trunc-{n}.idx"
        dest.write_bytes(raw[:n])
        with pytest.raises(IndexCorrupt):
            ClipIndex.open(dest, tmp_path / "no-manifest.json")


def test_missing(tmp_path: Path) -> None:
    with pytest.raises(IndexMissing):
        ClipIndex.open(tmp_path / "missing.idx", FIX / "manifest.json")


def test_build_twice_same_sha256(tmp_path: Path) -> None:
    first = tmp_path / "a.idx"
    second = tmp_path / "b.idx"
    _build(first)
    _build(second)
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()


def test_check_codes(tmp_path: Path) -> None:
    out = tmp_path / "clips.idx"
    missing = _run([str(FIX), "--out", str(out), "--check"])
    assert missing.returncode == 1
    _build(out)
    ok = _run([str(FIX), "--out", str(out), "--check"])
    assert ok.returncode == 0
    stale = tmp_path / "stale-root"
    stale.mkdir()
    (stale / "manifest.json").write_text("{}\n")
    bad = _run([str(stale), "--out", str(out), "--check"])
    assert bad.returncode == 1
