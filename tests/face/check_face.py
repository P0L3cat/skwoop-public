#!/usr/bin/env python3
"""Headless check for the Skwoop web face.

Serves face/ with python3 -m http.server and opens the page in headless
Chrome. The canvas must contain non-black pixels and the page must not log
console errors. file:// must draw, or show the server hint.
"""

import json
import subprocess
import sys
from pathlib import Path


def face_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "face"


def check_data(face: Path) -> None:
    js = (face / "face-data.js").read_text(encoding="utf-8")
    marker = "window.FACE_DATA = "
    at = js.find(marker)
    if at < 0:
        raise SystemExit("face-data.js is missing window.FACE_DATA")

    blob = js[at + len(marker):].strip()
    if blob.endswith(";"):
        blob = blob[:-1].strip()

    inline = json.loads(blob)
    on_disk = json.loads((face / "face-data.json").read_text(encoding="utf-8"))
    if inline != on_disk:
        raise SystemExit("face-data.js and face-data.json differ")


def check_syntax(face: Path) -> None:
    names = [
        "face.js",
        "demo.js",
        "pad-face.js",
        "face-data.js",
        "sample-face/dot-face.js",
    ]
    for name in names:
        done = subprocess.run(["node", "--check", str(face / name)], check=False)
        if done.returncode != 0:
            raise SystemExit(f"syntax check failed: {name}")


def main() -> int:
    face = face_dir()
    check_data(face)
    check_syntax(face)
    helper = Path(__file__).resolve().parent / "check_face.mjs"
    try:
        done = subprocess.run(["node", str(helper), str(face)], timeout=90, check=False)
    except subprocess.TimeoutExpired:
        print("face check timed out", file=sys.stderr)
        return 1

    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
