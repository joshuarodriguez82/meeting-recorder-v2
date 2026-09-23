"""
One sentence, heard by both streams, must appear once.

The live transcript runs the microphone and system audio as two
independent streams (core/live_transcriber.py). When the same speech
reaches BOTH, it is transcribed twice and shows up as two people saying
the same words at the same moment:

  * on speakers, the far end plays out loud and the mic picks it up —
    their sentence appears under "Speaker N" and again under "You";
  * on a headset with sidetone / "listen to this device" enabled, the
    user's own voice is routed into system audio — their sentence
    appears under "You" and again under "Speaker N".

Field report (2026-09): both shapes, on both headsets and speakers.

WHICH COPY IS REAL
------------------
The copy that leaked into the other stream is the QUIETER one relative
to that stream's own speech. Absolute levels can't be compared across
streams (mic gain and system volume are unrelated), so each chunk's
level is taken relative to the median level of recent chunks from the
same source. The copy furthest below its own stream's norm is the leak.

When the two are within ``AMBIGUOUS_DB`` of each other, BOTH stay. A
leak loses 15-25 dB on its way into the other stream; two copies at the
same relative level are far more likely two people saying similar things
("can you see my screen?" / "yes, I can see your screen") than a leak,
and hiding real speech on a coin flip is the worse mistake.

WHAT COUNTS AS THE SAME WORDS
----------------------------
Token coverage, not exact equality: the two streams cut utterances at
different places (independent VAD) and Whisper hears a quieter copy a
little differently. A chunk is a duplicate when at least ``COVERAGE`` of
its words appear, in order, in what the other stream said within
``WINDOW_S`` seconds. Short chunks ("yeah", "okay", "right") are never
treated as duplicates: two people really do say those at once.

Pure: no audio, no threads, an injectable clock. The transcriber owns
publishing and retraction; this decides.
"""

from __future__ import annotations

import re
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Callable, Deque, Dict, List, Sequence

#: Seconds (wall clock, arrival order) within which two chunks from
#: different streams can be the same speech. Each stream's chunk waits
#: for its own VAD pause and then for the single worker, so the second
#: copy can trail the first by several seconds.
WINDOW_S = 20.0

#: Fewer words than this and a chunk is never a duplicate — overlapping
#: backchannels are real.
MIN_WORDS = 4

#: Fraction of a chunk's words that must appear, in order, in the other
#: stream's recent text.
COVERAGE = 0.7

#: Relative levels closer than this can't say which copy leaked.
AMBIGUOUS_DB = 3.0

#: Recent chunk levels kept per source for the median.
LEVEL_HISTORY = 30

_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def _coverage(needle: Sequence[str], haystack: Sequence[str]) -> float:
    """Fraction of ``needle`` found, in order, inside ``haystack``."""
    if not needle or not haystack:
        return 0.0
    sm = SequenceMatcher(None, needle, haystack, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / float(len(needle))


@dataclass
class _Chunk:
    source: str
    segment_ids: List[int]
    words: List[str]
    rel_db: float
    at: float


@dataclass
class Decision:
    """``drop``: don't publish the new chunk (it is the leaked copy).
    ``retract``: segment ids already published that are the leaked copy
    of the new chunk and should be withdrawn."""

    drop: bool = False
    retract: List[int] = field(default_factory=list)


class CrossStreamDeduper:
    def __init__(self, *, window_s: float = WINDOW_S,
                 min_words: int = MIN_WORDS, coverage: float = COVERAGE,
                 ambiguous_db: float = AMBIGUOUS_DB,
                 clock: Callable[[], float] = time.monotonic):
        self._window_s = window_s
        self._min_words = min_words
        self._coverage = coverage
        self._ambiguous_db = ambiguous_db
        self._clock = clock
        self._recent: Deque[_Chunk] = deque()
        self._levels: Dict[str, Deque[float]] = {}

    def reset(self) -> None:
        self._recent.clear()
        self._levels.clear()

    def _relative(self, source: str, level_db: float) -> float:
        hist = self._levels.setdefault(source, deque(maxlen=LEVEL_HISTORY))
        hist.append(level_db)
        return level_db - statistics.median(hist)

    def observe(self, source: str, segment_ids: List[int], text: str,
                level_db: float) -> Decision:
        """Record one transcribed chunk and decide what to show.

        ``segment_ids`` are the ids the chunk WOULD publish under; they
        are remembered only if the chunk is kept, so a later, louder
        copy can retract them.
        """
        now = self._clock()
        while self._recent and now - self._recent[0].at > self._window_s:
            self._recent.popleft()

        chunk = _Chunk(source, list(segment_ids), words(text),
                       self._relative(source, level_db), now)
        decision = Decision()

        if len(chunk.words) >= self._min_words:
            others = [c for c in self._recent if c.source != source]
            pooled = [w for c in others for w in c.words]
            if _coverage(chunk.words, pooled) >= self._coverage:
                # The other stream's chunks that hold these words.
                matched = [c for c in others
                           if _coverage(c.words, chunk.words) >= 0.5]
                if matched:
                    loudest = max(c.rel_db for c in matched)
                    quietest = min(c.rel_db for c in matched)
                    if chunk.rel_db > loudest + self._ambiguous_db:
                        # The copy on screen is the leak.
                        for c in matched:
                            decision.retract.extend(c.segment_ids)
                            self._recent.remove(c)
                    elif chunk.rel_db < quietest - self._ambiguous_db:
                        # This copy is the leak.
                        decision.drop = True
                    # Otherwise the levels can't tell — keep both.

        if not decision.drop:
            self._recent.append(chunk)
        return decision
