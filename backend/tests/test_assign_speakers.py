"""
Attributing transcript segments to speakers.

WHY THIS FILE DID NOT EXIST UNTIL NOW
-------------------------------------
``DiarizationEngine.assign_speakers`` decides who said every line in
every transcript this app produces, and nothing referenced it anywhere
in the test suite. It also carried a computed-and-never-read ``seg_mid``
local — the tell of a function written once and never revisited.

THE DEFECT (finding 3 of the 2026-09-02 pipeline audit)
-------------------------------------------------------
Whisper emits segments several seconds long. Diarization emits speaker
turns with their own boundaries. The two do not line up, and the old
rule gave each WHOLE segment to whichever turn overlapped it most.

So a segment spanning a hand-off —

    "...so we'll take that away. Actually, hold on, that's not right."

— went entirely to one person, and the other's words were put in their
mouth. In a meeting transcript that is not a cosmetic error: it is the
input to the summary, the action items and the commitments, so the wrong
person gets assigned the follow-up.

The fix needs word timestamps, which nothing requested until
core/decode_options.py started asking for them on the batch pass.

THE RULE
--------
When a segment overlaps two or more turns and carries words, it is SPLIT
at the word boundary where the speaker changes. When it does not carry
words — an older session, a model build that ignored the request — it
falls back to whole-segment max-overlap, i.e. exactly the previous
behaviour. Degrading has to be silent and safe; that is the common case
for every session recorded before today.
"""

from __future__ import annotations

from core.diarization import DiarizationEngine

assign = DiarizationEngine.assign_speakers


def _seg(start, end, text, words=None):
    seg = {"start": start, "end": end, "text": text}
    if words is not None:
        seg["words"] = words
    return seg


def _words(*triples):
    return [{"start": s, "end": e, "word": w} for s, e, w in triples]


def _turn(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker}


# ── The behaviour that already existed, pinned before changing it ───

def test_a_segment_inside_one_turn_goes_to_that_speaker():
    out = assign([_seg(1.0, 3.0, "hello there")],
                 [_turn(0.0, 5.0, "SPEAKER_00")])
    assert [s["speaker_id"] for s in out] == ["SPEAKER_00"]


def test_a_segment_overlapping_no_turn_is_unknown():
    """Silence, music, a diarization pass that found nothing there. It
    must not be attributed to whoever happens to be first."""
    out = assign([_seg(10.0, 11.0, "hmm")], [_turn(0.0, 5.0, "SPEAKER_00")])
    assert out[0]["speaker_id"] == "SPEAKER_UNKNOWN"


def test_no_turns_at_all_leaves_every_segment_unknown():
    out = assign([_seg(0.0, 1.0, "a"), _seg(1.0, 2.0, "b")], [])
    assert [s["speaker_id"] for s in out] == ["SPEAKER_UNKNOWN"] * 2


def test_an_empty_transcript_produces_nothing():
    assert assign([], [_turn(0.0, 5.0, "SPEAKER_00")]) == []


def test_the_original_fields_survive():
    """Callers downstream read start/end/text off these dicts."""
    out = assign([_seg(1.0, 2.0, "keep me")], [_turn(0.0, 5.0, "S0")])
    assert out[0]["start"] == 1.0
    assert out[0]["end"] == 2.0
    assert out[0]["text"] == "keep me"


# ── Fallback: no word timestamps ────────────────────────────────────

def test_without_words_the_majority_speaker_still_wins():
    """Every session recorded before today has no word timestamps, and
    every one of them must keep attributing exactly as it did."""
    seg = _seg(0.0, 10.0, "one long segment")
    turns = [_turn(0.0, 2.0, "SPEAKER_00"), _turn(2.0, 10.0, "SPEAKER_01")]
    out = assign([seg], turns)
    assert len(out) == 1
    assert out[0]["speaker_id"] == "SPEAKER_01"


def test_an_empty_word_list_is_treated_as_no_words():
    seg = _seg(0.0, 10.0, "text", words=[])
    turns = [_turn(0.0, 2.0, "S0"), _turn(2.0, 10.0, "S1")]
    out = assign([seg], turns)
    assert len(out) == 1
    assert out[0]["speaker_id"] == "S1"


# ── The fix: splitting at a hand-off ────────────────────────────────

def test_a_segment_spanning_two_speakers_is_split():
    """The defect, stated directly. Four words, the speaker changes
    after the second."""
    seg = _seg(0.0, 4.0, "yes exactly no wait", words=_words(
        (0.0, 1.0, " yes"), (1.0, 2.0, " exactly"),
        (2.0, 3.0, " no"), (3.0, 4.0, " wait"),
    ))
    turns = [_turn(0.0, 2.0, "SPEAKER_00"), _turn(2.0, 4.0, "SPEAKER_01")]
    out = assign([seg], turns)

    assert len(out) == 2
    assert out[0]["speaker_id"] == "SPEAKER_00"
    assert out[1]["speaker_id"] == "SPEAKER_01"


def test_the_split_puts_each_speakers_words_in_their_own_segment():
    """Splitting into the right COUNT while scrambling the text would be
    worse than not splitting at all."""
    seg = _seg(0.0, 4.0, "yes exactly no wait", words=_words(
        (0.0, 1.0, " yes"), (1.0, 2.0, " exactly"),
        (2.0, 3.0, " no"), (3.0, 4.0, " wait"),
    ))
    turns = [_turn(0.0, 2.0, "S0"), _turn(2.0, 4.0, "S1")]
    out = assign([seg], turns)

    assert out[0]["text"] == "yes exactly"
    assert out[1]["text"] == "no wait"


def test_the_split_pieces_carry_their_own_times():
    seg = _seg(0.0, 4.0, "a b", words=_words((0.0, 1.5, " a"), (2.5, 4.0, " b")))
    turns = [_turn(0.0, 2.0, "S0"), _turn(2.0, 4.0, "S1")]
    out = assign([seg], turns)

    assert out[0]["start"] == 0.0 and out[0]["end"] == 1.5
    assert out[1]["start"] == 2.5 and out[1]["end"] == 4.0


def test_three_speakers_in_one_segment_produce_three_pieces():
    seg = _seg(0.0, 6.0, "a b c", words=_words(
        (0.0, 1.0, " a"), (2.0, 3.0, " b"), (4.0, 5.0, " c")))
    turns = [_turn(0.0, 2.0, "S0"), _turn(2.0, 4.0, "S1"), _turn(4.0, 6.0, "S2")]
    out = assign([seg], turns)
    assert [s["speaker_id"] for s in out] == ["S0", "S1", "S2"]


def test_a_speaker_who_interjects_and_hands_back_keeps_both_turns_apart():
    """A -> B -> A inside one segment. Merging the two A runs would put
    B's words inside A's line."""
    seg = _seg(0.0, 6.0, "mine yours mine", words=_words(
        (0.0, 1.0, " mine"), (2.0, 3.0, " yours"), (4.0, 5.0, " mine")))
    turns = [_turn(0.0, 2.0, "A"), _turn(2.0, 4.0, "B"), _turn(4.0, 6.0, "A")]
    out = assign([seg], turns)

    assert [s["speaker_id"] for s in out] == ["A", "B", "A"]
    assert [s["text"] for s in out] == ["mine", "yours", "mine"]


def test_a_segment_whose_words_are_all_one_speaker_is_not_split():
    """Splitting has to be rare. A segment that does not span a hand-off
    must come out as ONE segment, or every transcript fragments into
    per-word lines."""
    seg = _seg(0.0, 3.0, "all mine here", words=_words(
        (0.0, 1.0, " all"), (1.0, 2.0, " mine"), (2.0, 3.0, " here")))
    out = assign([seg], [_turn(0.0, 5.0, "S0")])
    assert len(out) == 1
    assert out[0]["text"] == "all mine here"


def test_words_outside_every_turn_do_not_vanish():
    """A word in a gap between turns still has to appear in the
    transcript — losing text is worse than mis-attributing it."""
    seg = _seg(0.0, 6.0, "a b c", words=_words(
        (0.0, 1.0, " a"), (2.2, 2.8, " b"), (4.0, 5.0, " c")))
    turns = [_turn(0.0, 2.0, "S0"), _turn(3.5, 6.0, "S1")]
    out = assign([seg], turns)

    joined = " ".join(s["text"] for s in out)
    assert "a" in joined and "b" in joined and "c" in joined


def test_a_word_with_unusable_timings_does_not_abandon_the_segment():
    """Defensive: a malformed word entry must degrade to whole-segment
    attribution, not raise into the processing pipeline."""
    seg = {"start": 0.0, "end": 4.0, "text": "x y",
           "words": [{"start": None, "end": None, "word": " x"},
                     {"start": 3.0, "end": 4.0, "word": " y"}]}
    out = assign([seg], [_turn(0.0, 2.0, "S0"), _turn(2.0, 4.0, "S1")])
    assert out, "a malformed word list dropped the segment entirely"
    assert all(s["text"] for s in out)


def test_the_split_is_stable_for_equal_overlap():
    """A tie must resolve the same way every run, or re-processing one
    session produces a different transcript each time."""
    seg = _seg(0.0, 2.0, "word", words=_words((0.0, 2.0, " word")))
    turns = [_turn(0.0, 1.0, "A"), _turn(1.0, 2.0, "B")]
    first = assign([seg], turns)
    second = assign([seg], turns)
    assert [s["speaker_id"] for s in first] == [s["speaker_id"] for s in second]


def test_multiple_segments_are_each_handled_independently():
    segs = [
        _seg(0.0, 2.0, "one", words=_words((0.0, 2.0, " one"))),
        _seg(2.0, 4.0, "two", words=_words((2.0, 4.0, " two"))),
    ]
    turns = [_turn(0.0, 2.0, "S0"), _turn(2.0, 4.0, "S1")]
    out = assign(segs, turns)
    assert [s["speaker_id"] for s in out] == ["S0", "S1"]
