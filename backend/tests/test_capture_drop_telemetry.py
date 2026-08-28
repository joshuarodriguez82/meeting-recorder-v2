"""Audio dropped by a full loopback queue must be counted, not silent.

`_loopback_q_putter` drops the oldest block when the queue is full so a
slow writer can never block the audio callback — that policy is right.
What was wrong is what happened when the drop-and-retry ITSELF failed:
a bare `except: pass`, so the block vanished with no record anywhere.

That is audio loss, and it is the exact shape of this repo's recurring
defect: a result you couldn't read rendering as a result that isn't
there. The stop-time sync-integrity report already publishes
`mic_overflows` / `loopback_overflows` from the same object; dropped
blocks belong in the same channel, where recording_service already logs
and stores it.

Pinned here:
  - the counter starts at zero and appears in get_capture_stats();
  - a drop increments it rather than disappearing;
  - the drop path still never raises into the audio callback, because
    raising there is worse than dropping.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace


def _load_capture_module():
    """Import core.audio_capture with its hardware deps stubbed.

    The module imports sounddevice/numpy at module scope; CI installs
    numpy but not sounddevice, and we only exercise pure queue logic.
    """
    if "sounddevice" not in sys.modules:
        sd = types.ModuleType("sounddevice")
        sd.query_devices = lambda *a, **k: []
        sd.query_hostapis = lambda *a, **k: []
        sd.default = SimpleNamespace(device=(None, None))
        sys.modules["sounddevice"] = sd
    import core.audio_capture as ac
    return ac


class _FullQueue:
    """Always full; get_nowait also fails, so the retry path is taken
    and then fails too — the exact case that used to vanish."""

    def put_nowait(self, item):
        raise Exception("queue full")

    def get_nowait(self):
        raise Exception("nothing to pop")


class _FullThenOK:
    """Full once, then the drop-oldest retry succeeds — the normal
    overflow path, which is NOT a lost block and must not be counted."""

    def __init__(self):
        self.puts = 0
        self.popped = False

    def put_nowait(self, item):
        self.puts += 1
        if self.puts == 1:
            raise Exception("queue full")

    def get_nowait(self):
        self.popped = True
        return object()


def _capture_with_queue(ac, queue):
    cap = ac.AudioCapture.__new__(ac.AudioCapture)
    cap._loopback_queue = queue
    cap._loopback_drops = 0
    return cap


def test_drop_counter_is_reported_in_capture_stats():
    ac = _load_capture_module()
    cap = ac.AudioCapture.__new__(ac.AudioCapture)
    cap._mic_samples = cap._loopback_samples = 0
    cap._mic_overflows = cap._loopback_overflows = 0
    cap._loopback_drops = 0
    cap._loopback_sr = 48000
    cap.actual_sr = 48000
    cap.mic_start_monotonic = cap.loopback_start_monotonic = None
    stats = cap.get_capture_stats()
    assert "loopback_drops" in stats
    assert stats["loopback_drops"] == 0


def test_a_lost_block_increments_the_counter():
    ac = _load_capture_module()
    cap = _capture_with_queue(ac, _FullQueue())
    cap._loopback_q_putter(b"block")
    assert cap._loopback_drops == 1
    cap._loopback_q_putter(b"block")
    assert cap._loopback_drops == 2


def test_the_drop_path_never_raises_into_the_audio_callback():
    """Raising here would kill the capture stream mid-meeting — far
    worse than losing one block."""
    ac = _load_capture_module()
    cap = _capture_with_queue(ac, _FullQueue())
    cap._loopback_q_putter(b"block")  # must not raise


def test_successful_drop_oldest_retry_is_not_counted_as_a_loss():
    """Queue full, oldest popped, block accepted: the newest audio was
    kept. That is the policy working, not data loss."""
    ac = _load_capture_module()
    q = _FullThenOK()
    cap = _capture_with_queue(ac, q)
    cap._loopback_q_putter(b"block")
    assert q.popped is True
    assert cap._loopback_drops == 0
