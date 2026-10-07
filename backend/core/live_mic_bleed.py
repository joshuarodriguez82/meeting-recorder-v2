"""
Live transcript: stop crediting the far end's words to "You" once the
mic is shown to be hearing the call.

WHY
---
The live transcript labels everything the microphone hears "You". On
laptop speakers the mic also hears the far end, and core/live_dedup is
meant to drop that copy: of two chunks with the same words it drops the
one quieter relative to its own stream's norm. When the user is quiet
or muted (muting the call does not mute the device, and the recorder
reads the device), the mic's norm IS the leaked far end, so both copies
sit at their stream's norm, the levels can't tell, and both stay. Field
log 2026-10-07: an 18-minute call where the user barely spoke put
766 s of mic speech under "You", and the deduper dropped 4 chunks.

The signal that does tell is timing. On a headset the user mostly
speaks while the far end is quiet; mic speech landing on top of far-end
playback is double-talk, a small share. On speakers most mic "speech"
happens while the far end is playing, because it is the far end. That
share is what core/channel_attribution calls the contested fraction:
0.611 and 0.797 on the two field calls with this failure, 0.000 on the
same machine's other calls that week.

WHAT THIS DOES
--------------
Keeps a short energy timeline of the loopback (system audio). For each
mic chunk it measures how much of the chunk's span the far end was
playing. Over the recent mic speech, once more than HEARS_CALL_FRACTION
of it overlapped playback (and there is at least MIN_MIC_SECONDS of
it), the mic is hearing the call, and from then on a mic chunk that is
itself mostly on top of playback is dropped before transcription: the
loopback stream transcribes those words already. Mic speech while the
far end is silent still shows as "You".

Live preview only. The transcript written after the call is decided by
core/channel_attribution on the full recording.

Pure: no audio devices, no threads of its own; thread-safe because the
capture thread feeds the loopback and the worker asks about mic chunks.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Tuple

import numpy as np

#: Loopback energy is kept per frame of this length.
FRAME_S = 0.1

#: Seconds of loopback timeline kept.
HISTORY_S = 180.0

#: A frame is playback when it is this far above the loopback's quiet
#: floor (its 10th-percentile level) and above ABSOLUTE_FLOOR_DB.
ACTIVE_ABOVE_FLOOR_DB = 15.0
ABSOLUTE_FLOOR_DB = -55.0

#: Mic speech judged over this many recent seconds of it.
WINDOW_MIC_S = 120.0

#: No verdict before this much mic speech: a few chunks prove nothing.
MIN_MIC_SECONDS = 30.0

#: Share of recent mic speech on top of playback that means the mic is
#: hearing the call. Same bar as channel_attribution's stand-down.
HEARS_CALL_FRACTION = 0.5

#: Once the mic hears the call, a chunk at least this much on top of
#: playback is the far end's.
DROP_CHUNK_OVERLAP = 0.5


@dataclass
class MicVerdict:
    drop: bool
    overlap: float          # this chunk's share on top of playback
    hears_call: bool        # the meeting-level verdict after this chunk


class MicBleedGate:
    def __init__(self, samplerate: int):
        self._sr = int(samplerate)
        self._frame = max(1, int(self._sr * FRAME_S))
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._lb_samples = 0          # loopback samples seen so far
            self._carry = np.zeros(0, dtype=np.float32)
            self._frames: Deque[Tuple[float, float]] = deque(
                maxlen=int(HISTORY_S / FRAME_S))
            self._mic: Deque[Tuple[float, float]] = deque()  # (secs, on-top secs)
            self._mic_total = 0.0
            self._mic_on_top = 0.0
            self._hears_call = False

    @property
    def hears_call(self) -> bool:
        return self._hears_call

    def push_loopback(self, chunk: np.ndarray) -> None:
        """Feed system audio, in arrival order, at the gate's rate."""
        block = np.asarray(chunk, dtype=np.float32)
        if block.ndim > 1:
            block = block.mean(axis=1)
        with self._lock:
            data = (np.concatenate([self._carry, block])
                    if len(self._carry) else block)
            n_frames = len(data) // self._frame
            t0 = (self._lb_samples - len(self._carry)) / self._sr
            for i in range(n_frames):
                f = data[i * self._frame:(i + 1) * self._frame]
                ms = float(np.mean(f.astype(np.float64) ** 2))
                db = 10.0 * np.log10(ms + 1e-12)
                self._frames.append((t0 + i * FRAME_S, db))
            self._carry = data[n_frames * self._frame:].copy()
            self._lb_samples += len(block)

    def _playing(self, start_s: float, end_s: float) -> float:
        """Share of [start_s, end_s) the far end was playing; 0.0 when
        the loopback timeline doesn't cover it."""
        if not self._frames or end_s <= start_s:
            return 0.0
        levels = np.fromiter((db for _, db in self._frames), dtype=np.float64)
        floor = float(np.percentile(levels, 10))
        bar = max(floor + ACTIVE_ABOVE_FLOOR_DB, ABSOLUTE_FLOOR_DB)
        covered = active = 0
        for t, db in self._frames:
            if start_s <= t < end_s:
                covered += 1
                active += db >= bar
        if covered == 0:
            return 0.0
        return active / covered

    def mic_chunk(self, start_s: float, end_s: float) -> MicVerdict:
        """Judge one mic chunk spanning [start_s, end_s) on the
        recording's clock."""
        dur = max(0.0, float(end_s) - float(start_s))
        with self._lock:
            overlap = self._playing(start_s, end_s)
            self._mic.append((dur, dur * overlap))
            self._mic_total += dur
            self._mic_on_top += dur * overlap
            while self._mic and self._mic_total - self._mic[0][0] >= WINDOW_MIC_S:
                d, o = self._mic.popleft()
                self._mic_total -= d
                self._mic_on_top -= o
            self._hears_call = bool(
                self._mic_total >= MIN_MIC_SECONDS
                and self._mic_on_top > HEARS_CALL_FRACTION * self._mic_total)
            drop = bool(self._hears_call and overlap >= DROP_CHUNK_OVERLAP)
            return MicVerdict(drop=drop, overlap=float(overlap),
                              hears_call=self._hears_call)
