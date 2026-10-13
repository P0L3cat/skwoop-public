"""Rebuild webpod/static/face-data.json from an optional face-sim checkout. Robot face only.

    SKWOOP_FACE_SRC=~/face-sim python3 webpod/build_assets.py

The shipped face-data.json is what the web pod serves. This script does not copy images.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"


def build(src: Path) -> dict[str, object]:
    tools = src / "tools"
    if not (tools / "render_va.py").is_file():
        raise SystemExit(f"no face sim under {src} (set SKWOOP_FACE_SRC)")

    sys.path.insert(0, str(tools))
    import face_layout as fl
    import pod_mood as pm
    import render_va as rv

    return {
        "screen": rv.W,
        "tick_ms": rv.TICK_MS,
        "pal": rv.PAL,
        "geo": rv.GEO,
        "styles": {k: v for k, v in rv.STYLES.items() if k == "robot"},
        "blink": rv._BLINK,
        "fields": pm.FIELDS,
        "poses": pm.POSES,
        "pose_min": pm.POSE_MIN,
        "pose_max": pm.POSE_MAX,
        "mood_names": {k: [v, a] for k, (v, a) in pm.MOOD_NAMES.items()},
        "smooth_s": pm.SMOOTH_S,
        "squint_arc": pm.SQUINT_ARC,
        "layout": fl.K,
        "robot_state_y": rv.GEO["kRobotStateY"],
        "text_safe_r": fl.K["kTextSafeR"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    default = Path(os.environ.get("SKWOOP_FACE_SRC", "")).expanduser()
    ap.add_argument("--src", type=Path, default=default)
    args = ap.parse_args()
    if not str(args.src):
        raise SystemExit("set SKWOOP_FACE_SRC or pass --src")

    data = build(args.src)
    (STATIC / "face-data.json").write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))
    print(f"face-data.json: {len(data['styles'])} styles, {len(data['mood_names'])} mood names")


if __name__ == "__main__":
    main()
