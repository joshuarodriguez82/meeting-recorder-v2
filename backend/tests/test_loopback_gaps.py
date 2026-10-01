"""
The far-end track must stay on the wall clock through silence.

Diagnostics bundle 2026-10-01: across 55 Windows recordings the
system-audio track was shorter than the mic with zero overflows and zero
drops — 8 s on 25 min, 98 s on 16 min, 7,082 s (83 %) on a 142-minute
call. WASAPI loopback sends nothing while nothing plays and the reader
wrote whatever arrived back to back, so every quiet stretch was cut out
and the rest of the far end slid earlier than it was said.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
import soundfile as sf

from core.loopback_gaps import THRESHOLD_S, GapFiller

SR = 48000
BLOCK = 1024


def _steady(filler, seconds, start=0.0):
    """Blocks arriving exactly in real time; returns total padding."""
    t, pad = start, 0
    for _ in range(int(seconds * SR / BLOCK)):
        t += BLOCK / SR
        pad += filler.before(BLOCK, t)
    return pad, t


def test_steady_playback_is_never_padded():
    f = GapFiller(SR)
    pad, _ = _steady(f, 600)
    assert pad == 0


def test_a_silent_gap_is_filled_with_its_own_length():
    """Ten seconds of nothing playing must become ten seconds of
    silence, so what follows lands where it was said."""
    f = GapFiller(SR)
    _, t = _steady(f, 5)
    pad = f.before(BLOCK, t + 10.0 + BLOCK / SR)
    assert pad == pytest.approx(10.0 * SR, abs=2)


def test_the_track_ends_as_long_as_the_wall_clock():
    """The field symptom, end to end: a 142-minute call with long quiet
    stretches must produce a 142-minute far-end track."""
    f = GapFiller(SR)
    t, written = 0.0, 0
    for _ in range(20):
        p, t = _steady(f, 60, t)         # a minute of talk
        written += int(60 * SR / BLOCK) * BLOCK + p
        t += 360.0                       # six minutes of nothing
    p = f.before(BLOCK, t + BLOCK / SR)
    written += p + BLOCK
    assert written / SR == pytest.approx(t + BLOCK / SR, abs=0.1)


def test_jitter_and_bursts_below_the_threshold_are_left_alone():
    """A late read followed by a burst is normal buffering, not a gap."""
    f = GapFiller(SR)
    _, t = _steady(f, 5)
    late = t + (THRESHOLD_S * 0.8)
    assert f.before(BLOCK, late) == 0


def test_never_trims_audio_that_arrived_early():
    """A burst ahead of the wall clock is audio we have — keep it."""
    f = GapFiller(SR)
    t = 0.0
    for _ in range(500):            # all at once, no time passing
        assert f.before(BLOCK, t) == 0


def test_padding_is_reported():
    f = GapFiller(SR)
    _, t = _steady(f, 1)
    f.before(BLOCK, t + 3.0 + BLOCK / SR)
    assert f.padded_seconds == pytest.approx(3.0, abs=0.01)


# ── Through the real writer ─────────────────────────────────────────

from tests._app_import import _stub_optional_modules  # noqa: E402

_stub_optional_modules()

from core import audio_capture as ac  # noqa: E402


def test_the_queue_writer_writes_a_gap_as_silence(tmp_path):
    """The native macOS path sends a gap as a sample COUNT (no giant
    array on Apple's queue); the writer turns it into silence."""
    wav = tmp_path / "lb.wav"
    cap = ac.AudioCapture(mic_device_index=None, output_device_index=None,
                          on_chunk=lambda c: None,
                          loopback_wav_path=str(wav))
    cap._loopback_sr = SR
    cap._running = True
    cap._loopback_q_putter(np.ones(100, dtype=np.float32))
    cap._loopback_q_putter(SR * 2)
    cap._loopback_q_putter(np.ones(100, dtype=np.float32))
    cap._loopback_q_putter(None)
    cap._loopback_writer_sd()
    data, sr = sf.read(str(wav), dtype="float32")
    assert len(data) == 100 + SR * 2 + 100
    assert data[100:100 + SR * 2].max() == 0.0
    assert data[-1] == 1.0


class _QuietThenTalking:
    """A WASAPI loopback stream that delivers a block, then nothing for
    a while (nothing playing), then blocks again."""

    def __init__(self, capture, gap_s):
        self.capture, self.gap_s, self.reads = capture, gap_s, 0

    def is_active(self):
        return True

    def read(self, n, exception_on_overflow=False):
        self.reads += 1
        if self.reads == 2:
            time.sleep(self.gap_s)
        if self.reads > 3:
            self.capture._running = False
        return np.full(n, 0.5, dtype=np.float32).tobytes()


def test_the_windows_reader_fills_the_silence_it_was_not_sent(tmp_path):
    wav = tmp_path / "lb.wav"
    cap = ac.AudioCapture(mic_device_index=None, output_device_index=None,
                          on_chunk=lambda c: None,
                          loopback_wav_path=str(wav))
    cap._loopback_sr = 16000
    cap._running = True
    cap._pa_stream = _QuietThenTalking(cap, gap_s=1.2)
    cap._loopback_reader_pa()
    data, _ = sf.read(str(wav), dtype="float32")
    filled = cap.get_capture_stats()["loopback_gap_filled_s"]
    assert filled == pytest.approx(1.2, abs=0.25)
    assert len(data) == pytest.approx(4 * ac.BLOCK_SIZE + filled * 16000,
                                      abs=50)
    assert cap.get_capture_stats()["loopback_samples"] == len(data)
