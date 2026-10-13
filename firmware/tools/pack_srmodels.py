#!/usr/bin/env python3
"""Repack the esp-sr model image with our own models added.

esp-sr's movemodel.py packs only the models picked in sdkconfig into
build/srmodels/srmodels.bin. This adds every model directory under the
--extra roots (for example models/wakenet/wn9_heyskwoop from the Hey Skwoop
training kit), packs with esp-sr's own pack_models, and refuses an image that
does not fit the model partition.
"""

import argparse
import os
import shutil
import sys
import tempfile

# Files esp-sr expects per model family; checked before packing.
REQUIRED = {
    "wn9_": ("_MODEL_INFO_", "wn9_data", "wn9_index"),
}
NAME_MAX = 31  # model_name is char[32] in the packed header


def model_dirs(root):
    if not os.path.isdir(root):
        return []

    return sorted(
        os.path.join(root, name) for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))
    )


def check_model(path):
    name = os.path.basename(path)

    if len(name) > NAME_MAX:
        raise SystemExit(f"pack_srmodels: model name too long: {name}")

    for prefix, files in REQUIRED.items():
        if name.startswith(prefix):
            missing = [f for f in files if not os.path.isfile(os.path.join(path, f))]

            if missing:
                raise SystemExit(f"pack_srmodels: {name} is missing {', '.join(missing)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--esp-sr", required=True, help="esp-sr component dir (for model/pack_model.py)")
    ap.add_argument("--base", required=True, help="dir with the sdkconfig-selected model dirs")
    ap.add_argument("--extra", action="append", default=[], help="root holding extra model dirs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-bytes", type=lambda s: int(s, 0), required=True)
    args = ap.parse_args()

    sys.path.insert(0, os.path.join(args.esp_sr, "model"))
    from pack_model import pack_models  # noqa: E402  (esp-sr's packer)

    with tempfile.TemporaryDirectory() as stage:
        names = []

        for src in model_dirs(args.base) + [d for root in args.extra for d in model_dirs(root)]:
            name = os.path.basename(src)

            if name in names:
                raise SystemExit(f"pack_srmodels: duplicate model {name}")

            check_model(src)
            shutil.copytree(src, os.path.join(stage, name))
            names.append(name)

        pack_models(stage, "srmodels.bin")
        size = os.path.getsize(os.path.join(stage, "srmodels.bin"))

        if size > args.max_bytes:
            raise SystemExit(f"pack_srmodels: {size} bytes > model partition {args.max_bytes}")

        shutil.copyfile(os.path.join(stage, "srmodels.bin"), args.out)
        print(f"pack_srmodels: {', '.join(names)} -> {size} of {args.max_bytes} bytes")


if __name__ == "__main__":
    main()
