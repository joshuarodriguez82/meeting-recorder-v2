"""
Why a speaker has no voice fingerprint.

THE DEFECT (field report 2026-09-10)
------------------------------------
Renaming a speaker who had spoken for most of a 93-minute meeting —
**422 transcript segments** — produced:

    Renamed to "You", but no voice profile saved.
    no voice fingerprint available for this speaker — they may have
    spoken too briefly (<1.5s), or the session audio may be missing
    from disk

Neither half of that had been checked. The message was a fixed string
emitted whenever ``speaker.embedding`` was empty, and it offered two
guesses: one provably false for a speaker with 422 segments, and one
that the code was in a position to test with a single ``exists()`` call
and did not.

It is the same defect as the "AI models not loaded. Add API keys"
message that core/model_status.py exists for — a state the code cannot
see, asserted as a diagnosis, sending someone to look in the wrong
place. Here it is worse in one respect: the reader is invited to
conclude their audio is gone.

WHAT THIS MODULE DOES
---------------------
Names the reason that actually applies, and says "not known" when none
does rather than picking the most alarming candidate. Pure, so the
wording is testable without speechbrain, torch, or a WAV on disk —
which is precisely why the string it replaces never had a test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Mirrors core/speaker_embeddings.MIN_TOTAL_SECONDS. Duplicated rather
#: than imported so this module stays free of the speechbrain import
#: chain; test_fingerprint_status.py asserts the two agree.
MIN_TOTAL_SECONDS = 1.5

#: Mirrors core/speaker_embeddings.MIN_TURN_SECONDS — sub-250ms turns
#: are backchannels whose embeddings are unstable, and the extractor
#: drops them before measuring. A caller that measured raw segment
#: duration instead would report "4.0s of speech" for a speaker the
#: extractor saw 0.4s of, and the reason would not add up.
MIN_TURN_SECONDS = 0.25


@dataclass(frozen=True)
class FingerprintInputs:
    """What is knowable about one speaker's missing fingerprint."""

    #: Speech the extractor would actually use: turns of at least
    #: MIN_TURN_SECONDS, summed.
    usable_seconds: float = 0.0
    segment_count: int = 0
    #: False when speechbrain / torch / torchaudio cannot be imported.
    encoder_available: bool = True
    #: The session's recorded audio path, "" when it has none.
    audio_path: str = ""
    #: Whether that path is readable right now.
    audio_exists: bool = True


def describe_missing_fingerprint(inputs: FingerprintInputs) -> str:
    """One sentence naming the reason, checked rather than guessed.

    Order is by what the user can act on, and each branch is a distinct
    remedy:

    1. **No encoder.** Nothing about this session can be fingerprinted
       and no amount of re-recording changes it. Saying anything about
       this speaker's audio would send them to the wrong place.
    2. **No audio path recorded.** The session never had a file — an
       import, or a recording that failed before finalize.
    3. **Audio gone from disk.** The one guess the old message made
       that was worth making; now it is only said when it is true, and
       it names the path so the user can go and look.
    4. **Too little speech.** Reported with the measurement, so a
       speaker who really did only say "mm-hmm" reads as explained
       rather than accused.
    5. **Not known.** Everything checkable passed. Say so — an unknown
       cause dressed up as a known one is what this module exists to
       stop.
    """
    if not inputs.encoder_available:
        return ("voice fingerprinting is not available in this install, so "
                "no speaker on any session can be saved to the known-speakers "
                "list")
    if not inputs.audio_path:
        return ("this session has no audio file recorded, so there is nothing "
                "to take a voice fingerprint from")
    if not inputs.audio_exists:
        return (f"the session audio is no longer on disk "
                f"({inputs.audio_path}), so the voice fingerprint could not "
                f"be computed")
    if inputs.usable_seconds < MIN_TOTAL_SECONDS:
        return (f"this speaker has only {inputs.usable_seconds:.1f}s of usable "
                f"speech and at least {MIN_TOTAL_SECONDS:.1f}s is needed for a "
                f"reliable voice fingerprint")
    return (f"the voice fingerprint could not be computed for this speaker, "
            f"and the reason was not recorded — they have "
            f"{inputs.usable_seconds:.0f}s of speech across "
            f"{inputs.segment_count} segments and the audio is present, so "
            f"this is worth reporting")


def usable_speech_seconds(spans, min_turn_seconds: float = MIN_TURN_SECONDS) -> float:
    """Total speech the extractor would keep, from (start, end) pairs.

    Applies the same sub-turn filter the extractor does, so the number
    in the message is the number the decision was made on.
    """
    total = 0.0
    for start, end in spans:
        length = float(end) - float(start)
        if length >= min_turn_seconds:
            total += length
    return total
