"""Clef helpers shared by the engine adapters (stdlib; numpy + cv2 only for the detector).

* clef codes follow MusicXML sign+line: G2 treble, F4 bass, C3 alto, C4 tenor (+ G1 F3 F5 C1 C2 C5 for the other
  positions homr's vocabulary can emit).  `CLEF_NAME` maps the four supported ones to the contract's names.
* staff_pos = half staff-spaces above the BOTTOM staff line (0 = bottom line, 8 = top line, -2 = first ledger line
  below, 10 = first above).  pitch = clef bottom-line pitch + staff_pos diatonic steps; this is the only clef-dependent
  step, so a UI can re-derive a pitch for any other clef from staff_pos without re-running OMR.
* detect_clef(): lightweight template matcher on the clef glyph at the start of a staff, for treble/bass/alto/tenor.
"""
from __future__ import annotations

import os

STEPS = ["C", "D", "E", "F", "G", "A", "B"]
SHARP_ORDER = ["F", "C", "G", "D", "A", "E", "B"]
FLAT_ORDER = ["B", "E", "A", "D", "G", "C", "F"]

# bottom-line pitch (step, octave) per clef
CLEF_BOTTOM = {
    "G2": ("E", 4), "G1": ("G", 4), "F4": ("G", 2), "F3": ("B", 2), "F5": ("E", 2),
    "C1": ("C", 4), "C2": ("A", 3), "C3": ("F", 3), "C4": ("D", 3), "C5": ("B", 2),
}
CLEF_NAME = {"G2": "treble", "F4": "bass", "C3": "alto", "C4": "tenor"}
NAME_CLEF = {v: k for k, v in CLEF_NAME.items()}
SUPPORTED = ("G2", "F4", "C3", "C4")


def dia(step, octave):
    return octave * 7 + STEPS.index(step)


def from_dia(d):
    return STEPS[d % 7], d // 7


def key_alter(fifths, step):
    if fifths and fifths > 0 and step in SHARP_ORDER[:fifths]:
        return 1
    if fifths and fifths < 0 and step in FLAT_ORDER[:-fifths]:
        return -1
    return 0


def pitch_from_staff_pos(clef, pos):
    """(step, octave) of a notehead at staff_pos `pos` under `clef` (unknown clef -> treble)."""
    return from_dia(dia(*CLEF_BOTTOM.get(clef or "G2", CLEF_BOTTOM["G2"])) + int(pos))


def staff_pos_of(clef, step, octave):
    return dia(step, octave) - dia(*CLEF_BOTTOM.get(clef or "G2", CLEF_BOTTOM["G2"]))


def clef_name(code):
    return CLEF_NAME.get(code)


# ---------------------------------------------------------------------------------------------
# detector
# ---------------------------------------------------------------------------------------------
UPX = 8            # templates are stored at 8 px per staff space
T_H = 13           # canvas height in spaces: from 4.5 above the top line to 4.5 below the bottom line
T_W = 4            # template width in spaces, starting at the staff's left end
_TPL = {}


def _templates():
    if not _TPL:
        import numpy as np
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clef_templates.npz")
        z = np.load(p)
        for k in SUPPORTED:
            _TPL[k] = z[k].astype("float32")
    return _TPL


def canvas(gray, left_x, ys, pad_x=1.5, width=None):
    """Binary, staff-line-free crop in the template frame (UPX px / space): rows = top-4.5 .. bottom+4.5 spaces, cols =
    left_x - pad_x .. left_x + T_W + 1.5 spaces.  `ys` = the 5 staff line y's at the left end (input px)."""
    import cv2
    import numpy as np
    unit = (ys[4] - ys[0]) / 4.0
    if unit < 3:
        return None
    H, W = gray.shape[:2]
    x0, x1 = left_x - pad_x * unit, left_x + (T_W + 1.5) * unit
    y0, y1 = ys[0] - 4.5 * unit, ys[4] + 4.5 * unit
    out_w, out_h = int(round((x1 - x0) / unit * UPX)), T_H * UPX
    # affine: output px -> input px
    s = unit / UPX
    M = np.array([[s, 0, x0], [0, s, y0]], dtype="float32")
    crop = cv2.warpAffine(gray, M, (out_w, out_h), flags=cv2.INTER_AREA | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    _, b = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (int(1.3 * UPX), 1))
    lines = cv2.morphologyEx(b, cv2.MORPH_OPEN, k)
    b = cv2.subtract(b, cv2.dilate(lines, np.ones((3, 1), np.uint8)))
    return b.astype("float32") / 255.0


def detect_clef(gray, left_x, ys):
    """Score the four supported clefs at the start of one staff.  Returns {code: score in [-1, 1]} (normalised
    cross-correlation of the staff-line-free glyph; ~0.5+ is a clear match) or None if the crop is unusable."""
    import cv2
    img = canvas(gray, left_x, ys)
    if img is None or img.sum() < 20:
        return None
    out = {}
    for k, t in _templates().items():
        h, w = t.shape
        if img.shape[1] < w:
            return None
        r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
        # vertical position is fixed by the staff (only +-0.3 space jitter); x slides
        mid = r.shape[0] // 2
        j = int(0.3 * UPX)
        out[k] = float(r[max(0, mid - j): mid + j + 1].max())
    return out


def classify(scores):
    """(clef, confidence 0..1) from detect_clef() scores; confidence mixes absolute score and margin to the runner-up."""
    if not scores:
        return None, 0.0
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    best, s1 = ordered[0]
    s2 = ordered[1][1] if len(ordered) > 1 else 0.0
    conf = (0.6 * s1 + 0.8 * (s1 - s2)) * (1.0 if s1 >= 0.35 else 0.5)  # weak absolute match: halve
    conf = max(0.0, min(1.0, conf))
    return best, round(conf, 3)
