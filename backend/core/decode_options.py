"""
One definition of what "transcribe" means in this app.

WHY THIS EXISTS
---------------
Two transcription paths, each building its own decode arguments inline:

    core/transcription.py     language="en", vad_filter=True, initial_prompt=…
    core/live_transcriber.py  language="en", vad_filter=True

Three defects fell out of that duplication, and not one of them showed
up as a failure:

1. **Language was hardcoded to English in both.** No setting, no
   per-session field, no detection. Non-English speech was decoded AS
   English — Whisper does not error on that, it produces fluent,
   confident, wrong text, and every summary, action item and embedding
   downstream is built on it. A meeting held in Italian came out as
   English-shaped nonsense with a green checkmark over it.

2. **The glossary never reached the live path.** The batch pass got an
   ``initial_prompt`` built from TerminologyService so it preferred the
   user's product and customer vocabulary; the live path did not. The
   transcript the user WATCHES mis-heard the exact terms the glossary
   exists to correct, while the one written to disk got them right.

3. **Every hallucination control sat at its default**, including
   ``condition_on_previous_text=True`` — the documented cause of a
   phrase looping across a long silence, and a meeting recording is
   mostly long silences.

Fixing one call site and not the other is how a duplication like that
keeps paying out. Both paths now splat this dict, so an option is added
once and a test pins the key set.

THE ONE DELIBERATE DIFFERENCE
-----------------------------
Live and batch are not the same job. Live has 15 seconds of audio and a
latency budget; batch has the whole file and should be as accurate as
the model allows. That difference is stated here as ``live=True``
rather than being an accident of which arguments somebody typed.

EVERY KEY BELOW WAS CHECKED against faster-whisper 1.2.1's
``transcribe()`` signature (the pinned version, read from the wheel).
``test_every_option_is_a_real_transcribe_parameter`` re-checks it
wherever the real library is installed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: The value the UI and config.env use for "let Whisper decide". Not a
#: language code — it must be translated to None before it reaches the
#: decoder, or faster-whisper treats "auto" as a language and fails.
AUTO = "auto"

#: Minimum confidence before a detected language is accepted. Without a
#: floor, detection will happily infer a language from a few seconds of
#: throat-clearing and then decode the entire meeting as it. On a miss,
#: faster-whisper falls back to English, which is the right default for
#: this user base.
LANGUAGE_DETECTION_THRESHOLD = 0.6

#: Silence shorter than this does not split a segment. The default
#: (2000ms) is tuned for continuous speech; meetings pause constantly
#: and the default leaves long dead-air tails glued onto segments, which
#: is where hallucinated text appears.
MIN_SILENCE_DURATION_MS = 700

#: A run of silence longer than this, inside what the model thinks is
#: speech, marks the surrounding words as hallucinated and drops them.
#: Requires word timestamps, so it is batch-only.
HALLUCINATION_SILENCE_THRESHOLD_S = 2.0

#: Beam width. Batch gets the model's default-quality search; live
#: takes the cheap greedy-ish path because a 15-second window has to
#: come back before the next one arrives.
BATCH_BEAM_SIZE = 5
LIVE_BEAM_SIZE = 1


def normalize_language(language: Optional[str]) -> Optional[str]:
    """A language code faster-whisper accepts, or None for detect.

    Blank, missing and the sentinel ``"auto"`` all mean detect. Anything
    else is lower-cased and trimmed — config.env holds whatever an older
    build or a hand edit put there.
    """
    value = (language or "").strip().lower()
    if not value or value == AUTO:
        return None
    return value


def build(
    *,
    language: Optional[str],
    initial_prompt: str = "",
    live: bool = False,
) -> Dict[str, Any]:
    """Decode options for one ``model.transcribe()`` call.

    Splat into the call: ``model.transcribe(audio, **build(...))``.

    ``initial_prompt`` is the glossary bias from TerminologyService. An
    empty one becomes None rather than an empty string: an empty string
    is a real prompt to the decoder — it conditions on nothing and can
    push the first segment toward silence — whereas None means "no
    prompt at all", which is what the caller meant.
    """
    resolved = normalize_language(language)
    prompt = (initial_prompt or "").strip() or None

    opts: Dict[str, Any] = {
        "language": resolved,
        "initial_prompt": prompt,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": MIN_SILENCE_DURATION_MS},
        # Off deliberately. See the module docstring — this default is
        # what makes a phrase repeat for a minute over a shared screen.
        "condition_on_previous_text": False,
        "word_timestamps": not live,
        "beam_size": LIVE_BEAM_SIZE if live else BATCH_BEAM_SIZE,
    }

    if resolved is None:
        # Only meaningful when detecting; some versions reject it
        # alongside an explicit language.
        opts["language_detection_threshold"] = LANGUAGE_DETECTION_THRESHOLD

    if not live:
        # Requires word timestamps, which live does not request.
        opts["hallucination_silence_threshold"] = \
            HALLUCINATION_SILENCE_THRESHOLD_S

    return opts
