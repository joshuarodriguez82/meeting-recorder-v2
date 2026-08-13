"""
Capture-confidence meters — the "is audio actually reaching the file"
instrument described in AGENTS.md's "Why this exists": before this,
the only signal that capture had silently died was noticing the live
transcript had stopped producing text.

Covers:
  - the RMS -> 0..1 meter mapping (monotonic, visually distinct bands)
  - flowing / silent / dead per-stream state transitions, driven by
    injected timestamps (never real sleeps)
  - the false-alarm guard: a silent-but-alive stream must NEVER produce
    a capture_warning — this is the single most important property of
    the whole feature, since a false alarm here trains the user to
    ignore the real one
  - a genuinely dead stream DOES warn, after the threshold and not
    before

Imports services.recording_service directly. That module pulls in
core.audio_capture (sounddevice) and core.transcription (faster_whisper)
at module load time, neither of which is in the lightweight CI test
env — so we reuse the same headless-stub trick _app_import.py already
uses for server.py before importing.
"""

from datetime import datetime, timedelta

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from services import recording_service as rs  # noqa: E402


# ── RMS -> level mapping ────────────────────────────────────────────

def test_rms_to_level_zero_and_negative_are_zero():
    assert rs._rms_to_level(0.0) == 0.0
    assert rs._rms_to_level(-1.0) == 0.0


def test_rms_to_level_monotonic_and_distinct():
    quiet = rs._rms_to_level(rs.SILENCE_RMS_FLOOR)       # "someone is talking" floor
    normal = rs._rms_to_level(rs.DUCK_LEVEL_THRESHOLD)    # "quiet speech band"
    loud = rs._rms_to_level(0.2)                          # loud / near-clipping

    # Strictly increasing — louder audio must never read as a smaller
    # or equal meter width.
    assert 0.0 <= quiet < normal < loud <= 1.0

    # The whole point of the log/dB mapping: normal speech should NOT
    # be pinned near zero on a linear-RMS reading. DUCK_LEVEL_THRESHOLD
    # is documented as "quiet speech band" — it should land comfortably
    # in the meter's middle range, not down in single digits.
    assert normal >= 0.4


def test_rms_to_level_clamps_to_one():
    assert rs._rms_to_level(10.0) == 1.0


def test_rms_to_level_never_raises_on_bad_input():
    assert rs._rms_to_level(float("nan")) == 0.0
    assert rs._rms_to_level(float("inf")) == 0.0


# ── _stream_state transitions (pure function, injected timestamps) ──

def test_stream_state_flowing_when_recent_audio():
    now = datetime(2026, 1, 1, 12, 0, 0)
    last_chunk = now - timedelta(seconds=0.1)
    last_audio = now - timedelta(seconds=0.1)
    assert rs._stream_state(last_chunk, last_audio, now) == "flowing"


def test_stream_state_silent_when_chunks_arrive_but_quiet():
    now = datetime(2026, 1, 1, 12, 0, 0)
    last_chunk = now - timedelta(seconds=0.1)   # chunks still arriving
    last_audio = now - timedelta(seconds=30)    # but nothing above the floor recently
    assert rs._stream_state(last_chunk, last_audio, now) == "silent"


def test_stream_state_silent_when_never_any_real_audio():
    now = datetime(2026, 1, 1, 12, 0, 0)
    last_chunk = now - timedelta(seconds=0.1)
    assert rs._stream_state(last_chunk, None, now) == "silent"


def test_stream_state_dead_when_no_chunks_ever():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert rs._stream_state(None, None, now) == "dead"


def test_stream_state_dead_when_chunks_stopped_arriving():
    now = datetime(2026, 1, 1, 12, 0, 0)
    last_chunk = now - timedelta(seconds=rs.STREAM_DEAD_S + 1)
    last_audio = now - timedelta(seconds=rs.STREAM_DEAD_S + 1)
    assert rs._stream_state(last_chunk, last_audio, now) == "dead"


def test_stream_state_boundary_just_under_dead_threshold_is_not_dead():
    now = datetime(2026, 1, 1, 12, 0, 0)
    last_chunk = now - timedelta(seconds=rs.STREAM_DEAD_S - 0.5)
    assert rs._stream_state(last_chunk, None, now) != "dead"


# ── RecordingService.get_capture_levels() integration ───────────────

class _FakeSession:
    def __init__(self, started_at):
        self.started_at = started_at


def _make_recording_service() -> "rs.RecordingService":
    """A RecordingService with just enough state poked in to exercise
    get_capture_levels() without touching real audio devices, models,
    or Settings. Constructor accepts settings=None fine — the methods
    under test never read self._settings."""
    svc = rs.RecordingService(settings=None)
    return svc


def _arm_recording(svc, *, started_at, mic_configured=True, system_configured=True):
    svc._recording = True
    svc._session = _FakeSession(started_at)
    # System-audio configuration is signalled by _loopback_temp_path
    # being non-None, exactly as start_recording() sets it from
    # output_device_index — see recording_service.py.
    svc._loopback_temp_path = "/tmp/fake_loopback.wav" if system_configured else None
    return svc


def test_not_recording_returns_benign_defaults():
    svc = _make_recording_service()
    svc._recording = False
    levels = svc.get_capture_levels()
    assert levels["mic_level"] == 0.0
    assert levels["system_level"] == 0.0
    assert levels["capture_warning"] is None


def test_system_audio_not_configured_reports_none_not_dead():
    """A recording with no system-audio device selected is a deliberate
    choice, not a capture failure — system_state must be None (n/a),
    never 'dead', and must never produce a capture_warning."""
    now = datetime.now()
    svc = _make_recording_service()
    _arm_recording(svc, started_at=now - timedelta(seconds=60),
                    system_configured=False)
    svc._last_chunk_at = now
    svc._last_mic_audio_at = now

    levels = svc.get_capture_levels()
    assert levels["system_state"] is None
    assert levels["capture_warning"] is None


def test_silent_but_alive_stream_produces_no_capture_warning():
    """THE false-alarm guard. Mic chunks are flowing continuously (the
    stream is alive) but every chunk is quiet (muted mic / only the far
    end talking) — completely normal, must never warn, even long after
    the grace period and long past the dead-stream warning threshold."""
    now = datetime.now()
    svc = _make_recording_service()
    _arm_recording(svc, started_at=now - timedelta(minutes=10))
    svc._last_chunk_at = now                       # chunks arriving right now
    svc._last_mic_audio_at = now - timedelta(minutes=9)  # but quiet for 9 minutes
    svc._last_loopback_chunk_at = now
    svc._last_loopback_audio_at = now - timedelta(minutes=9)
    svc._mic_level_ema = 0.0001
    svc._loopback_level_ema = 0.0001

    levels = svc.get_capture_levels()
    assert levels["mic_state"] == "silent"
    assert levels["system_state"] == "silent"
    assert levels["capture_warning"] is None


def test_dead_mic_stream_no_warning_before_threshold():
    now = datetime.now()
    svc = _make_recording_service()
    _arm_recording(svc, started_at=now - timedelta(minutes=5))
    svc._last_chunk_at = now - timedelta(seconds=rs.CAPTURE_WARNING_DEAD_S - 5)
    svc._last_loopback_chunk_at = now

    levels = svc.get_capture_levels()
    assert levels["mic_state"] == "dead"          # meter is honest immediately
    assert levels["capture_warning"] is None       # but banner waits for the threshold


def test_dead_mic_stream_warns_after_threshold():
    now = datetime.now()
    svc = _make_recording_service()
    _arm_recording(svc, started_at=now - timedelta(minutes=5))
    svc._last_chunk_at = now - timedelta(seconds=rs.CAPTURE_WARNING_DEAD_S + 5)
    svc._last_loopback_chunk_at = now

    levels = svc.get_capture_levels()
    assert levels["mic_state"] == "dead"
    assert levels["capture_warning"] is not None
    assert "microphone" in levels["capture_warning"].lower()


def test_dead_system_stream_warns_after_threshold_when_mic_healthy():
    now = datetime.now()
    svc = _make_recording_service()
    _arm_recording(svc, started_at=now - timedelta(minutes=5))
    svc._last_chunk_at = now
    svc._last_mic_audio_at = now
    svc._last_loopback_chunk_at = now - timedelta(seconds=rs.CAPTURE_WARNING_DEAD_S + 5)

    levels = svc.get_capture_levels()
    assert levels["mic_state"] == "flowing"
    assert levels["system_state"] == "dead"
    assert levels["capture_warning"] is not None
    assert "system" in levels["capture_warning"].lower()


def test_warning_suppressed_during_startup_grace_period():
    """A recording that just started hasn't had time to receive its
    first chunk yet — must not immediately flash a scary warning."""
    now = datetime.now()
    svc = _make_recording_service()
    _arm_recording(svc, started_at=now - timedelta(seconds=1))
    svc._last_chunk_at = None
    svc._last_loopback_chunk_at = None

    levels = svc.get_capture_levels()
    assert levels["capture_warning"] is None


def test_flowing_stream_reports_nonzero_level():
    now = datetime.now()
    svc = _make_recording_service()
    _arm_recording(svc, started_at=now - timedelta(minutes=1))
    svc._last_chunk_at = now
    svc._last_mic_audio_at = now
    svc._mic_level_ema = rs.DUCK_LEVEL_THRESHOLD
    svc._last_loopback_chunk_at = now
    svc._last_loopback_audio_at = now
    svc._loopback_level_ema = rs.DUCK_LEVEL_THRESHOLD

    levels = svc.get_capture_levels()
    assert levels["mic_state"] == "flowing"
    assert levels["system_state"] == "flowing"
    assert levels["mic_level"] > 0.3
    assert levels["system_level"] > 0.3
