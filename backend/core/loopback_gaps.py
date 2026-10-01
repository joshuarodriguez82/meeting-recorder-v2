"""
Keeping the system-audio track on the wall clock.

THE FIELD DATA (diagnostics bundle, 2026-10-01)
-----------------------------------------------
Across 55 Windows recordings the system-audio track came out shorter
than the microphone track with ZERO overflows and ZERO drops — 8 s on a
25-minute call, 98 s on 16 minutes, and 7,082 s (83 %) on a 142-minute
call. Nothing was lost; it was never sent. WASAPI loopback delivers no
packets while nothing is playing, and the reader wrote whatever arrived
back to back. Every quiet stretch was therefore cut out of the far-end
track, and everything the other participants said afterwards sat
earlier in the recording than it was said — so the merged audio, the
transcript order and the speaker attribution (which compares the two
tracks moment by moment) were all wrong in proportion to how quiet the
call had been.

THE FIX
-------
The reader knows the wall clock. When a block arrives, the track should
by now hold (elapsed wall time × rate) samples; when it holds less by
more than ``THRESHOLD_S``, the difference was a silent gap and is filled
with silence BEFORE the block is written.

Only ever padded, never trimmed: audio that arrived is never discarded.
The threshold is well above read jitter and buffer bursts, so steady
playback is never touched; it also slowly absorbs the few-ppm drift
between the audio clock and the wall clock, in the direction that
matters.
"""

from __future__ import annotations

from typing import Optional

#: Shortfall below this is jitter, not a gap.
THRESHOLD_S = 0.5


class GapFiller:
    """Decides how much silence to insert before each arriving block."""

    def __init__(self, samplerate: int, threshold_s: float = THRESHOLD_S):
        self.samplerate = int(samplerate)
        self.threshold = int(threshold_s * self.samplerate)
        self._t0: Optional[float] = None
        self._written = 0
        self.padded_samples = 0

    def before(self, block_samples: int, now: float) -> int:
        """Silence (in samples) to write before a block of
        ``block_samples`` that has just arrived at monotonic time
        ``now``. Call once per block, then write the silence and the
        block. The block is taken to END at ``now``."""
        if self._t0 is None:
            # The first block's own duration counts as already elapsed.
            self._t0 = now - block_samples / float(self.samplerate)
        expected = int((now - self._t0) * self.samplerate)
        shortfall = expected - (self._written + block_samples)
        pad = shortfall if shortfall > self.threshold else 0
        self._written += pad + block_samples
        self.padded_samples += pad
        return pad

    @property
    def padded_seconds(self) -> float:
        return self.padded_samples / float(self.samplerate)
