"""
Regression coverage for the finalize-timing bug diagnosed from field
logs: `session.ended_at` used to be stamped AFTER the finalize
subprocess returned, so `expected_s` (ended_at - started_at) — used by
both AUDIO_INTEGRITY and SYNC_INTEGRITY — silently included the entire
finalize duration. A slow (e.g. AEC-enabled) finalize therefore looked
exactly like lost audio: two AEC-enabled stops showed `mic_gap`
tracking finalize duration to within 0.5s (278.5s finalize -> 278.9s
gap; 144.8s -> 145.2s), even though zero audio was actually lost.

The fix stamps `ended_at` at capture-stop time (before finalize is
spawned) and records finalize's own cost in `session.finalize_duration_s`
instead. This module proves:

  (a) a slow finalize no longer inflates `expected_s` / fires a false
      AUDIO_INTEGRITY or SYNC_INTEGRITY warning, and finalize's cost is
      still visible via `finalize_duration_s`.
  (b) a GENUINELY short WAV (real audio loss, independent of finalize
      speed) still warns — the fix must not weaken real-loss detection.
  (c) the AEC outcome is persisted on the session for all three
      possible states — not requested / requested+decided (accepted or
      rejected) / requested-but-no-decision-came-back — and the last
      case is never indistinguishable from a clean "off" or a
      rejection.

Imports services.recording_service directly, using the same headless-
stub trick as test_capture_confidence.py (recording_service pulls in
sounddevice / faster_whisper transitively at module load).
"""

from datetime import datetime, timedelta

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from services import recording_service as rs  # noqa: E402


class _FakeSettings:
    def __init__(self, recordings_dir, echo_cancellation_enabled=False):
        self.recordings_dir = str(recordings_dir)
        self.echo_cancellation_enabled = echo_cancellation_enabled


class _FakeCapture:
    """Stands in for core.audio_capture.AudioCapture. Only the surface
    stop_recording() actually touches: stop(), the two wallclock-anchor
    attributes, and get_capture_stats()."""

    def __init__(self, mic_samples, mic_sr=16000,
                 loopback_samples=0, loopback_sr=16000,
                 mic_overflows=0, loopback_overflows=0):
        self.mic_start_monotonic = None
        self.loopback_start_monotonic = None
        self._stats = {
            "mic_sr": mic_sr,
            "loopback_sr": loopback_sr,
            "mic_samples": mic_samples,
            "loopback_samples": loopback_samples,
            "mic_overflows": mic_overflows,
            "loopback_overflows": loopback_overflows,
        }

    def stop(self):
        pass

    def get_capture_stats(self):
        return self._stats


def _make_svc(tmp_path, echo_cancellation_enabled=False) -> "rs.RecordingService":
    settings = _FakeSettings(tmp_path, echo_cancellation_enabled)
    return rs.RecordingService(settings=settings)


def _arm(svc, *, started_at, mic_samples, mic_sr=16000,
         loopback_samples=0, loopback_sr=16000):
    """Pokes just enough state into a fresh RecordingService to drive
    stop_recording() through the mic-only merge branch without any real
    audio hardware or files — the finalize call itself is monkeypatched
    by each test."""
    session = rs.Session(session_id="TESTSESS")
    session.started_at = started_at
    svc._session = session
    svc._recording = True
    svc._capture = _FakeCapture(
        mic_samples=mic_samples, mic_sr=mic_sr,
        loopback_samples=loopback_samples, loopback_sr=loopback_sr,
    )
    svc._chunk_count = 1
    svc._wav_temp_path = "/tmp/does-not-exist-mic.wav"
    svc._loopback_temp_path = None
    svc._live_transcriber = None
    svc._wav_writer = None
    return session


# ── (a) slow finalize must not corrupt the capture-window math ───────

def test_long_finalize_does_not_produce_false_integrity_warnings(tmp_path, monkeypatch):
    """The field repro, reproduced directly: finalize takes real
    wall-clock time (standing in for a 278s AEC-enabled finalize on a
    real machine) but must never be counted as part of the recording
    window. mic_samples exactly matches the (pre-finalize) capture
    window, so a correct implementation reports zero deficit."""
    import time as _time

    capture_window_s = 1800.0  # a 30-minute meeting
    mic_sr = 16000
    mic_samples = int(capture_window_s * mic_sr)

    def fake_finalize(**kwargs):
        _time.sleep(0.4)  # stand-in for a slow AEC-enabled finalize
        return (capture_window_s, False, None)

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path)
    _arm(svc,
         started_at=datetime.now() - timedelta(seconds=capture_window_s),
         mic_samples=mic_samples, mic_sr=mic_sr)

    out = svc.stop_recording()

    assert out.audio_integrity_warning is None
    assert out.sync_warning is None
    assert out.audio_expected_duration_s == pytest.approx(
        capture_window_s, abs=2.0)
    # Finalize's cost must still be visible, just separately.
    assert out.finalize_duration_s is not None
    assert out.finalize_duration_s >= 0.35


def test_finalize_duration_recorded_even_when_fast(tmp_path, monkeypatch):
    def fake_finalize(**kwargs):
        return (60.0, False, None)

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=60),
         mic_samples=60 * 16000)

    out = svc.stop_recording()
    assert out.finalize_duration_s is not None
    assert out.finalize_duration_s < 5.0  # generous CI-jitter bound


# ── (b) a genuinely short WAV must still warn ─────────────────────────

def test_genuinely_short_wav_still_warns(tmp_path, monkeypatch):
    """Capture itself ran the full window (mic_samples matches it) but
    the merged WAV came back dramatically shorter — real loss, nothing
    to do with finalize speed. Must still warn, and the message must
    not misattribute the loss to finalize."""
    capture_window_s = 1800.0
    mic_sr = 16000
    mic_samples = int(capture_window_s * mic_sr)

    def fake_finalize(**kwargs):
        return (600.0, False, None)  # 10 min out of a 30-min window

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path)
    _arm(svc,
         started_at=datetime.now() - timedelta(seconds=capture_window_s),
         mic_samples=mic_samples, mic_sr=mic_sr)

    out = svc.stop_recording()

    assert out.audio_integrity_warning is not None
    msg = out.audio_integrity_warning.lower()
    assert "capture window" in msg
    # The rewritten message must explicitly separate finalize time from
    # the loss claim, not just drop the topic.
    assert "finalize" in msg
    assert out.audio_actual_duration_s == pytest.approx(600.0)
    assert out.audio_expected_duration_s == pytest.approx(
        capture_window_s, abs=2.0)


def test_sync_integrity_still_flags_real_mic_gap(tmp_path, monkeypatch):
    """A capture stream that genuinely fell behind wall-clock (dropped
    frames) must still produce a sync_warning — independent of finalize
    speed and independent of the AUDIO_INTEGRITY check above."""
    capture_window_s = 1800.0
    mic_sr = 16000
    # Mic only delivered 25 of the 30 minutes' worth of samples.
    mic_samples = int(1500.0 * mic_sr)

    def fake_finalize(**kwargs):
        return (1500.0, False, None)

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path)
    _arm(svc,
         started_at=datetime.now() - timedelta(seconds=capture_window_s),
         mic_samples=mic_samples, mic_sr=mic_sr)

    out = svc.stop_recording()
    assert out.sync_warning is not None
    assert "behind real time" in out.sync_warning


# ── (c) AEC outcome persisted for every distinct state ────────────────

def test_aec_outcome_persisted_when_accepted(tmp_path, monkeypatch):
    accepted = {
        "requested": True, "accepted": True,
        "reason": "accepted_erle_pass",
        "erle_db": 12.3, "residual_delay_ms": 4.0,
    }

    def fake_finalize(**kwargs):
        return (100.0, True, dict(accepted))

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path, echo_cancellation_enabled=True)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=100),
         mic_samples=100 * 16000)

    out = svc.stop_recording()
    assert out.aec_outcome == accepted


def test_aec_outcome_persisted_when_rejected(tmp_path, monkeypatch):
    rejected = {
        "requested": True, "accepted": False,
        "reason": "erle_below_threshold",
        "erle_db": 1.1, "residual_delay_ms": 9.0,
    }

    def fake_finalize(**kwargs):
        return (100.0, True, dict(rejected))

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path, echo_cancellation_enabled=True)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=100),
         mic_samples=100 * 16000)

    out = svc.stop_recording()
    assert out.aec_outcome == rejected


def test_aec_requested_but_no_decision_is_recorded_distinctly(tmp_path, monkeypatch):
    """The finalize subprocess succeeded (a RESULT line came back) but
    no AEC_RESULT line did, despite --echo-cancellation having been
    requested. This must render as "unknown", NEVER as "AEC rejected"
    (accepted=False with a real reason) and NEVER as "not requested" —
    exactly the "unreadable outcome renders as clean" failure mode
    called out in the task (document hits as "Untitled" sessions;
    extension posts as "never posted")."""

    def fake_finalize(**kwargs):
        return (100.0, True, None)  # no AEC_RESULT parsed

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path, echo_cancellation_enabled=True)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=100),
         mic_samples=100 * 16000)

    out = svc.stop_recording()
    assert out.aec_outcome["requested"] is True
    assert out.aec_outcome["accepted"] is None
    assert out.aec_outcome["reason"] == "no_decision_returned"
    # Must not collapse into either of the other two shapes.
    assert out.aec_outcome != {"requested": False}
    assert out.aec_outcome.get("accepted") is not False


def test_aec_not_requested_recorded_distinctly(tmp_path, monkeypatch):
    def fake_finalize(**kwargs):
        return (100.0, False, None)

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path, echo_cancellation_enabled=False)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=100),
         mic_samples=100 * 16000)

    out = svc.stop_recording()
    assert out.aec_outcome == {"requested": False}


def test_aec_outcome_recorded_when_finalize_subprocess_itself_fails(tmp_path, monkeypatch):
    """AEC was requested but the whole finalize subprocess crashed
    before any AEC_RESULT could come back. Still must be recorded as a
    distinct "no decision" state, not silently dropped."""

    def fake_finalize(**kwargs):
        raise RuntimeError("finalize subprocess exited with code -11")

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path, echo_cancellation_enabled=True)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=100),
         mic_samples=100 * 16000)

    out = svc.stop_recording()
    assert out.aec_outcome["requested"] is True
    assert out.aec_outcome["accepted"] is None
    assert out.aec_outcome["reason"] == "finalize_subprocess_failed"


# ── finalize-in-progress state (field repro 2026-08-14) ───────────────
#
# The user clicked Process 36s into a 192s AEC finalize and got told the
# WAV "may have been moved, deleted, or not yet synced down from the
# cloud" — all three false. These tests pin the fix at its source: the
# persisted three-state marker (finalize_status / finalize_started_at /
# finalize_error) that /sessions/{id}/process (see server.py) reads to
# tell "still running" and "failed" apart from "genuinely missing".

def test_finalize_status_is_finalizing_and_on_disk_during_the_subprocess_call(
    tmp_path, monkeypatch,
):
    """The marker must be written to the session JSON on disk BEFORE
    the (blocking, possibly minutes-long) subprocess call — a
    concurrent /sessions/{id}/process request reads the session from
    disk, not from this in-memory object, so if the stub write happened
    AFTER the call started, a request arriving mid-finalize would see
    stale (pre-recording-stop) state instead of "finalizing"."""
    import json as _json

    seen: dict = {}

    def fake_finalize(**kwargs):
        # Inspect what's actually on disk RIGHT NOW, mid-call.
        stub_path = tmp_path / "session_TESTSESS.json"
        seen["stub_existed"] = stub_path.exists()
        if stub_path.exists():
            data = _json.loads(stub_path.read_text())
            seen["finalize_status_on_disk"] = data.get("finalize_status")
            seen["finalize_started_at_on_disk"] = data.get("finalize_started_at")
        return (60.0, False, None)

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=60),
         mic_samples=60 * 16000)

    svc.stop_recording()

    assert seen["stub_existed"] is True
    assert seen["finalize_status_on_disk"] == "finalizing"
    assert seen["finalize_started_at_on_disk"] is not None


def test_finalize_status_cleared_on_success(tmp_path, monkeypatch):
    def fake_finalize(**kwargs):
        return (60.0, False, None)

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=60),
         mic_samples=60 * 16000)

    out = svc.stop_recording()

    assert out.finalize_status is None
    assert out.finalize_started_at is None
    assert out.finalize_error is None


def test_finalize_status_failed_with_reason_on_subprocess_error(tmp_path, monkeypatch):
    def fake_finalize(**kwargs):
        raise RuntimeError("finalize subprocess exited with code -11")

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc = _make_svc(tmp_path)
    _arm(svc, started_at=datetime.now() - timedelta(seconds=60),
         mic_samples=60 * 16000)

    out = svc.stop_recording()

    assert out.finalize_status == "failed"
    assert out.finalize_error is not None
    assert "code -11" in out.finalize_error
    # The reason it started (and roughly when) must survive so a
    # /sessions/{id}/process caller can still report it.
    assert out.finalize_started_at is not None


def test_finalize_status_fields_round_trip_through_json():
    """The whole point of persisting this to disk (see
    _write_session_stub) is that a reader loading the session JSON gets
    the same state back — this is what makes the marker survive a
    backend restart."""
    session = rs.Session(session_id="RT1")
    session.finalize_status = "finalizing"
    session.finalize_started_at = datetime(2026, 8, 14, 10, 51, 10)
    session.finalize_error = None

    restored = rs.Session.from_dict(session.to_dict())

    assert restored.finalize_status == "finalizing"
    assert restored.finalize_started_at == datetime(2026, 8, 14, 10, 51, 10)
    assert restored.finalize_error is None

    session.finalize_status = "failed"
    session.finalize_error = "finalize subprocess exited with code -11"
    restored2 = rs.Session.from_dict(session.to_dict())
    assert restored2.finalize_status == "failed"
    assert restored2.finalize_error == "finalize subprocess exited with code -11"

    # A session that predates this feature (no finalize_status key at
    # all in the dict) must load as None, not raise or default to a
    # scary value.
    legacy = rs.Session.from_dict({"session_id": "LEGACY"})
    assert legacy.finalize_status is None
    assert legacy.finalize_started_at is None
    assert legacy.finalize_error is None
