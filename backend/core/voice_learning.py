"""
Learning voices from an imported meeting's transcript.

WHY
---
A Teams or Zoom transcript names every speaker from the meeting roster.
Imported together with the recording, that is a labelled sample of each
person's voice — exactly what a rename in the Speakers tab produces one
at a time. Saving those voices as known speakers means the next call
with the same people names them live, with no Speaker 1 / Speaker 2.

THE RISK, AND THE CHECKS
------------------------
A transcript's clock is not guaranteed to be the recording's clock:
transcription can start before or after the recording. Offset by a few
seconds, every line labelled "Jane" holds a mix of whoever was talking
then, and saving that as Jane's voice would make the app confidently
name the wrong person on every future call. A wrong saved voice is worse
than none.

So two independent checks must both pass before anything is saved.

1. The timing. A few of the transcript's lines are transcribed again
   from the recording at the transcript's own timestamps, and the words
   heard are compared with the words the transcript says were said
   there. An offset of even a second or two shows up as different
   words. This is the check that catches a clean swap: in a two-person
   back-and-forth, a transcript one line out labels every one of Jane's
   lines with Rob's name and vice versa — each label is then perfectly
   consistent, just wrong (seen with the real voice model, 2026-10-03),
   so check 2 alone would save Rob's voice as Jane.
2. The voices. Each speaker's own lines are checked against everyone's
   voice: every line's embedding should sit closer to its own speaker's
   average than to anyone else's. This catches a label that isn't one
   person — two people sharing a room account, a roster name for a
   dial-in. The meeting is learned from only when that agreement is
   high overall, and a speaker only when their own lines agree. It
   needs two named speakers — with one, there is no one to be confused
   with.

Pure — numbers in, decision out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Dict, List, Sequence, Tuple

import numpy as np

MIN_NAMED_SPEAKERS = 2
#: Lines a speaker needs before their voice is worth saving.
MIN_TURNS = 3
#: Share of lines that must sit closest to their own speaker.
MIN_AGREEMENT = 0.7

#: Labels that mean "the transcript didn't say who" — never saved.
UNNAMED = {"unknown speaker", "speaker 1"}


@dataclass
class AlignmentCheck:
    ok: bool
    agreement: float
    reason: str = ""
    #: speaker -> L2-normalised average voice, for the speakers to learn.
    learnable: Dict[str, np.ndarray] = field(default_factory=dict)
    #: speaker -> their own lines' agreement, for every speaker checked.
    per_speaker: Dict[str, float] = field(default_factory=dict)


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-8 else v


def check_alignment(
        turn_embeddings: Dict[str, Sequence[np.ndarray]]) -> AlignmentCheck:
    """Decide whether a transcript's speaker labels match the voices in
    the audio. ``turn_embeddings`` maps each named speaker to one voice
    embedding per line they said."""
    usable = {s: [_unit(np.asarray(e, dtype=np.float32)) for e in embs]
              for s, embs in turn_embeddings.items()
              if len(embs) >= MIN_TURNS}
    if len(usable) < MIN_NAMED_SPEAKERS:
        return AlignmentCheck(
            False, 0.0,
            "needs at least two named speakers with a few lines each")

    sums = {s: np.sum(np.stack(e), axis=0) for s, e in usable.items()}
    counts = {s: len(e) for s, e in usable.items()}
    correct_total = lines_total = 0
    per_speaker: Dict[str, float] = {}
    for s, embs in usable.items():
        right = 0
        for e in embs:
            # Leave this line out of its own speaker's average, so a
            # speaker with few lines isn't judged against itself.
            best, best_sim = None, -2.0
            for other, total in sums.items():
                if other == s:
                    centre = (total - e) / max(1, counts[other] - 1)
                else:
                    centre = total / counts[other]
                sim = float(np.dot(e, _unit(centre)))
                if sim > best_sim:
                    best, best_sim = other, sim
            right += best == s
        per_speaker[s] = right / len(embs)
        correct_total += right
        lines_total += len(embs)

    agreement = correct_total / lines_total
    if agreement < MIN_AGREEMENT:
        return AlignmentCheck(
            False, agreement,
            "the transcript's timing doesn't line up with the voices in "
            "the recording", per_speaker=per_speaker)
    learnable = {s: _unit(sums[s] / counts[s]).astype(np.float32)
                 for s, a in per_speaker.items() if a >= MIN_AGREEMENT}
    return AlignmentCheck(True, agreement, learnable=learnable,
                          per_speaker=per_speaker)


def named_speakers(speakers: Dict[str, str]) -> List[str]:
    """Speaker ids whose display name is a real name from the
    transcript, not a placeholder."""
    return [sid for sid, name in speakers.items()
            if (name or "").strip()
            and (name or "").strip().lower() not in UNNAMED
            and (name or "").strip() != sid]


# ── check 1: does the transcript's timing match the recording? ───────

SPOT_CHECKS = 8
#: Lines worth checking: long enough to transcribe reliably.
SPOT_MIN_SECONDS = 2.0
SPOT_MIN_WORDS = 4
#: Slack either side of a line, so a small offset (which harms nothing)
#: doesn't fail the check.
SPOT_PAD_SECONDS = 0.75
#: Share of a line's words that must be heard in its span. The two
#: transcribers disagree on details, never on most of the words.
MIN_WORD_RECALL = 0.6
MIN_SPOTS = 3

_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) >= 3}


def word_recall(expected: str, heard: str) -> float:
    """Share of the transcript line's words that were heard."""
    want = _words(expected)
    if not want:
        return 0.0
    return len(want & _words(heard)) / len(want)


def pick_spot_checks(
        lines: Sequence[Tuple[float, float, str]],
        n: int = SPOT_CHECKS) -> List[Tuple[float, float, str]]:
    """Up to ``n`` (start, end, text) lines spread across the meeting,
    each long enough to transcribe on its own."""
    good = [ln for ln in lines
            if ln[1] - ln[0] >= SPOT_MIN_SECONDS
            and len(_words(ln[2])) >= SPOT_MIN_WORDS]
    if len(good) <= n:
        return list(good)
    step = len(good) / n
    return [good[int(i * step)] for i in range(n)]


def timing_matches(recalls: Sequence[float]) -> Tuple[bool, float, str]:
    """(ok, median recall, reason) from the spot checks."""
    if len(recalls) < MIN_SPOTS:
        return False, 0.0, "too few clear lines to check the timing"
    m = float(median(recalls))
    if m < MIN_WORD_RECALL:
        return False, m, ("the transcript's timing doesn't line up with "
                          "the recording")
    return True, m, ""
