"""Accidental glyph detector and the one rule set that turns accidentals into `alter` (CONTRACT section 1).

* AlterState: key signature -> explicit accidental (sharp / flat / natural / double) -> carry within the measure for
  the same staff position (step + octave) -> reset at the barline, except an accidental carried across a barline by a tie.
  Matched and fallback noteheads go through the same code.
* detect_accidental(): template matcher for the glyph just left of a notehead (omr/accidental_templates.npz, built
  by a one-off script (not included) from the Bravura subset), used when the transformer printed no lift for a notehead
  or could not see the notehead at all.
"""
from __future__ import annotations

import os

from clefs import key_alter

LIFT_ALTER = {"N": 0, "#": 1, "##": 2, "b": -1, "bb": -2}
ALTER_LIFT = {0: "N", 1: "#", 2: "##", -1: "b", -2: "bb"}

UPX = 8          # template resolution: px per staff space
WIN_L = 2.6      # window starts this many spaces left of the notehead's left edge ...
WIN_R = 0.1      # ... and ends this far inside it
WIN_H = 2.4      # +- spaces around the notehead centre
_TPL = {}
_K3 = None


class AlterState:
    """Per-staff accidental state while walking noteheads left to right (chord members: any order)."""

    def __init__(self, key=0):
        self.key = key
        self.measure = None
        self.acc = {}          # (step, octave) -> alter, valid in the current measure
        self.prev_acc = {}     # same, of the previous measure (for ties)

    def new_measure(self, m, tied_over=()):
        if m == self.measure:
            return
        self.prev_acc = dict(self.acc)
        self.acc = {}
        for k in tied_over:  # a tie across the barline keeps the accidental of the note it continues
            if k in self.prev_acc:
                self.acc[k] = self.prev_acc[k]
        self.measure = m

    def alter(self, step, octave, explicit=None):
        """explicit = lift symbol ('#', 'b', 'N', '##', 'bb') printed at this notehead, or None."""
        if explicit in LIFT_ALTER:
            a = LIFT_ALTER[explicit]
            self.acc[(step, octave)] = a
            return a
        if (step, octave) in self.acc:
            return self.acc[(step, octave)]
        return key_alter(self.key, step)


def _templates():
    if not _TPL:
        import numpy as np
        z = np.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "accidental_templates.npz"))
        for k in z.files:
            _TPL[k] = z[k].astype("float32") / 255.0
    return _TPL


def prep_page(gray, unit):
    """Binary page (255 = ink) with staff lines removed (horizontal opening, then a small vertical bridge so glyphs
    crossing a line stay in one piece)."""
    import cv2
    import numpy as np
    _, b = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, int(round(1.8 * unit))), 1))
    lines = cv2.morphologyEx(b, cv2.MORPH_OPEN, k)
    b = cv2.subtract(b, cv2.dilate(lines, np.ones((3, 1), np.uint8)))
    kv = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, int(round(0.45 * unit)))))
    return cv2.morphologyEx(b, cv2.MORPH_CLOSE, kv)


LAST_SCORES = {}  # debug: best IoU per lift class of the last detect_accidental call


def detect_accidental(binpage, x_left, cy, unit):
    """Look left of a notehead (left edge x_left, centre y cy, staff space `unit`, input px).  Returns (lift, score) with
    lift in '#','b','N','##','bb' or None; score = mask IoU (0..1) of the best glyph-sized connected component against
    the matching template.  Candidates must sit on the notehead's line/space (+-0.8 space) with their right edge within
    1.7 spaces left of the head, and have the template's size (+-0.5 space)."""
    import cv2
    import numpy as np
    global _K3
    if _K3 is None:
        _K3 = np.ones((3, 3), np.uint8)
    if unit < 4:
        return None, 0.0
    H, W = binpage.shape[:2]
    xa, xb = int(max(0, x_left - 2.4 * unit)), int(min(W, x_left + 0.15 * unit))
    ya, yb = int(max(0, cy - 2.4 * unit)), int(min(H, cy + 2.4 * unit))
    if xb - xa < 3 or yb - ya < 3:
        return None, 0.0
    crop = (binpage[ya:yb, xa:xb] > 0).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(crop, connectivity=8)
    comps = [k for k in range(1, n) if st[k][4] >= 6]
    # candidates: every component, plus pairs that touch or nearly touch (a flat's stem and bowl are often split by
    # staff-line removal)
    cands = [(k,) for k in comps]
    gap = 0.12 * unit
    for i, a_ in enumerate(comps):
        for b_ in comps[i + 1:]:
            xa1, ya1, wa, ha = st[a_][:4]
            xb1, yb1, wb, hb = st[b_][:4]
            dx = max(0, max(xa1, xb1) - min(xa1 + wa, xb1 + wb))
            dy = max(0, max(ya1, yb1) - min(ya1 + ha, yb1 + hb))
            if dx <= gap and dy <= gap:
                cands.append((a_, b_))
    best = (None, 0.0)
    per = {}
    for ks in cands:
        x = min(st[k][0] for k in ks)
        y = min(st[k][1] for k in ks)
        w = max(st[k][0] + st[k][2] for k in ks) - x
        h = max(st[k][1] + st[k][3] for k in ks) - y
        wu, hu = w / unit, h / unit
        cxr = (xa + x + w) - x_left          # right edge relative to the head's left edge (px)
        gy = (ya + y + h / 2.0) - cy
        if abs(gy) > 0.8 * unit or cxr > 0.15 * unit or cxr < -1.7 * unit or wu < 0.4 or hu < 0.7:
            continue
        m = np.isin(lab[y:y + h, x:x + w], ks).astype(np.float32)
        if m.sum() / float(w * h) > 0.72:   # a solid blob is a notehead / dot, not an accidental
            continue
        for lift, t in _templates().items():
            th, tw = t.shape
            if abs(hu - th / UPX) > 0.5 or abs(wu - tw / UPX) > 0.45:
                continue
            r = (cv2.resize(m, (tw, th), interpolation=cv2.INTER_AREA) > 0.4).astype(np.uint8)
            r = (cv2.dilate(r, _K3) if lift != "##" else r) > 0   # tolerate +-1 template px of stroke-width / offset difference
            tb = (cv2.dilate((t > 0.5).astype(np.uint8), _K3) if lift != "##" else (t > 0.5).astype(np.uint8)) > 0
            iou = float((r & tb).sum()) / max(1.0, float((r | tb).sum()))
            per[lift] = max(per.get(lift, 0.0), iou)
            if iou > best[1]:
                best = (lift, iou)
    LAST_SCORES.clear()
    LAST_SCORES.update(per)
    return best


def tie_between(binpage, a, b, unit):
    """True if an arc joins notehead box `a` = (x0, y0, x1, y1) to the next same-pitch notehead `b` (input px): a band just
    above or just below the heads that is inked over >= 85 % of the columns between them (barline columns excluded)."""
    H, W = binpage.shape[:2]
    xa, xb = a[2], b[0]
    if xb - xa < 0.8 * unit or xb - xa > 14 * unit:
        return False
    cy = (a[1] + a[3] + b[1] + b[3]) / 4.0
    x0, x1 = int(xa + 0.15 * (xb - xa)), int(xb - 0.15 * (xb - xa))
    if x1 - x0 < 4:
        return False
    for sgn in (1, -1):
        y0, y1 = sorted((int(cy + sgn * 0.45 * unit), int(cy + sgn * 1.7 * unit)))
        y0, y1 = max(0, y0), min(H, y1)
        if y1 - y0 < 2:
            continue
        band = binpage[y0:y1, x0:x1] > 0
        cols = band.any(axis=0)
        full = band.all(axis=0)  # a barline is a full-height run: ignore those columns
        use = ~full
        if use.sum() >= 4 and cols[use].mean() >= 0.85:
            return True
    return False
