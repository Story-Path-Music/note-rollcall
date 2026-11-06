"""Pitch alteration rules of the OMR adapter (omr/accidentals.py + run_engine._notehead_pitch): key signature, printed
accidentals (natural cancels the key), carry within the measure, reset at the barline, ties, fallback noteheads.
No OMR model is run."""
import sys
from pathlib import Path

import numpy as np

OMR = Path(__file__).resolve().parents[2] / "omr"
sys.path.insert(0, str(OMR))

import accidentals as acc  # noqa: E402
import run_engine as re_  # noqa: E402

FLATS3 = -3   # B-flat, E-flat, A-flat


def test_key_signature_applies_without_accidental():
    st = acc.AlterState(FLATS3)
    st.new_measure(0)
    assert st.alter("E", 4) == -1
    assert st.alter("F", 4) == 0
    assert acc.AlterState(2).alter("F", 4) == 1


def test_natural_cancels_key_signature():
    st = acc.AlterState(FLATS3)
    st.new_measure(0)
    assert st.alter("E", 4, "N") == 0          # printed natural: E-natural, not the key's E-flat
    assert st.alter("E", 4) == 0               # carries to the next E4 in the measure
    assert st.alter("E", 5) == -1              # other octave still follows the key


def test_accidental_carries_within_measure_same_octave_only():
    st = acc.AlterState(0)
    st.new_measure(0)
    assert st.alter("F", 4, "#") == 1
    assert st.alter("F", 4) == 1
    assert st.alter("F", 5) == 0
    assert st.alter("G", 4) == 0
    assert st.alter("F", 4, "b") == -1         # a later accidental replaces it
    assert st.alter("F", 4) == -1
    assert st.alter("B", 3, "##") == 2 and st.alter("B", 3) == 2
    assert st.alter("B", 3, "bb") == -2


def test_reset_at_barline():
    st = acc.AlterState(FLATS3)
    st.new_measure(0)
    st.alter("E", 4, "N")
    st.alter("C", 5, "#")
    st.new_measure(1)
    assert st.alter("E", 4) == -1              # back to the key
    assert st.alter("C", 5) == 0
    st.new_measure(1)                          # same measure again: nothing changes
    assert st.alter("E", 4, "N") == 0 and st.alter("E", 4) == 0


def test_tie_across_barline_keeps_accidental():
    st = acc.AlterState(0)
    st.new_measure(0)
    st.alter("F", 4, "#")
    st.alter("C", 5, "#")
    st.new_measure(1, {("F", 4)})
    assert st.alter("F", 4) == 1               # tied-over F-sharp keeps sounding sharp
    assert st.alter("C", 5) == 0               # untied accidental is reset


def _ev(step, octv, alter, key, bars, lift):
    # (pos, gid, step, octave, alter, key, bars, lift) as produced by run_engine._decode_homr_symbols
    return ("upper", 0, step, octv, alter, key, bars, lift)


def test_matched_head_ignores_transformer_alter_when_lift_missing():
    """The transformer printed no lift (it dropped the natural) and reports the key's E-flat; the rule engine uses the
    glyph evidence (explicit natural) and the carry instead."""
    st = acc.AlterState(FLATS3)
    st.new_measure(0)
    ev = _ev("E", 4, -1, FLATS3, 0, None)
    # treble clef, staff_pos 0 = E4
    step, octv, alt, _c, _k = re_._notehead_pitch("G2", 0, ev, FLATS3, state=st, explicit="N")
    assert (step, octv, alt) == ("E", 4, 0)
    step, octv, alt, _c, _k = re_._notehead_pitch("G2", 0, _ev("E", 4, -1, FLATS3, 0, None), FLATS3, state=st)
    assert (step, octv, alt) == ("E", 4, 0)    # carry, although the transformer still says E-flat


def test_fallback_heads_share_state_with_matched_heads():
    st = acc.AlterState(0)
    st.new_measure(0)
    # matched F-sharp (treble pos 1 = F4), then an unmatched F4 head: carries the sharp
    m = re_._notehead_pitch("G2", 1, _ev("F", 4, 1, 0, 0, "#"), 0, state=st, explicit="#")
    assert m[:3] == ("F", 4, 1)
    f = re_._notehead_pitch("G2", 1, None, 0, state=st)
    assert f[:3] == ("F", 4, 1) and f[3] == 0.3
    # unmatched head with an accidental read from the image sets the state for what follows
    st.new_measure(1)
    g = re_._notehead_pitch("G2", 1, None, 0, state=st, explicit="b")
    assert g[:3] == ("F", 4, -1)
    h = re_._notehead_pitch("G2", 1, None, 0, state=st)
    assert h[:3] == ("F", 4, -1)
    st.new_measure(2)                          # barline: reset
    assert re_._notehead_pitch("G2", 1, None, 0, state=st)[:3] == ("F", 4, 0)


def test_fallback_head_uses_key_signature():
    st = acc.AlterState(FLATS3)
    st.new_measure(0)
    assert re_._notehead_pitch("G2", 0, None, FLATS3, state=st)[:3] == ("E", 4, -1)


def test_other_clefs_use_the_same_rule():
    st = acc.AlterState(FLATS3)
    st.new_measure(0)
    # bass clef bottom line G2; staff_pos 5 = E3 (G2 + 5 diatonic steps)
    assert re_._notehead_pitch("F4", 5, None, FLATS3, state=st, explicit="N")[:3] == ("E", 3, 0)
    assert re_._notehead_pitch("F4", 5, None, FLATS3, state=st)[:3] == ("E", 3, 0)


def test_tie_between_detects_arc():
    unit = 20.0
    page = np.zeros((200, 400), np.uint8)
    a, b = (50, 90, 70, 110), (250, 90, 270, 110)
    assert not acc.tie_between(page, a, b, unit)
    page[70:74, 80:240] = 255                  # an arc-like band above the heads between them
    assert acc.tie_between(page, a, b, unit)
