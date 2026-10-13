#!/usr/bin/env python3
"""Host check for tools/pack_srmodels.py: the custom wake model lands in the
image next to the stock ones, broken models and oversize images are refused."""

import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "..", "..", "..", "firmware", "tools", "pack_srmodels.py")


def fake_model(root, name, files=("_MODEL_INFO_", "wn9_data", "wn9_index"), size=100):
    os.makedirs(os.path.join(root, name))

    for f in files:
        with open(os.path.join(root, name, f), "wb") as out:
            out.write(os.urandom(size))


def packed_names(path):
    data = open(path, "rb").read()
    count = struct.unpack_from("I", data, 0)[0]
    at = 4
    names = []

    for _ in range(count):
        names.append(data[at:at + 32].rstrip(b"\0").decode())
        files = struct.unpack_from("I", data, at + 32)[0]
        at += 36 + files * 40

    return sorted(names)


def run(esp_sr, base, extra, out, max_bytes):
    return subprocess.run(
        [sys.executable, TOOL, "--esp-sr", esp_sr, "--base", base, "--extra", extra, "--out", out,
         "--max-bytes", str(max_bytes)],
        capture_output=True, text=True)


def main():
    esp_sr = sys.argv[1]

    with tempfile.TemporaryDirectory() as t:
        base, extra, out = os.path.join(t, "base"), os.path.join(t, "extra"), os.path.join(t, "srmodels.bin")
        fake_model(base, "wn9_hiesp")
        fake_model(extra, "wn9_heyskwoop")

        r = run(esp_sr, base, extra, out, 0x7E0000)
        assert r.returncode == 0, r.stderr
        assert packed_names(out) == ["wn9_heyskwoop", "wn9_hiesp"]

        r = run(esp_sr, base, extra, out, 500)
        assert r.returncode != 0 and "model partition" in r.stderr

        fake_model(extra, "wn9_broken", files=("_MODEL_INFO_",))
        r = run(esp_sr, base, extra, out, 0x7E0000)
        assert r.returncode != 0 and "missing wn9_data" in r.stderr

    print("pack ok")


if __name__ == "__main__":
    main()
