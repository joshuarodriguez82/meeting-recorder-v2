"""
Saying WHY a voice fingerprint is missing, instead of guessing twice.

THE FIELD REPORT (2026-09-10)
-----------------------------
Renaming the speaker who had done most of the talking in a 93-minute
meeting — 422 transcript segments — produced::

    Renamed to "You", but no voice profile saved.
    no voice fingerprint available for this speaker — they may have
    spoken too briefly (<1.5s), or the session audio may be missing
    from disk

Neither half had been checked. It was a fixed string emitted whenever
``speaker.embedding`` was empty. One of its two guesses is provably
false for a speaker with 422 segments; the other was one ``exists()``
call away from being an answer.

That is the "AI models not loaded. Add API keys" defect again — a state
the code cannot see, asserted as a diagnosis — with the added insult
that the reader is invited to conclude their recording is gone.
"""

from __future__ import annotations

import pytest

from core.fingerprint_status import (
    MIN_TOTAL_SECONDS,
    MIN_TURN_SECONDS,
    FingerprintInputs,
    describe_missing_fingerprint,
    usable_speech_seconds,
)


def _inputs(**kw) -> FingerprintInputs:
    """A speaker who talked for most of the meeting and whose audio is
    right there — the reported case."""
    base = dict(usable_seconds=1800.0, segment_count=422,
                encoder_available=True, audio_path="C:\\\\Users\\\\<you>\\\\s.wav",
                audio_exists=True)
    base.update(kw)
    return FingerprintInputs(**base)


# ── The reported defect ─────────────────────────────────────────────

def test_a_speaker_with_422_segments_is_not_told_they_spoke_too_briefly():
    """The exact sentence from the field report, and the reason it was
    worth fixing: it is false, and it is the first thing the reader
    tries to make sense of."""
    msg = describe_missing_fingerprint(_inputs())
    assert "too briefly" not in msg
    assert "1.5s is needed" not in msg


def test_present_audio_is_never_reported_as_missing():
    """"your audio may be gone" is the most alarming thing this message
    can say, and it was said unconditionally."""
    msg = describe_missing_fingerprint(_inputs())
    assert "no longer on disk" not in msg


def test_an_unexplained_failure_says_so_and_shows_its_working():
    """Everything checkable passed. An unknown cause dressed up as a
    known one is the whole defect — say "not recorded", and give the
    numbers that make it reportable."""
    msg = describe_missing_fingerprint(_inputs())
    assert "not recorded" in msg
    assert "422" in msg


# ── Each real cause, named ──────────────────────────────────────────

def test_a_missing_encoder_is_reported_as_an_install_problem():
    """Nothing on any session can be fingerprinted and no amount of
    re-recording changes it. Talking about this speaker's audio would
    send them to entirely the wrong place."""
    msg = describe_missing_fingerprint(_inputs(encoder_available=False))
    assert "not available in this install" in msg
    assert "disk" not in msg


def test_a_session_with_no_audio_file_says_that():
    msg = describe_missing_fingerprint(_inputs(audio_path=""))
    assert "no audio file recorded" in msg


def test_audio_gone_from_disk_names_the_path():
    """The one guess the old message made that was worth making — now
    said only when true, and with the path so the user can go look."""
    msg = describe_missing_fingerprint(
        _inputs(audio_path="~/Recordings/gone.wav", audio_exists=False))
    assert "no longer on disk" in msg
    assert "~/Recordings/gone.wav" in msg


def test_a_genuinely_brief_speaker_is_told_how_brief():
    """Reported with the measurement, so someone who really did only say
    "mm-hmm" reads as explained rather than accused."""
    msg = describe_missing_fingerprint(
        _inputs(usable_seconds=0.4, segment_count=1))
    assert "0.4s" in msg
    assert "1.5s" in msg


# ── Ordering: the most actionable cause wins ────────────────────────

def test_a_missing_encoder_outranks_everything_else():
    """With no encoder, the audio and the speech length are irrelevant —
    reporting either would have the user chase a non-problem."""
    msg = describe_missing_fingerprint(_inputs(
        encoder_available=False, audio_exists=False, usable_seconds=0.1))
    assert "not available in this install" in msg


def test_missing_audio_outranks_too_little_speech():
    """Speech length is measured from segments; with the file gone,
    nothing could have been measured from it anyway."""
    msg = describe_missing_fingerprint(
        _inputs(audio_exists=False, usable_seconds=0.1))
    assert "no longer on disk" in msg


# ── The measurement matches the one the extractor makes ─────────────

def test_backchannels_do_not_count_toward_usable_speech():
    """core/speaker_embeddings drops turns under 250ms before deciding.
    Measuring raw duration here would report "4.0s of speech" for a
    speaker the extractor saw 0.4s of, and the reason would not add
    up."""
    spans = [(0.0, 0.1)] * 40 + [(10.0, 10.4)]
    assert usable_speech_seconds(spans) == pytest.approx(0.4)


def test_usable_speech_of_nothing_is_zero():
    assert usable_speech_seconds([]) == 0.0


def test_the_thresholds_match_the_extractor():
    """These are duplicated to keep this module clear of the speechbrain
    import chain. A drift would have the message quote a cutoff the
    extractor is not using."""
    from core import speaker_embeddings
    assert MIN_TOTAL_SECONDS == speaker_embeddings.MIN_TOTAL_SECONDS
    assert MIN_TURN_SECONDS == speaker_embeddings.MIN_TURN_SECONDS
