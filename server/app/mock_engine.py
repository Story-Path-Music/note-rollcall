"""Standalone mock OMR engine (same CLI as omr/run_engine.py). Used when the real one is absent.

Env knobs for tests: MOCK_ENGINE_DELAY_MS (total, split over the stages), MOCK_ENGINE_FAIL=1.
Emits the same `@@progress` stderr markers as omr/run_engine.py (docs/CONTRACT.md section 1).
"""
import argparse
import json
import os
import struct
import sys
import time


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", head[16:24])


STAGES = (("detect", 0.3, 2), ("transcribe", 0.55, 4), ("post", 0.15, 2))  # (stage, delay share, steps)


def progress(stage, p):
    sys.stderr.write('@@progress {"stage":"%s","p":%s}\n' % (stage, round(p, 4)))
    sys.stderr.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="mock")
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if os.environ.get("MOCK_ENGINE_FAIL") == "1":
        sys.stderr.write("mock engine forced failure\n")
        sys.exit(3)
    d = int(os.environ.get("MOCK_ENGINE_DELAY_MS", "0") or 0)
    for stage, share, steps in STAGES:
        for k in range(steps):
            progress(stage, k / steps)
            if d:
                time.sleep(d * share / steps / 1000)
        progress(stage, 1.0)
    w, h = png_size(a.image)
    sx, sy = w * 0.1, h * 0.1
    sw, sh = w * 0.8, h * 0.04
    steps = ["C", "D", "E", "F", "G", "A", "B", "C"]
    notes = []
    for i, st in enumerate(steps):
        nw = sw / 24
        notes.append({
            "id": f"n{i}", "staff": "s0",
            "bbox": [round(sx + (i + 1) * sw / 9, 1), round(sy + sh * 0.5 + (i % 3) * sh * 0.1, 1),
                     round(nw, 1), round(nw * 0.75, 1)],
            "step": st, "alter": 0, "octave": 4 + (1 if i == 7 else 0), "confidence": 0.9,
            "staff_index": 0, "clef": "G2", "staff_pos": i - 2 if i < 7 else 5,  # E4 = 0 under treble
        })
    out = {
        "engine": "mock", "engine_version": "0", "width_px": w, "height_px": h,
        "staves": [{"id": "s0", "index": 0, "clef": "G2", "clef_name": "treble", "clef_confidence": None,
                    "clef_changes": [], "key_fifths": 0, "bbox": [sx, sy, sw, sh]}],
        "notes": notes, "elapsed_ms": d, "warnings": [],
    }
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False)
    progress("done", 1.0)


if __name__ == "__main__":
    main()
