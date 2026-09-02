"""
One definition of what "transcribe" means in this app.

WHY THIS MODULE EXISTS
----------------------
There are two transcription paths and they each built their own decode
arguments inline:

    core/transcription.py:83    (batch, post-stop)
        language="en", vad_filter=True, initial_prompt=prompt

    core/live_transcriber.py:564 (live preview)
        language="en", vad_filter=True

Three defects fell out of that one duplication, and none of them was
visible as a failure:

1. **Language is hardcoded English in both.** There is no setting, no
   per-session field, no auto-detect. Non-English speech is decoded AS
   English — Whisper does not error, it produces fluent, confident,
   wrong text. A meeting held in Italian transcribes into English-shaped
   nonsense and every downstream summary, action item and embedding is
   built on it.

2. **The glossary never reaches the live path.** The batch pass gets
   `initial_prompt` built from TerminologyService, so it prefers the
   user's product and customer vocabulary. The live path does not, so
   the transcript the user WATCHES mis-hears exactly the terms the
   glossary exists to fix, while the one written to disk gets them
   right. Two transcripts of one meeting, disagreeing.

3. **Every hallucination control is at its default**, including
   `condition_on_previous_text=True` — the documented cause of a phrase
   looping across a long silence, which is what a meeting recording is
   full of.

Fixing any one of those in one call site and not the other is how the
duplication keeps paying out. So the options move here, both paths splat
this dict, and a new option is added once.

THE ONE DELIBERATE DIFFERENCE
-----------------------------
Live and batch are not the same job. Live trades accuracy for latency on
a 15-second window; batch has the whole file and should be as accurate
as the model allows. That difference is now stated in one place as an
explicit `live=True` flag rather than being an accident of which
arguments somebody remembered to type.
"""

from __future__ import annotations

import pytest

from core import decode_options as do


# ── Language ────────────────────────────────────────────────────────

def test_auto_means_none_which_is_what_whisper_wants():
    """faster-whisper detects the language when `language` is None.
    Passing the string "auto" would be taken as a language CODE and
    fail, so the translation has to happen here."""
    opts = do.build(language="auto")
    assert opts["language"] is None


def test_an_explicit_language_is_passed_through():
    assert do.build(language="it")["language"] == "it"
    assert do.build(language="es")["language"] == "es"


def test_language_is_normalised():
    """Settings can hold whatever a user typed or an older build wrote."""
    assert do.build(language="  EN  ")["language"] == "en"
    assert do.build(language="")["language"] is None
    assert do.build(language=None)["language"] is None


def test_english_still_works_the_way_it_always_has():
    """Every existing install has English audio and no language setting.
    They must land on exactly the previous behaviour."""
    assert do.build(language="en")["language"] == "en"


def test_detection_threshold_travels_with_auto_detect():
    """Auto-detect with no confidence floor will guess a language from a
    few seconds of silence and then decode the whole meeting as it."""
    opts = do.build(language="auto")
    assert opts["language_detection_threshold"] is not None


def test_no_detection_threshold_when_the_language_is_pinned():
    """Sending a detection knob alongside an explicit language is noise
    at best; some versions reject the combination."""
    assert "language_detection_threshold" not in do.build(language="en")


# ── The glossary prompt ─────────────────────────────────────────────

def test_the_prompt_is_carried_when_there_is_one():
    opts = do.build(language="en", initial_prompt="Acme, Globex, ACD, SIP")
    assert opts["initial_prompt"] == "Acme, Globex, ACD, SIP"


def test_an_empty_prompt_becomes_none_not_an_empty_string():
    """An empty string is a real prompt to the decoder — it conditions
    on nothing and can bias the first segment toward silence. None is
    the "no prompt" value."""
    assert do.build(language="en", initial_prompt="")["initial_prompt"] is None
    assert do.build(language="en", initial_prompt="   ")["initial_prompt"] is None
    assert do.build(language="en")["initial_prompt"] is None


def test_the_live_path_gets_the_prompt_too():
    """The defect this fixes: the live transcript mis-hears the exact
    vocabulary the glossary exists for, while the batch pass gets it
    right."""
    opts = do.build(language="en", initial_prompt="Globex", live=True)
    assert opts["initial_prompt"] == "Globex"


# ── Hallucination controls ──────────────────────────────────────────

def test_conditioning_on_previous_text_is_off():
    """faster-whisper defaults this to True. On meeting audio — long
    silences, crosstalk, dead air while someone shares a screen — it is
    the classic cause of a phrase repeating for a minute. Topic
    continuity across a silence is not worth that here."""
    assert do.build(language="en")["condition_on_previous_text"] is False


def test_vad_is_on_in_both_modes():
    """Both call sites already passed vad_filter=True; a silent change
    here would alter every transcript in the app."""
    assert do.build(language="en")["vad_filter"] is True
    assert do.build(language="en", live=True)["vad_filter"] is True


def test_vad_parameters_are_stated_rather_than_defaulted():
    opts = do.build(language="en")
    assert "min_silence_duration_ms" in opts["vad_parameters"]


# ── Word timestamps: batch only ─────────────────────────────────────

def test_batch_asks_for_word_timestamps():
    """Speaker attribution needs them to split a segment at a hand-off
    (see core/diarization.assign_speakers), and the hallucination
    filter below needs them too."""
    assert do.build(language="en")["word_timestamps"] is True


def test_live_does_not_ask_for_word_timestamps():
    """They cost decode time the live path spends on latency, and
    nothing in the live view consumes them."""
    assert do.build(language="en", live=True)["word_timestamps"] is False


def test_the_hallucination_filter_only_runs_where_words_exist():
    """`hallucination_silence_threshold` requires word timestamps. Set
    without them, faster-whisper raises."""
    batch = do.build(language="en")
    live = do.build(language="en", live=True)
    assert batch["hallucination_silence_threshold"] is not None
    assert "hallucination_silence_threshold" not in live


# ── The batch/live split ────────────────────────────────────────────

def test_live_uses_a_cheaper_beam_than_batch():
    """The one deliberate difference between the two paths, stated
    instead of being an accident of which arguments got typed."""
    live = do.build(language="en", live=True)["beam_size"]
    batch = do.build(language="en")["beam_size"]
    assert live < batch


def test_every_option_is_a_real_transcribe_parameter():
    """A typo in a key here is silent — faster-whisper would raise a
    TypeError on a real recording and nowhere else, so the suite would
    stay green while transcription was broken for everyone.

    Skips when faster_whisper is the lightweight stub tests/_app_import
    installs (the normal case for this suite, and for CI's backend job).
    A skip SAYS it skipped; that is the honest failure mode, unlike a
    hand-copied list of parameter names that could drift from the
    library without anything noticing.
    """
    import inspect
    try:
        from faster_whisper import WhisperModel
        params = inspect.signature(WhisperModel.transcribe).parameters
    except (ImportError, TypeError, ValueError):
        pytest.skip("faster_whisper is stubbed in this environment")
    if not params or "audio" not in params:
        pytest.skip("faster_whisper is stubbed in this environment")

    accepted = set(params)
    for live in (False, True):
        for key in do.build(language="auto", live=live):
            assert key in accepted, f"{key} is not a transcribe() parameter"


def test_the_option_set_is_pinned():
    """The whole point is that both paths send the SAME thing. Pinning
    the key set means adding an option to one mode without deciding
    about the other fails here rather than in the field."""
    assert set(do.build(language="auto")) == {
        "language", "language_detection_threshold", "initial_prompt",
        "vad_filter", "vad_parameters", "condition_on_previous_text",
        "word_timestamps", "hallucination_silence_threshold", "beam_size",
    }
    assert set(do.build(language="en", live=True)) == {
        "language", "initial_prompt", "vad_filter", "vad_parameters",
        "condition_on_previous_text", "word_timestamps", "beam_size",
    }
