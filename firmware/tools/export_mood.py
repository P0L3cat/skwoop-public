#!/usr/bin/env python3
"""Export the shipped skwoop-mood weights (v1.1, context-free path) for the pod.

Writes components/mood/mood_v11.bin (header + int8 text table, per-axis
scale) and tests/firmware/mood/golden.tsv: public sentences with
  - the PAD the int8 table must give (dequantized weights, numpy float32):
    the C port has to match this to ~1e-4, which checks the featurizer;
  - the mood name of the float model, to measure what int8 costs.

Usage: tools/export_mood.py <mood-model repo>   (needs numpy)
"""

import csv
import struct
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
MAGIC = b"MOOD"
VERSION = 1
GOLDEN_SETS = ("handwritten.tsv", "handwritten_b.tsv", "handwritten_c.tsv", "handwritten_d.tsv",
               "handwritten_e.tsv", "household_v11.tsv")
GOLDEN_MAX = 600


def main() -> None:
    repo = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(repo))
    from skwoop_mood.features import featurize  # noqa: E402
    from skwoop_mood.model import load_weights  # noqa: E402
    from skwoop_mood.mapping import nearest_mood  # noqa: E402

    wt = load_weights(repo / "weights" / "skwoop_mood.npz")
    cfg = wt.cfg
    scale = np.abs(wt.w).max(axis=0) / 127.0
    q = np.clip(np.rint(wt.w / scale), -127, 127).astype(np.int8)
    deq = q.astype(np.float32) * scale.astype(np.float32)
    protos = np.array([(p.v, p.a, p.d) for p in wt.mood_table], dtype=np.float32)

    header = MAGIC + struct.pack(
        "<IIIII3f3f18f", VERSION, cfg.hash_bits, cfg.char_min, cfg.char_max,
        int(cfg.use_chars) | int(cfg.use_bigrams) << 1, *scale.astype(np.float32), *wt.b, *protos.reshape(-1))
    out = HERE / "components" / "mood" / "mood_v11.bin"
    out.write_bytes(header + q.tobytes(order="C"))
    print(f"export_mood: {out.name} {out.stat().st_size} bytes, hash_bits {cfg.hash_bits}")

    names = [p.name for p in wt.mood_table]
    rows = []
    seen = set()

    for name in GOLDEN_SETS:
        with open(repo / "data" / name, newline="") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                text = row["text"].strip()

                if text and "\t" not in text and text not in seen and len(rows) < GOLDEN_MAX:
                    seen.add(text)
                    idx, vals = featurize(text, cfg)
                    v = np.asarray(vals, dtype=np.float32)
                    pad_q = np.clip(wt.b + v @ deq[idx], -1, 1)
                    pad_f = np.clip(wt.b + v @ wt.w[idx], -1, 1)
                    mood_f = nearest_mood(wt.mood_table, *(float(x) for x in pad_f))
                    rows.append((text, *(f"{float(x):.6f}" for x in pad_q), mood_f))

    gold = HERE.parent / "tests" / "firmware" / "mood" / "golden.tsv"

    with open(gold, "w", newline="") as f:
        f.write("# text\tv_int8\ta_int8\td_int8\tmood_float  (tools/export_mood.py)\n")

        for r in rows:
            f.write("\t".join(r) + "\n")

    print(f"export_mood: {len(rows)} golden lines; moods {names}")


if __name__ == "__main__":
    main()
