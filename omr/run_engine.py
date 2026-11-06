#!/usr/bin/env python
"""OMR engine adapter (CONTRACT section 1).

    <engine-python> omr/run_engine.py --engine <oemer|homr|mock> --image <page.png> --out <result.json>

Writes one result.json per page image with per-notehead bounding boxes (input-image pixel
coordinates), pitch (step/alter/octave as sounding-as-written, i.e. key signature and accidentals
applied), plus clef / key_fifths per staff.  Exit 0 on success, !=0 with the reason on stderr.

Engines
  oemer  (MIT)     uses oemer's internal layers (NoteHead.bbox, staff_line_pos) + hooks the MusicXML
                   builder to read the resolved pitch; coordinates are mapped back through oemer's
                   dewarp grid and resize to the input image.
  homr   (AGPL-3)  uses homr's classical-CV notehead/staff detector for coordinates and the
                   transformer's pitch/accidental/clef/key tokens; the two are aligned per staff.
  mock             stdlib only, fake notes sized to the image (development).

Environment: OMR_THREADS (default 4), OMR_GPU=1 (try CUDA if onnxruntime has it).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import traceback
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accidentals  # noqa: E402  AlterState / accidental glyph detector (omr/accidentals.py)
import clefs  # noqa: E402  clef codes / staff_pos <-> pitch / glyph detector (omr/clefs.py)
from clefs import CLEF_BOTTOM, SHARP_ORDER, FLAT_ORDER, STEPS, dia, from_dia, key_alter  # noqa: E402,F401
THREADS = int(os.environ.get("OMR_THREADS", "4"))
USE_GPU = os.environ.get("OMR_GPU") == "1"

def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def progress(stage, p):
    """Machine-readable progress marker on stderr (CONTRACT section 1): `@@progress {"stage":..,"p":..}`.
    stage in detect|transcribe|post|done; p = 0..1 progress *within* that stage.  Other consumers ignore the line."""
    try:
        eprint("@@progress " + json.dumps({"stage": stage, "p": round(max(0.0, min(1.0, float(p))), 4)},
                                           separators=(",", ":")))
    except Exception:  # noqa: BLE001  progress must never break a run
        pass


def pitch_from_halfspaces(clef, hs):
    """hs = half staff-spaces above the bottom line.  Returns (step, octave)."""
    return clefs.pitch_from_staff_pos(clef, hs)


# ---------------------------------------------------------------------------------------------
# image helpers
# ---------------------------------------------------------------------------------------------

def image_size(path):
    """(w, h) from PNG / JPEG headers, stdlib only."""
    with open(path, "rb") as f:
        head = f.read(26)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return w, h
        if head[:2] == b"\xff\xd8":
            f.seek(2)
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                while b == b"\xff":
                    b = f.read(1)
                if not b:
                    break
                marker = b[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    f.read(3)
                    h, w = struct.unpack(">HH", f.read(4))
                    return w, h
                ln = struct.unpack(">H", f.read(2))[0]
                f.seek(ln - 2, 1)
    return None


def patch_ort():
    import onnxruntime as ort

    orig = ort.InferenceSession.__init__

    def init(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kw):
        if sess_options is None:
            sess_options = ort.SessionOptions()
        if not sess_options.intra_op_num_threads:
            sess_options.intra_op_num_threads = THREADS
        sess_options.inter_op_num_threads = 1
        if not USE_GPU or "CUDAExecutionProvider" not in ort.get_available_providers():
            providers = ["CPUExecutionProvider"]
            provider_options = None
        orig(self, path_or_bytes, sess_options, providers, provider_options, **kw)

    ort.InferenceSession.__init__ = init


def setup_threads():
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(k, str(THREADS))


def pkg_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------------------------

def run_mock(image, warnings):
    # fake stage timeline so the UI can be tested: total OMR_MOCK_DELAY_MS (default 1200) split over the stages
    total = float(os.environ.get("OMR_MOCK_DELAY_MS", "1200") or 0) / 1000.0
    for st, share, steps in (("detect", 0.3, 3), ("transcribe", 0.55, 6), ("post", 0.15, 2)):
        for k in range(steps):
            progress(st, k / steps)
            time.sleep(total * share / steps)
        progress(st, 1.0)
    size = image_size(image)
    if size is None:
        warnings.append("mock: could not read image size, assuming 1000x1400")
        size = (1000, 1400)
    w, h = size
    staves, notes = [], []
    space = max(6.0, w / 90.0)
    n_staves = 3
    x0, x1 = 0.08 * w, 0.92 * w
    pitches = [("C", 0, 5), ("B", -1, 4), ("A", 0, 4), ("G", 1, 4), ("F", 0, 4), ("E", 0, 4), ("D", 0, 4), ("C", 0, 4)]
    for si in range(n_staves):
        y = (0.12 + si * 0.28) * h
        clef = "G2" if si != 1 else "F4"
        staves.append({"id": f"s{si}", "clef": clef, "key_fifths": -1 if si == 0 else 0,
                       "bbox": [x0, y, x1 - x0, 4 * space]})
        for i, (st, al, oc) in enumerate(pitches):
            cx = x0 + (i + 1) * (x1 - x0) / (len(pitches) + 1)
            cy = y + 4 * space - (i * 0.5) * space
            notes.append({"id": f"n{len(notes)}", "staff": f"s{si}",
                          "bbox": [cx - 0.6 * space, cy - 0.5 * space, 1.2 * space, space],
                          "step": st, "alter": al, "octave": oc, "confidence": None})
    warnings.append("mock engine: fake notes, not recognised from the image")
    return dict(engine="mock", engine_version="1", width_px=w, height_px=h, staves=staves, notes=notes)


# ---------------------------------------------------------------------------------------------
# oemer
# ---------------------------------------------------------------------------------------------

def _oemer_ensure_weights():
    from oemer import MODULE_PATH
    from oemer.ete import CHECKPOINTS_URL, download_file
    for title, url in CHECKPOINTS_URL.items():
        if not title.endswith(".onnx"):
            continue
        sd = "unet_big" if title.startswith("1st") else "seg_net"
        d = os.path.join(MODULE_PATH, "checkpoints", sd)
        sp = os.path.join(d, title.split("_")[1])
        if not os.path.exists(sp):
            eprint("oemer: downloading", title)
            os.makedirs(d, exist_ok=True)
            download_file(title, url, sp)


def run_oemer(image, warnings):
    import cv2
    import numpy as np
    from argparse import Namespace

    setup_threads()
    cv2.setNumThreads(THREADS)
    patch_ort()
    hang = int(os.environ.get("OMR_HANG_S", "0"))
    if hang:  # diagnostic: print every thread's stack and exit if inference hangs (seen once: 0 CPU after ~17 min)
        import faulthandler
        faulthandler.dump_traceback_later(hang, exit=True)
    progress("detect", 0.0)  # oemer exposes no inner loop: coarse markers only
    _oemer_ensure_weights()
    from oemer import ete, layers
    from oemer import build_system as bs
    from oemer.symbol_extraction import ClefType

    img = cv2.imread(image)
    if img is None:
        raise RuntimeError("cannot read image: " + image)
    H_in, W_in = img.shape[:2]

    tmp = tempfile.mkdtemp(prefix="oemer_")
    try:
        ext = os.path.splitext(image)[1].lower() or ".png"
        local = os.path.join(tmp, "page" + ext)
        shutil.copy(image, local)
        cache = os.path.splitext(image)[0] + ".pkl"  # debugging aid: oemer prediction cache next to the image
        if os.environ.get("OMR_OEMER_CACHE") == "1" and os.path.exists(cache):
            shutil.copy(cache, os.path.join(tmp, "page.pkl"))

        cap = {}
        orig_est = ete.estimate_coords

        def est(staff):
            cx, cy = orig_est(staff)
            cap["cy"] = cy
            cap["shape"] = staff.shape
            return cx, cy

        ete.estimate_coords = est

        rec = {}
        orig_perform = bs.AddNote.perform

        def perform(self, parent_elem=None):
            elem = orig_perform(self, parent_elem)
            try:
                if elem is not None:
                    p = elem.find("pitch")
                    clef = self.ctx.clefs[self.note.track].label
                    key = self.ctx.key.value if self.ctx.key is not None else 0
                    rec[self.note.id] = dict(
                        step=p.find("step").text, alter=int(float(p.find("alter").text)),
                        octave=int(p.find("octave").text),
                        clef="G2" if clef == ClefType.G_CLEF else "F4", key=key)
            except Exception:
                pass
            return elem

        bs.AddNote.perform = perform

        def attempt(deskew):
            ete.clear_data()
            cap.clear()
            rec.clear()
            args = Namespace(img_path=local, output_path=tmp, use_tf=False, save_cache=False,
                             without_deskew=not deskew)
            ete.extract(args)

        try:
            attempt(True)
        except Exception as e:  # oemer's dewarp / builder are brittle on some inputs
            warnings.append(f"oemer failed with deskew ({type(e).__name__}: {e}); retried without deskew")
            attempt(False)

        notes_l = layers.get_layer("notes")
        staffs_l = layers.get_layer("staffs").reshape(-1)
        Hp, Wp = cap["shape"] if "shape" in cap else layers.get_layer("symbols_pred").shape
        cy_map = cap.get("cy")
        sx, sy = W_in / Wp, H_in / Hp

        def map_y(y, x):
            y = min(max(int(round(y)), 0), Hp - 1)
            x = min(max(int(round(x)), 0), Wp - 1)
            if cy_map is not None:
                v = cy_map[y, x]
                if v == v:  # not NaN
                    return float(v)
            return float(y)

        # staves
        staves, skey = [], {}
        for st in sorted(staffs_l, key=lambda s: (s.group, s.track)):
            xl, xr = float(st.x_left), float(st.x_right)
            xc = (xl + xr) / 2
            y0, y1 = map_y(st.y_upper, xc), map_y(st.y_lower, xc)
            sid = f"s{len(staves)}"
            skey[(st.group, st.track)] = sid
            staves.append(dict(id=sid, clef=None, key_fifths=0,
                               bbox=[xl * sx, y0 * sy, (xr - xl) * sx, (y1 - y0) * sy], _g=(st.group, st.track),
                               _unit=float(st.unit_size) * sy))

        notes, fallback = [], 0
        per_staff = {}
        for nh in notes_l:
            if nh.invalid or nh.bbox is None:
                continue
            x1, y1, x2, y2 = [float(v) for v in nh.bbox]
            xc = (x1 + x2) / 2
            ya, yb = map_y(y1, xc), map_y(y2 - 1, xc)
            sid = skey.get((nh.group, nh.track))
            if sid is None:
                continue
            r = rec.get(nh.id)
            if r is None:
                fallback += 1
                tr = nh.track or 0
                clef = "G2" if tr % 2 == 0 else "F4"
                pos = int(nh.staff_line_pos)
                # oemer position 0 = G-clef D4 / F-clef F2 (bottom line E4 = 1 / G2 = 1)
                step, octv = pitch_from_halfspaces(clef, pos - 1)
                r = dict(step=step, alter=0, octave=octv, clef=clef, key=0)
                conf = 0.3
            else:
                conf = None
            per_staff.setdefault(sid, []).append(r)
            notes.append(dict(id=f"n{len(notes)}", staff=sid,
                              bbox=[x1 * sx, ya * sy, (x2 - x1) * sx, max(1.0, (yb - ya) * sy)],
                              step=r["step"], alter=r["alter"], octave=r["octave"], confidence=conf,
                              clef=r["clef"]))
        if fallback:
            warnings.append(f"oemer: {fallback} noteheads were not emitted by oemer's MusicXML builder; "
                            "pitch from staff position only (no accidentals), confidence 0.3")
        # oemer's Staff objects are per-measure chunks of a staff line group: merge chunks lying on the same
        # horizontal row (y overlap) into one staff per row.
        order = sorted(range(len(staves)), key=lambda k: staves[k]["bbox"][1] + staves[k]["bbox"][3] / 2)
        clusters = []  # list of lists of indices
        for k in order:
            b = staves[k]["bbox"]
            yc, h = b[1] + b[3] / 2, max(b[3], 1.0)
            for c in clusters:
                cb = staves[c[0]]["bbox"]
                if abs(yc - (cb[1] + cb[3] / 2)) < 0.7 * max(h, cb[3]):
                    c.append(k)
                    break
            else:
                clusters.append([k])
        clusters.sort(key=lambda c: (staves[c[0]]["bbox"][1], staves[c[0]]["bbox"][0]))
        remap, merged = {}, []
        for ci, c in enumerate(clusters):
            xs0 = min(staves[k]["bbox"][0] for k in c)
            xs1 = max(staves[k]["bbox"][0] + staves[k]["bbox"][2] for k in c)
            ys0 = min(staves[k]["bbox"][1] for k in c)
            ys1 = max(staves[k]["bbox"][1] + staves[k]["bbox"][3] for k in c)
            sid = f"s{ci}"
            for k in c:
                remap[staves[k]["id"]] = sid
            merged.append(dict(id=sid, clef=None, key_fifths=0, bbox=[xs0, ys0, xs1 - xs0, ys1 - ys0]))
        for n in notes:
            n["staff"] = remap[n["staff"]]
        per_m = {}
        for old_sid, rs in per_staff.items():
            per_m.setdefault(remap[old_sid], []).extend(rs)
        from collections import Counter
        for m in merged:
            rs = per_m.get(m["id"], [])
            if rs:
                m["clef"] = Counter(r["clef"] for r in rs).most_common(1)[0][0]
                m["key_fifths"] = Counter(r["key"] for r in rs).most_common(1)[0][0]
        merged = [m for m in merged if m["id"] in per_m]  # drop staves without noteheads
        staves = merged
        if not notes:
            raise RuntimeError("oemer found no noteheads")
        return dict(engine="oemer", engine_version=pkg_version("oemer"), width_px=W_in, height_px=H_in,
                    staves=staves, notes=notes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# homr
# ---------------------------------------------------------------------------------------------

ACC_GLYPH_MIN_OTHER = 0.8  # same for a sharp/flat/double (the transformer seldom omits those)
ACC_GLYPH_MIN = 0.65 # glyph detector: minimum mask IoU to trust an accidental the transformer did not print
LIFT_ALTER = {"N": 0, "#": 1, "##": 2, "b": -1, "bb": -2}


def _decode_homr_symbols(syms):
    """EncodedSymbol list of one staff -> (clefs{pos: initial clef}, first_key,
    events[(pos, group, step, octave, alter, key, bars, lift)], clef_seq{pos: [(group_from, clef)]}).

    `clefs` is the clef of the first clef token per half (NOT the last: a staff may change clef mid-line);
    clef_seq lists every later change with the first note group it applies to."""
    clef = {"upper": None, "lower": None}
    clef_seq = {"upper": [], "lower": []}
    key = None
    first_key = None
    state = {"upper": {}, "lower": {}}
    events = []
    gid = -1
    bars = 0
    in_chord = False
    for s in syms:
        r = s.rhythm
        if r.startswith("clef_"):
            pos = s.position if s.position in clef else "upper"
            if clef[pos] is None:
                clef[pos] = r[5:]
            elif clef[pos] != r[5:] or clef_seq[pos]:
                if (clef_seq[pos][-1][1] if clef_seq[pos] else clef[pos]) != r[5:]:
                    clef_seq[pos].append((gid + 1, r[5:]))
        elif r.startswith("keySignature_"):
            try:
                key = int(r.split("_")[1])
                if first_key is None:
                    first_key = key
            except ValueError:
                pass
        elif "barline" in r or r.startswith("repeat"):
            state = {"upper": {}, "lower": {}}
            bars += 1
        elif r == "chord":
            in_chord = True
        elif r.startswith("note") and s.pitch not in (".", "_") and len(s.pitch) >= 2:
            pos = s.position if s.position in state else "upper"
            step, octv = s.pitch[0], int(s.pitch[1:])
            if s.lift in LIFT_ALTER:
                alter = LIFT_ALTER[s.lift]
                state[pos][(step, octv)] = alter
            else:
                alter = state[pos].get((step, octv), key_alter(key or 0, step))
            if not (in_chord and gid >= 0):
                gid += 1
            in_chord = False
            events.append((pos, gid, step, octv, alter, key or 0, bars, s.lift if s.lift in LIFT_ALTER else None))
        else:
            in_chord = False
    return clef, first_key, events, clef_seq


def _resolve_clef(model_clef, scores):
    """Combine the transformer's clef token with the glyph detector.  Returns (clef, confidence, source)."""
    dc, dconf = clefs.classify(scores) if scores else (None, 0.0)
    if model_clef is None:
        if dc is not None and dconf >= 0.2:
            return dc, dconf, "detector"
        return None, 0.0, None
    if dc is None:
        return model_clef, 0.5, "model"
    if dc == model_clef:
        return model_clef, round(0.6 + 0.4 * dconf, 3), "both"
    if dconf >= 0.5:  # clear glyph evidence against the model's token (e.g. alto read as treble)
        return dc, round(0.8 * dconf, 3), "detector"
    return model_clef, 0.4, "model"


def _notehead_pitch(clef, hs, ev, cur_key, pitch_src="cv", state=None, explicit=None):
    """Pitch of one detected notehead.  Letter and octave always come from the notehead's staff position `hs` under the
    clef in effect there (the same rule for matched and unmatched noteheads).  `alter` follows omr/accidentals.py:
    with an AlterState `state`: key signature -> explicit accidental (`explicit`: the printed lift, from the transformer
    or the glyph detector) -> carry within the measure -> reset at the barline.  Without `state`: a matched transformer
    event `ev` supplies the alter, an unmatched notehead gets the key signature's alter.  Unmatched noteheads get
    confidence 0.3.  Returns (step, octave, alter, confidence, key)."""
    if ev is None:
        step, octv = pitch_from_halfspaces(clef, hs)
        alter = state.alter(step, octv, explicit) if state is not None else key_alter(cur_key, step)
        return step, octv, alter, 0.3, cur_key
    if pitch_src == "tr":
        step, octv = ev[2], ev[3]
    else:
        step, octv = pitch_from_halfspaces(clef, hs)
    if state is not None:
        state.key = ev[5]
        return step, octv, state.alter(step, octv, explicit), None, ev[5]
    return step, octv, ev[4], None, ev[5]


def _align(cv, tr):
    """Needleman-Wunsch on diatonic numbers. cv/tr: lists of dia ints. Returns list of (i, j) matches."""
    n, m = len(cv), len(tr)
    GAP = 1.0
    INF = 1e9

    def sub(a, b):
        d = abs(a - b)
        return 0.0 if d == 0 else (0.5 if d == 1 else (1.0 if d == 2 else INF))

    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        D[i][0] = i * GAP
    for j in range(1, m + 1):
        D[0][j] = j * GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            D[i][j] = min(D[i - 1][j] + GAP, D[i][j - 1] + GAP, D[i - 1][j - 1] + sub(cv[i - 1], tr[j - 1]))
    out = []
    i, j = n, m
    while i > 0 and j > 0:
        c = sub(cv[i - 1], tr[j - 1])
        if c < INF and abs(D[i][j] - (D[i - 1][j - 1] + c)) < 1e-9:
            out.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif abs(D[i][j] - (D[i - 1][j] + GAP)) < 1e-9:
            i -= 1
        else:
            j -= 1
    return out[::-1]


def run_homr(image, warnings):
    import cv2
    import numpy as np
    from concurrent.futures import Future

    setup_threads()
    cv2.setNumThreads(THREADS)
    patch_ort()
    import onnxruntime as ort
    from homr import main as hm
    from homr.staff_parsing import _ensure_same_number_of_staffs, parse_staff_image
    from homr.staff_regions import StaffRegions
    from homr.transformer.configs import Config

    alter_src = os.environ.get("OMR_HOMR_ALTER", "rules")  # rules: omr/accidentals.py; old: pre-fix behaviour (benchmarks)
    binpage = [None]
    n_acc_glyph = 0
    pitch_src = os.environ.get("OMR_HOMR_PITCH", "cv")  # cv: step/octave from notehead geometry; tr: from transformer
    hm.download_weights(False, False, False)
    ort.set_default_logger_severity(3)

    progress("detect", 0.0)
    img0 = cv2.imread(image)
    if img0 is None:
        raise RuntimeError("cannot read image: " + image)
    H_in, W_in = img0.shape[:2]
    gray0 = cv2.cvtColor(img0, cv2.COLOR_BGR2GRAY) if img0.ndim == 3 else img0
    progress("detect", 0.1)  # image loaded

    rec = {"crop": (0, 0, W_in, H_in), "resized": (W_in, H_in)}
    orig_ac, orig_rs = hm.autocrop, hm.resize_image

    def ac(img):
        res = orig_ac(img)
        d = res.__array_interface__["data"][0] - img.__array_interface__["data"][0]
        oy, rem = divmod(d, img.strides[0])
        ox = rem // img.strides[1]
        rec["crop"] = (int(ox), int(oy), res.shape[1], res.shape[0])
        return res

    def rs(img):
        res = orig_rs(img)
        rec["resized"] = (res.shape[1], res.shape[0])
        return res

    def no_title(debug, staff):
        f = Future()
        f.set_result("")
        return f

    hm.autocrop, hm.resize_image, hm.detect_title = ac, rs, no_title
    cfg = hm.ProcessingConfig(False, False, False, False, -1, False, False, False)
    multi, pimg, debug, _tf = hm.detect_staffs_in_image(image, cfg)
    progress("detect", 1.0)  # segmentation + staff detection done

    ox, oy, cw, ch = rec["crop"]
    rw, rh = rec["resized"]
    fx, fy = cw / rw, ch / rh

    def to_in(x, y):
        return ox + x * fx, oy + y * fy

    staffs = _ensure_same_number_of_staffs(multi, pimg)
    nv = len(staffs[0].staffs)
    regions = StaffRegions(staffs)
    tconf = Config()
    tconf.use_gpu_inference = False

    out_staves, out_notes = [], []
    i = 0
    n_total_staffs = max(1, nv * len(staffs))
    progress("transcribe", 0.0)
    n_tr_unmatched = n_cv_unmatched = n_failed = 0
    for voice in range(nv):
        for ms in staffs:
            staff = ms.staffs[voice]
            try:
                syms = parse_staff_image(debug, i, staff, pimg, regions, tconf)
            except Exception as e:
                n_failed += 1
                warnings.append(f"homr: transformer failed on staff {i} ({type(e).__name__}: {e}); geometry-only pitch")
                syms = []
            if os.environ.get("OMR_DUMP_TOKENS"):  # diagnostic: raw transformer tokens per staff
                with open(os.environ["OMR_DUMP_TOKENS"], "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps([i, [[x.rhythm, x.pitch, x.lift, x.position] for x in syms]]) + "\n")
            i += 1
            progress("transcribe", i / n_total_staffs)  # one marker per staff read by the transformer
            init_clefs, first_key, events, clef_seq = _decode_homr_symbols(syms)
            halves = ["upper", "lower"] if staff.is_grandstaff else ["upper"]
            sid_of = {}
            for hf in halves:
                grid = staff.grid
                xs0, xs1 = staff.min_x, staff.max_x
                top = min((p.y[0] if hf == "upper" else p.y[5]) for p in grid)
                bot = max((p.y[4] if hf == "upper" else p.y[9]) for p in grid)
                (bx0, by0), (bx1, by1) = to_in(xs0, top), to_in(xs1, bot)
                # the five staff lines of this half at the left end, in input pixels (for the clef glyph detector)
                p0 = grid[0]
                off = 0 if hf == "upper" else 5
                lys = None
                if len(p0.y) >= off + 5:
                    lys = [to_in(xs0, p0.y[off + k])[1] for k in range(5)]
                scores = None
                try:
                    if lys is not None:
                        scores = clefs.detect_clef(gray0, bx0, lys)
                except Exception as e:  # noqa: BLE001  the detector is an extra check, never fail the page
                    warnings.append(f"clef detector skipped ({type(e).__name__}: {e})")
                model_clef = init_clefs.get(hf)
                clef, cconf, csrc = _resolve_clef(model_clef, scores)
                if clef is None:  # no token and no glyph evidence: grand staff = treble over bass, else treble
                    clef, cconf, csrc = ("G2" if hf == "upper" else "F4"), 0.2, "default"
                    warnings.append(f"homr: staff {i - 1} {hf}: no clef found, assumed {clefs.CLEF_NAME[clef]}")
                elif model_clef is not None and clef != model_clef:
                    warnings.append(f"homr: staff {i - 1} {hf}: transformer read clef {model_clef}, glyph says {clef}; "
                                    "using the glyph")
                sid = f"s{len(out_staves)}"
                sid_of[hf] = sid
                out_staves.append(dict(id=sid, clef=clef, key_fifths=int(first_key or 0),
                                       bbox=[bx0, by0, bx1 - bx0, by1 - by0], clef_confidence=cconf,
                                       clef_source=csrc, clef_changes=[]))
            # CV noteheads
            cvn = {"upper": [], "lower": []}
            for note in staff.get_notes():
                cx, cy = note.center
                pt = staff.get_at(cx)
                if pt is None:
                    continue
                ys = list(pt.y)
                if len(ys) >= 10:
                    hf = "upper" if cy < (ys[4] + ys[5]) / 2 else "lower"
                    ls = ys[:5] if hf == "upper" else ys[5:10]
                else:
                    hf, ls = "upper", ys[:5]
                if hf not in sid_of:
                    hf = "upper"
                unit = (ls[4] - ls[0]) / 4.0
                hs = int(round((ls[4] - cy) / (unit / 2.0)))
                pts = cv2.boxPoints(note.box.box)
                cvn[hf].append(dict(cx=cx, hs=hs, unit=unit, x0=float(pts[:, 0].min()), x1=float(pts[:, 0].max()),
                                    y0=float(pts[:, 1].min()), y1=float(pts[:, 1].max())))
            for hf in halves:
                sidx = int(sid_of[hf][1:])
                clef0 = out_staves[sidx]["clef"]
                cn = cvn[hf]
                # group CV notes by x, sort by (x group, pitch)
                cn.sort(key=lambda d: d["cx"])
                grp, last = [], None
                for d in cn:
                    if last is None or d["cx"] - last > 0.8 * d["unit"]:
                        grp.append([])
                    grp[-1].append(d)
                    last = d["cx"]
                cv_list = [d for g in grp for d in sorted(g, key=lambda d: d["hs"])]
                evs = [e for e in events if e[0] == hf]
                by_g = {}
                for e in evs:
                    by_g.setdefault(e[1], []).append(e)
                tr_list = [e for g in sorted(by_g) for e in sorted(by_g[g], key=lambda e: dia(e[2], e[3]))]
                tr_dia = [dia(e[2], e[3]) for e in tr_list]
                # clef per notehead: initial clef, then each mid-staff change from the x where the transformer's notes
                # stop agreeing with the old clef (between the last notehead that matches exactly under the old clef
                # and the next one)
                for d in cv_list:
                    d["clef"] = clef0
                bounds = []  # [(x, clef)]
                for g_from, new in clef_seq.get(hf, []):
                    if not cv_list or not tr_list:
                        break
                    cv_dia = [dia(*pitch_from_halfspaces(d["clef"], d["hs"])) for d in cv_list]
                    pre = {j for j, e in enumerate(tr_list) if e[1] < g_from}
                    exact = [(a, b) for a, b in _align(cv_dia, tr_dia) if b in pre and cv_dia[a] == tr_dia[b]]
                    if not exact or len(exact) < max(2, len(pre) // 3):
                        warnings.append(f"homr: staff {i - 1}: clef change to {new} seen but could not be placed")
                        continue
                    ia = max(a for a, _ in exact)
                    if ia + 1 >= len(cv_list):
                        continue
                    xb = cv_list[ia]["cx"] + 0.5 * (cv_list[ia + 1]["cx"] - cv_list[ia]["cx"])
                    for d in cv_list:
                        if d["cx"] > xb:
                            d["clef"] = new
                    bounds.append((xb, new))
                if bounds:
                    out_staves[sidx]["clef_changes"] = [dict(x=round(float(to_in(xb, 0)[0]), 2), clef=c)
                                                        for xb, c in bounds]
                cv_dia = [dia(*pitch_from_halfspaces(d["clef"], d["hs"])) for d in cv_list]
                pairs = _align(cv_dia, tr_dia) if cv_list and tr_list else []
                matched = {a: b for a, b in pairs}
                n_tr_unmatched += len(tr_list) - len(pairs)
                # --- alteration: key signature -> printed accidental (transformer lift, else glyph left of the head)
                # -> carry in the measure -> reset at the barline (tie keeps it) -- one rule for matched and unmatched heads
                n_cv = len(cv_list)
                unit_in = (cv_list[0]["unit"] * fy) if cv_list else 1.0
                if binpage[0] is None and n_cv:
                    try:
                        binpage[0] = accidentals.prep_page(gray0, unit_in)
                    except Exception as e:  # noqa: BLE001  extra evidence only, never fail the page
                        warnings.append(f"accidental detector skipped ({type(e).__name__}: {e})")
                        binpage[0] = False
                bp = binpage[0] if binpage[0] is not None and binpage[0] is not False else None
                bar_xs = sorted(b.center[0] for b in staff.get_bar_lines())
                ev_of = [tr_list[matched[ci]] if ci in matched else None for ci in range(n_cv)]
                pitch_of = [pitch_from_halfspaces(d["clef"], d["hs"]) for d in cv_list]
                box_in = []
                for d in cv_list:
                    (a0, b0), (a1, b1) = to_in(d["x0"], d["y0"]), to_in(d["x1"], d["y1"])
                    box_in.append((a0, b0, a1, b1))
                # measure index: transformer barline count for matched heads; unmatched heads follow the nearest
                # earlier matched head plus the homr barlines between them
                meas, last_m, last_cvb = [], None, 0
                for ci, d in enumerate(cv_list):
                    cvb = sum(1 for bx in bar_xs if bx < d["cx"] - 0.2 * d["unit"])
                    if ev_of[ci] is not None:
                        m = ev_of[ci][6]
                    elif last_m is not None:
                        m = last_m + max(0, cvb - last_cvb)
                    else:
                        m = cvb
                    if last_m is not None:
                        m = max(m, last_m)
                    meas.append(m)
                    last_m, last_cvb = m, cvb
                astate = accidentals.AlterState(first_key or 0)
                prev_idx = []   # indices of the previous measure's heads
                cur_idx = []
                n_glyph = 0
                for ci, d in enumerate(cv_list):
                    e = ev_of[ci]
                    if astate.measure is None or meas[ci] != astate.measure:
                        tied = set()
                        if bp is not None and cur_idx:
                            prev_idx = cur_idx
                            firsts = [j for j in range(ci, min(ci + 4, n_cv)) if meas[j] == meas[ci]]
                            for j in firsts:
                                for pj in reversed(prev_idx):
                                    if pitch_of[pj] == pitch_of[j] and pitch_of[j] in astate.acc:
                                        if accidentals.tie_between(bp, box_in[pj], box_in[j], unit_in):
                                            tied.add(pitch_of[j])
                                        break
                        astate.new_measure(meas[ci], tied)
                        cur_idx = []
                    glyph = (None, 0.0)
                    if bp is not None:
                        glyph = accidentals.detect_accidental(bp, box_in[ci][0], (box_in[ci][1] + box_in[ci][3]) / 2.0,
                                                              unit_in)
                    # the transformer's lift only counts when it read the same line/space as the geometry (a shifted
                    # pairing would carry another note's accidental); it rarely omits sharps/flats, often naturals, so
                    # the image evidence needs a higher score for those than for a natural
                    tr_lift = e[7] if (e is not None and (e[2], e[3]) == pitch_of[ci]) else None
                    g_lift = glyph[0] if glyph[1] >= (ACC_GLYPH_MIN if glyph[0] == "N" else ACC_GLYPH_MIN_OTHER) else None
                    explicit = tr_lift or g_lift
                    if tr_lift is None and g_lift is not None:
                        n_glyph += 1
                    step, octv, alter, conf, _k = _notehead_pitch(d["clef"], d["hs"], e, astate.key, pitch_src,
                                                                  state=astate, explicit=explicit)
                    if alter_src == "old":  # comparison switch: transformer alter, key signature for unmatched heads
                        alter = e[4] if e is not None else key_alter(astate.key, step)
                    cur_idx.append(ci)
                    if os.environ.get("OMR_DEBUG_ACC"):
                        print(f"ACC {sid_of[hf]} m{meas[ci]} x{box_in[ci][0]:.0f} {step}{octv} alter={alter} "
                              f"tr_lift={tr_lift} glyph={glyph[0]}:{glyph[1]:.2f} matched={e is not None}", file=sys.stderr)
                    if e is None:
                        n_cv_unmatched += 1
                    a0, b0, a1, b1 = box_in[ci]
                    out_notes.append(dict(id=f"n{len(out_notes)}", staff=sid_of[hf], bbox=[a0, b0, a1 - a0, b1 - b0],
                                          step=step, alter=int(alter), octave=int(octv), confidence=conf,
                                          clef=d["clef"], staff_pos=int(d["hs"])))
                n_acc_glyph += n_glyph
    if n_acc_glyph:
        warnings.append(f"homr: {n_acc_glyph} accidentals the transformer did not output were read from the image")
    if n_cv_unmatched:
        warnings.append(f"homr: {n_cv_unmatched} noteheads had no matching transformer note; pitch from geometry + key "
                        "signature only (no accidentals), confidence 0.3")
    if n_tr_unmatched:
        warnings.append(f"homr: {n_tr_unmatched} transformer notes had no detected notehead (no coordinates, dropped)")
    if not out_notes:
        raise RuntimeError("homr found no noteheads")
    progress("transcribe", 1.0)
    return dict(engine="homr", engine_version=pkg_version("homr"), width_px=W_in, height_px=H_in,
                staves=out_staves, notes=out_notes)


# ---------------------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------------------
# stems (CONTRACT section 1: notes[].stem / notes[].extent)
# ---------------------------------------------------------------------------------------------
# homr's own notehead<->stem association (Note.stem) was found unreliable (stems cut at staff lines: ~30% of
# stems found on a clean engraving, ~1% on scans), so stems are measured on the input image itself, for both engines.

WANT_STEMS = os.environ.get("OMR_STEMS", "1") != "0"
STEM_MIN_UNITS = 1.5    # a vertical run must extend this many staff spaces beyond the notehead to count as a stem
STEM_MAX_UNITS = 7.5    # longer = merged with a barline / brace, capped


def detect_stems(image_path, staves, notes, warnings):
    """Adds notes[i]["stem"] = [x, y, w, h] | None and notes[i]["extent"] = union(head, stem) bbox, input pixels.

    Per notehead: for columns near its left and right edge, follow a dark vertical run (1 px wobble per row, small
    total drift) upwards and downwards from the head centre.  The longest run past the head that is at least
    STEM_MIN_UNITS long is the stem.  In a chord the run passes through the other heads, so every head of a chord
    gets the same (full) stem box.  Whole notes / heads without a stem get null."""
    import cv2
    import numpy as np

    gray = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return 0
    H, W = gray.shape
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if float(np.mean(gray)) < 235:  # not a clean white page (scan / photo): intersect with a local threshold
        loc = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 51, 12)
        both = cv2.bitwise_and(otsu, loc)
        if float(np.count_nonzero(both)) > 0.3 * float(np.count_nonzero(otsu)):
            otsu = both
    B = otsu > 0
    unit_of = {s["id"]: max(4.0, s["bbox"][3] / 4.0) for s in staves}

    def run(x, y, dy, lim, drift_max):
        """length of the dark run from (x, y) going dy (+1 down / -1 up), following +-1 px wobble."""
        x0, n, gap, yy = x, 0, 0, y
        while 0 <= yy < H and n < lim:
            if B[yy, x]:
                gap = 0
            elif x - 1 >= 0 and B[yy, x - 1] and abs(x - 1 - x0) <= drift_max:
                x -= 1
                gap = 0
            elif x + 1 < W and B[yy, x + 1] and abs(x + 1 - x0) <= drift_max:
                x += 1
                gap = 0
            else:
                gap += 1
                if gap > 1:
                    n -= gap - 1
                    break
            n += 1
            yy += dy
        return max(0, n), x

    found = 0
    step = max(1, len(notes) // 20)
    for k, nt in enumerate(notes):
        if k and k % step == 0:
            progress("post", k / len(notes))
        nt["stem"] = None
        nt["extent"] = [round(float(v), 2) for v in nt["bbox"]]
        u = unit_of.get(nt["staff"])
        if u is None:
            continue
        bx, by, bw, bh = nt["bbox"]
        x_l, x_r, cy = bx, bx + bw, by + bh / 2.0
        lim = int(STEM_MAX_UNITS * u)
        drift = max(1, int(0.25 * u))
        best = None
        for edge, sgn in ((x_l, 1), (x_r, -1)):
            for off in np.arange(-0.10, 0.26, 0.05):
                x = int(round(edge + sgn * off * u))
                if not (0 <= x < W):
                    continue
                for y0 in (int(round(cy)), int(round(cy - 0.25 * u)), int(round(cy + 0.25 * u))):
                    if not (0 <= y0 < H) or not B[y0, x]:
                        continue
                    up, xu = run(x, y0, -1, lim, drift)
                    dn, xd = run(x, y0 + 1, 1, lim, drift)
                    beyond = max(up, dn) - 0.5 * u  # run length past a normal notehead's half height (head boxes can be inflated)
                    if beyond >= STEM_MIN_UNITS * u and (best is None or beyond > best[0]):
                        best = (beyond, x, y0, up, dn, xu, xd)
        if best is None:
            continue
        _, x, y0, up, dn, xu, xd = best
        top, bot = y0 - up, y0 + dn
        # stem thickness: narrowest horizontal dark run through x on 15 rows of the stem (staff lines / beams are wider)
        xa, xb, wmin = x, x, 10 ** 9
        for ym in np.linspace(top + 0.1 * (bot - top), bot - 0.1 * (bot - top), 15):
            ym = min(H - 1, max(0, int(ym)))
            if by - 2 <= ym <= by + bh + 2:  # skip rows that cross the notehead itself
                continue
            xs = [xx for xx in range(max(0, x - drift - 1), min(W, x + drift + 2)) if B[ym, xx]]
            if not xs:
                continue
            # dark segments inside the window; keep the narrowest (the stem; flags / beams / staff lines are wider)
            segs, st0 = [], xs[0]
            for k in range(1, len(xs) + 1):
                if k == len(xs) or xs[k] != xs[k - 1] + 1:
                    segs.append((st0, xs[k - 1]))
                    if k < len(xs):
                        st0 = xs[k]
            ra, rb = min(segs, key=lambda sg: sg[1] - sg[0])
            if rb - ra + 1 < wmin and rb - ra + 1 <= 0.4 * u:
                wmin, xa, xb = rb - ra + 1, ra, rb
        sw = max(xb - xa + 1, 0.08 * u)
        nt["stem"] = [round(float(xa), 2), round(float(top), 2), round(float(sw), 2), round(float(bot - top + 1), 2)]
        ex0, ey0 = min(bx, xa), min(by, top)
        ex1, ey1 = max(bx + bw, xa + sw), max(by + bh, bot + 1)
        nt["extent"] = [round(float(v), 2) for v in (ex0, ey0, ex1 - ex0, ey1 - ey0)]
        found += 1
    return found


ENGINES = {"oemer": run_oemer, "homr": run_homr, "mock": run_mock}


def _ensure_utf8_mode():
    """Windows defaults to cp950 etc; engines read bundled UTF-8 files without encoding=. Re-run in UTF-8 mode."""
    if sys.flags.utf8_mode or os.environ.get("OMR_NO_REEXEC") == "1":
        return
    import subprocess
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", OMR_NO_REEXEC="1")
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", os.path.abspath(__file__)] + sys.argv[1:], env=env))


def _redetect_oemer_clefs(image, res, warnings):
    """oemer only knows G and F clefs.  Check every staff's clef glyph; where the detector clearly says otherwise,
    re-derive that staff's pitches from staff_pos under the detected clef (accidentals kept as a delta to the key)."""
    try:
        import cv2
        gray = cv2.imread(image, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            return
        by_staff = {}
        for n in res["notes"]:
            by_staff.setdefault(n["staff"], []).append(n)
        for st in res["staves"]:
            x, y, w, h = st["bbox"]
            sc = clefs.detect_clef(gray, x, [y + k * h / 4.0 for k in range(5)])
            dc, dconf = clefs.classify(sc)
            if dc is None:
                continue
            if dc == st["clef"]:
                st["clef_confidence"], st["clef_source"] = round(0.6 + 0.4 * dconf, 3), "both"
            elif dconf >= 0.5:
                warnings.append(f"oemer: staff {st['id']} clef {st['clef']} -> {dc} (glyph check); pitches recomputed")
                key = st.get("key_fifths") or 0
                for n in by_staff.get(st["id"], []):
                    old_clef = n.get("clef") or st["clef"]
                    pos = clefs.staff_pos_of(old_clef, n["step"], n["octave"])
                    delta = n["alter"] - key_alter(key, n["step"])
                    step, octv = clefs.pitch_from_staff_pos(dc, pos)
                    n.update(step=step, octave=octv, alter=max(-2, min(2, key_alter(key, step) + delta)), clef=dc)
                st["clef"], st["clef_confidence"], st["clef_source"] = dc, round(0.8 * dconf, 3), "detector"
            else:
                st["clef_confidence"], st["clef_source"] = 0.4, "model"
    except Exception as e:  # noqa: BLE001
        warnings.append(f"clef detector skipped ({type(e).__name__}: {e})")


def finalize_staves(res):
    """Reading order + the clef fields of CONTRACT section 1 for every engine.

    Staves are sorted top-to-bottom, left-to-right and renumbered (id = s<index>); every staff gets `index`,
    `clef_name` (treble|bass|alto|tenor, null for other clefs), `clef_confidence`, `clef_changes`; every note gets
    `staff_index`, `clef` (the clef in effect at that notehead) and `staff_pos`."""
    sts = res["staves"]
    order = sorted(range(len(sts)), key=lambda k: (sts[k]["bbox"][1] + sts[k]["bbox"][3] / 2, sts[k]["bbox"][0]))
    remap = {}
    new = []
    for i, k in enumerate(order):
        s = sts[k]
        remap[s["id"]] = f"s{i}"
        s["id"] = f"s{i}"
        s["index"] = i
        s["clef_name"] = clefs.clef_name(s["clef"])
        s.setdefault("clef_confidence", None)
        s.setdefault("clef_source", None)
        s.setdefault("clef_changes", [])
        new.append(s)
    res["staves"] = new
    clef_of = {s["id"]: s["clef"] for s in new}
    for n in res["notes"]:
        n["staff"] = remap[n["staff"]]
        n["staff_index"] = int(n["staff"][1:])
        n["clef"] = n.get("clef") or clef_of[n["staff"]]
        n["staff_pos"] = clefs.staff_pos_of(n["clef"], n["step"], n["octave"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", required=True, choices=sorted(ENGINES))
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.engine != "mock":
        _ensure_utf8_mode()
    if not os.path.isfile(a.image):
        eprint("image not found:", a.image)
        return 2
    warnings = []
    t0 = time.time()
    try:
        res = ENGINES[a.engine](a.image, warnings)
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        eprint(f"{a.engine} failed: {type(e).__name__}: {e}")
        if os.environ.get("OMR_DEBUG"):
            traceback.print_exc()
        return 1
    if a.engine != "mock":
        progress("post", 0.0)  # pitch/coordinate merge finished; stem detection next
    if WANT_STEMS and a.engine != "mock":
        try:
            detect_stems(a.image, res["staves"], res["notes"], warnings)
        except Exception as e:  # noqa: BLE001  stems are optional: never fail the page over them
            warnings.append(f"stem detection skipped ({type(e).__name__}: {e})")
    if a.engine == "oemer":
        _redetect_oemer_clefs(a.image, res, warnings)
    finalize_staves(res)
    res["elapsed_ms"] = int((time.time() - t0) * 1000)
    res["warnings"] = warnings
    order = ["engine", "engine_version", "width_px", "height_px", "staves", "notes", "elapsed_ms", "warnings"]
    res = {k: res[k] for k in order}
    for n in res["notes"]:
        n["bbox"] = [round(float(v), 2) for v in n["bbox"]]
        if "stem" not in n:
            n.pop("extent", None)
    for s in res["staves"]:
        s["bbox"] = [round(float(v), 2) for v in s["bbox"]]
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
        f.write("\n")
    progress("done", 1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
