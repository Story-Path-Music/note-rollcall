"""Four-clef support in the OMR adapter (omr/clefs.py + omr/run_engine.py): clef -> pitch, staff_pos round trip,
fallback path, clef-change decoding, detector policy, result fields.  No OMR model is run."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

OMR = Path(__file__).resolve().parents[2] / "omr"
sys.path.insert(0, str(OMR))

import clefs  # noqa: E402
import run_engine as re_  # noqa: E402

FOUR = ["G2", "F4", "C3", "C4"]


# (clef, staff_pos, expected step+octave): 0 = bottom line, 8 = top line, negative = ledger lines below
PITCH_CASES = [
    # treble: bottom line E4, middle C is the first ledger line below (-2)
    ("G2", 0, "E4"), ("G2", 4, "B4"), ("G2", 8, "F5"), ("G2", -2, "C4"), ("G2", -4, "A3"), ("G2", 10, "A5"),
    ("G2", 14, "E6"),
    # bass: bottom line G2, middle C is the first ledger line above (10)
    ("F4", 0, "G2"), ("F4", 6, "F3"), ("F4", 8, "A3"), ("F4", 9, "B3"), ("F4", 10, "C4"), ("F4", 12, "E4"), ("F4", -2, "E2"),
    ("F4", -4, "C2"),
    # alto: middle line = C4 (pos 4)
    ("C3", 0, "F3"), ("C3", 4, "C4"), ("C3", 8, "G4"), ("C3", -2, "D3"), ("C3", 10, "B4"), ("C3", -4, "B2"),
    # tenor: fourth line = C4 (pos 6)
    ("C4", 0, "D3"), ("C4", 6, "C4"), ("C4", 8, "E4"), ("C4", -2, "B2"), ("C4", 10, "G4"), ("C4", -4, "G2"),
]


@pytest.mark.parametrize("clef,pos,expect", PITCH_CASES)
def test_clef_pitch_mapping_incl_ledger_lines(clef, pos, expect):
    step, octv = clefs.pitch_from_staff_pos(clef, pos)
    assert f"{step}{octv}" == expect


def test_middle_c_position_per_clef():
    # the same pitch C4 sits at a different staff position in each clef
    assert [clefs.staff_pos_of(c, "C", 4) for c in FOUR] == [-2, 10, 4, 6]


@pytest.mark.parametrize("fifths,step,alter", [(2, "F", 1), (2, "C", 1), (2, "G", 0), (-3, "B", -1), (-3, "A", -1),
                                               (-3, "D", 0), (0, "F", 0), (7, "B", 1), (-7, "F", -1)])
def test_key_signature_alter(fifths, step, alter):
    assert clefs.key_alter(fifths, step) == alter


@pytest.mark.parametrize("clef", FOUR)
def test_key_signature_applies_the_same_in_every_clef(clef):
    # F on the bottom space of whichever clef ... any notehead whose letter is F gets the sharp in G major
    for pos in range(-6, 16):
        step, _ = clefs.pitch_from_staff_pos(clef, pos)
        assert clefs.key_alter(1, step) == (1 if step == "F" else 0)


@pytest.mark.parametrize("clef", list(clefs.CLEF_BOTTOM))
def test_staff_pos_round_trip(clef):
    for pos in range(-14, 24):
        step, octv = clefs.pitch_from_staff_pos(clef, pos)
        assert clefs.staff_pos_of(clef, step, octv) == pos


def test_staff_pos_reinterpretation_for_ui_override():
    # a note read under bass clef (A2, pos 1) means another pitch under each other clef: UI recompute = pitch_from_staff_pos(new, pos)
    pos = clefs.staff_pos_of("F4", "A", 2)
    assert pos == 1
    assert "".join(map(str, clefs.pitch_from_staff_pos("G2", pos))) == "F4"
    assert "".join(map(str, clefs.pitch_from_staff_pos("C3", pos))) == "G3"
    assert "".join(map(str, clefs.pitch_from_staff_pos("C4", pos))) == "E3"


def test_contract_clef_names():
    assert [clefs.clef_name(c) for c in FOUR] == ["treble", "bass", "alto", "tenor"]
    assert clefs.clef_name("G1") is None


# ---- fallback path (notehead without a matched transformer note) ---------------------------------------------------

@pytest.mark.parametrize("clef,hs,key,expect", [
    ("F4", 4, 0, ("D", 3, 0)), ("F4", 8, 1, ("A", 3, 0)), ("F4", -2, -2, ("E", 2, -1)),
    ("C3", 4, 0, ("C", 4, 0)), ("C3", 2, 2, ("A", 3, 0)), ("C3", 7, 2, ("F", 4, 1)),
    ("C4", 6, 0, ("C", 4, 0)), ("C4", -2, -2, ("B", 2, -1)), ("C4", 10, 1, ("G", 4, 0)),
    ("G2", 0, 0, ("E", 4, 0)), ("G2", 2, -1, ("G", 4, 0)), ("G2", 9, 3, ("G", 5, 1)),
])
def test_fallback_pitch_uses_the_staffs_clef_and_key(clef, hs, key, expect):
    step, octv, alter, conf, k = re_._notehead_pitch(clef, hs, None, key)
    assert (step, octv, alter) == expect
    assert conf == 0.3 and k == key


def test_matched_and_unmatched_noteheads_agree_on_letter_and_octave():
    for clef in FOUR:
        for hs in range(-4, 13):
            ev = ("upper", 0, "C", 9, 1, -2)  # transformer says something else entirely; geometry wins (cv mode)
            m = re_._notehead_pitch(clef, hs, ev, 0)
            u = re_._notehead_pitch(clef, hs, None, 0)
            assert m[:2] == u[:2]
            assert m[2] == 1 and m[3] is None and m[4] == -2  # accidental + key come from the matched event
    # model-pitch mode keeps the transformer's letter
    assert re_._notehead_pitch("F4", 3, ("upper", 0, "C", 3, 0, 0), 0, "tr")[:2] == ("C", 3)


# ---- decoding of homr tokens ---------------------------------------------------------------------------------------

def sym(rhythm, pitch=".", lift=".", position="upper"):
    return SimpleNamespace(rhythm=rhythm, pitch=pitch, lift=lift, position=position)


def test_decode_first_clef_wins_and_changes_are_sequenced():
    syms = [sym("clef_F4"), sym("keySignature_1"), sym("note_4", "G2"), sym("note_4", "A2"),
            sym("barline"), sym("clef_C4"), sym("note_4", "C4"), sym("note_4", "D4"), sym("clef_F4"),
            sym("note_4", "E2")]
    init, first_key, events, seq = re_._decode_homr_symbols(syms)
    assert init["upper"] == "F4" and first_key == 1
    assert seq["upper"] == [(2, "C4"), (4, "F4")]  # change to tenor before note group 2, back to bass before group 4
    assert [e[1] for e in events] == [0, 1, 2, 3, 4]
    assert [e[5] for e in events] == [1] * 5  # key in effect is recorded per event


def test_decode_repeated_clef_token_is_not_a_change():
    init, _, _, seq = re_._decode_homr_symbols([sym("clef_C3"), sym("clef_C3"), sym("note_4", "C4")])
    assert init["upper"] == "C3" and seq["upper"] == []


def test_decode_grand_staff_halves():
    init, _, events, _ = re_._decode_homr_symbols(
        [sym("clef_G2", position="upper"), sym("clef_F4", position="lower"),
         sym("note_4", "C5", position="upper"), sym("note_4", "C3", position="lower")])
    assert init == {"upper": "G2", "lower": "F4"}
    assert [e[0] for e in events] == ["upper", "lower"]


# ---- model token vs glyph detector ---------------------------------------------------------------------------------

def scores(**kw):
    base = {"G2": 0.0, "F4": 0.0, "C3": 0.0, "C4": 0.0}
    base.update(kw)
    return base


def test_resolve_clef_policy():
    # agree
    c, conf, src = re_._resolve_clef("F4", scores(F4=0.9))
    assert (c, src) == ("F4", "both") and conf > 0.9
    # transformer read alto as treble, glyph is a clear alto -> glyph wins
    c, conf, src = re_._resolve_clef("G2", scores(C3=0.9, G2=0.1))
    assert (c, src) == ("C3", "detector") and conf >= 0.5
    # weak glyph evidence never overrides the model
    assert re_._resolve_clef("G2", scores(C3=0.25, G2=0.1))[:1] == ("G2",)
    # no token: glyph if it is any good, else unknown (caller applies the default and warns)
    assert re_._resolve_clef(None, scores(C4=0.8))[0] == "C4"
    assert re_._resolve_clef(None, scores(C4=0.02))[0] is None
    # no detector at all
    assert re_._resolve_clef("F4", None) == ("F4", 0.5, "model")
    # a clef outside the four (e.g. G1) is replaced only on clear evidence
    assert re_._resolve_clef("G1", scores(G2=0.9))[0] == "G2"
    assert re_._resolve_clef("G1", scores(G2=0.2))[0] == "G1"


# ---- result fields -------------------------------------------------------------------------------------------------

def test_finalize_staves_order_ids_and_fields():
    res = {
        "staves": [
            {"id": "s0", "clef": "F4", "key_fifths": 0, "bbox": [50, 400, 900, 40]},   # lower on the page, emitted first
            {"id": "s1", "clef": "G2", "key_fifths": 0, "bbox": [50, 100, 900, 40], "clef_confidence": 0.9,
             "clef_changes": [{"x": 500, "clef": "C3"}]},
            {"id": "s2", "clef": "C4", "key_fifths": 0, "bbox": [50, 700, 900, 40]},
        ],
        "notes": [
            {"id": "n0", "staff": "s0", "step": "G", "alter": 0, "octave": 2, "bbox": [0, 0, 1, 1]},
            {"id": "n1", "staff": "s1", "step": "E", "alter": 0, "octave": 4, "bbox": [0, 0, 1, 1]},
            {"id": "n2", "staff": "s1", "step": "G", "alter": 0, "octave": 3, "bbox": [0, 0, 1, 1], "clef": "C3"},
            {"id": "n3", "staff": "s2", "step": "C", "alter": 0, "octave": 4, "bbox": [0, 0, 1, 1]},
        ],
    }
    re_.finalize_staves(res)
    assert [s["clef_name"] for s in res["staves"]] == ["treble", "bass", "tenor"]
    assert [s["id"] for s in res["staves"]] == ["s0", "s1", "s2"]
    assert [s["index"] for s in res["staves"]] == [0, 1, 2]
    assert res["staves"][0]["clef_confidence"] == 0.9 and res["staves"][1]["clef_confidence"] is None
    assert res["staves"][0]["clef_changes"] == [{"x": 500, "clef": "C3"}]
    got = {n["id"]: (n["staff"], n["staff_index"], n["clef"], n["staff_pos"]) for n in res["notes"]}
    assert got == {"n0": ("s1", 1, "F4", 0), "n1": ("s0", 0, "G2", 0), "n2": ("s0", 0, "C3", 1),
                   "n3": ("s2", 2, "C4", 6)}


def test_mock_engine_result_has_clef_fields(tmp_path):
    import json
    out = tmp_path / "r.json"
    img = tmp_path / "p.png"
    from PIL import Image
    Image.new("RGB", (1000, 1400), "white").save(img)
    assert re_.main(["--engine", "mock", "--image", str(img), "--out", str(out)]) == 0
    r = json.loads(out.read_text(encoding="utf-8"))
    assert [s["index"] for s in r["staves"]] == [0, 1, 2]
    assert [s["clef_name"] for s in r["staves"]] == ["treble", "bass", "treble"]
    n = r["notes"][0]
    assert n["staff_index"] == 0 and n["clef"] == "G2"
    assert clefs.pitch_from_staff_pos(n["clef"], n["staff_pos"]) == (n["step"], n["octave"])


def test_glyph_detector_on_synthetic_templates():
    """The shipped templates must classify themselves (cheap guard against a corrupt/old clef_templates.npz)."""
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")
    unit = 20.0
    for code in FOUR:
        t = clefs._templates()[code]
        # paste the template (8 px/space) onto a white page at 20 px/space with five staff lines
        big = cv2.resize(t, None, fx=unit / clefs.UPX, fy=unit / clefs.UPX, interpolation=cv2.INTER_LINEAR)
        page = np.full((int(13 * unit) + 60, 400), 255, np.uint8)
        top = 30 + int(4.5 * unit)
        ys = [top + k * unit for k in range(5)]
        for y in ys:
            page[int(y) - 1: int(y) + 2, 60:380] = 0
        x0 = 60
        h, w = big.shape
        region = page[30: 30 + h, x0: x0 + w]
        region[big > 120] = 0
        sc = clefs.detect_clef(page, float(x0), ys)
        assert max(sc, key=sc.get) == code
