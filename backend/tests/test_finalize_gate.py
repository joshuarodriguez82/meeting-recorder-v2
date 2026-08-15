"""
Coverage for utils/finalize_gate.py — the process-wide "at most one
finalize at a time" mutex plus the below-normal-CPU-priority hint for
the subprocess it lets through.

Two layers:
  (a) Unit tests directly against utils.finalize_gate — the gate itself
      and the platform-dependent priority-kwargs builder, isolated from
      RecordingService.
  (b) An integration test through RecordingService.stop_recording(),
      proving the field-facing contract: a second stop's finalize
      genuinely waits behind a first one's, surfaces as
      session.finalize_status == "queued" while it waits (not
      "finalizing" — see _finalize_status_detail in server.py), and a
      failure to apply the OS priority hint never blocks the finalize
      subprocess itself from running (services/recording_service.py's
      retry-without-priority-kwargs fallback).
"""

import subprocess
import sys
import threading
import time as _time
from datetime import datetime, timedelta

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from services import recording_service as rs  # noqa: E402
from utils import finalize_gate  # noqa: E402


# ── (a) finalize_slot: process-wide mutex + on_queued callback ───────

def test_finalize_slot_serializes_two_callers():
    """The second caller must not enter the `with` body until the first
    releases it — proven by a shared counter that must never exceed 1
    while either caller is "inside"."""
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()

    def worker():
        nonlocal in_flight, max_in_flight
        with finalize_gate.finalize_slot():
            with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            _time.sleep(0.1)
            with lock:
                in_flight -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max_in_flight == 1


def test_finalize_slot_calls_on_queued_only_when_contended():
    """A caller that acquires the slot uncontended must NOT have
    on_queued invoked. A caller that has to wait for another holder
    MUST have it invoked exactly once, before it blocks."""
    queued_calls = []
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def hold():
        with finalize_gate.finalize_slot(
                on_queued=lambda: queued_calls.append("holder-should-never")):
            holder_entered.set()
            release_holder.wait(timeout=5)

    holder_thread = threading.Thread(target=hold)
    holder_thread.start()
    assert holder_entered.wait(timeout=5)

    waiter_entered = threading.Event()

    def wait_then_enter():
        with finalize_gate.finalize_slot(
                on_queued=lambda: queued_calls.append("waiter")):
            waiter_entered.set()

    waiter_thread = threading.Thread(target=wait_then_enter)
    waiter_thread.start()

    # Give the waiter time to hit the contended lock and call on_queued
    # BEFORE we release the holder.
    _time.sleep(0.2)
    assert queued_calls == ["waiter"]
    assert not waiter_entered.is_set()

    release_holder.set()
    holder_thread.join(timeout=5)
    waiter_thread.join(timeout=5)
    assert waiter_entered.is_set()
    # Still exactly one call — not re-invoked once acquired.
    assert queued_calls == ["waiter"]


def test_finalize_slot_releases_on_exception():
    """A caller that raises inside the `with` body must still release
    the slot — otherwise one crashed finalize would permanently wedge
    every future one."""
    with pytest.raises(RuntimeError):
        with finalize_gate.finalize_slot():
            raise RuntimeError("boom")

    # If the lock leaked, this would hang; timeout is the failure mode
    # proof, but the with-statement itself completing is the real
    # assertion.
    acquired = finalize_gate._lock.acquire(timeout=2)
    assert acquired
    finalize_gate._lock.release()


# ── (a) below_normal_priority_kwargs: per-platform, degrades cleanly ──

def test_priority_kwargs_windows_uses_creationflags(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
    kwargs = finalize_gate.below_normal_priority_kwargs()
    assert kwargs == {"creationflags": 0x4000}


def test_priority_kwargs_windows_without_flag_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", raising=False)
    assert finalize_gate.below_normal_priority_kwargs() == {}


def test_priority_kwargs_posix_uses_preexec_fn(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.name", "posix")
    kwargs = finalize_gate.below_normal_priority_kwargs()
    assert set(kwargs.keys()) == {"preexec_fn"}
    assert callable(kwargs["preexec_fn"])


def test_priority_kwargs_unknown_platform_returns_empty(monkeypatch):
    monkeypatch.setattr(sys, "platform", "some-exotic-os")
    monkeypatch.setattr("os.name", "java")
    assert finalize_gate.below_normal_priority_kwargs() == {}


def test_posix_lower_priority_swallows_nice_failure(monkeypatch):
    """A priority hint that fails to apply must never raise — the
    finalize child must still be allowed to run at normal priority."""
    def raising_nice(n):
        raise PermissionError("simulated: nice() denied")

    monkeypatch.setattr("os.nice", raising_nice)
    # Must not raise.
    finalize_gate._posix_lower_priority()


# ── (b) integration: RecordingService.stop_recording() through the gate

class _FakeSettings:
    def __init__(self, recordings_dir, echo_cancellation_enabled=False):
        self.recordings_dir = str(recordings_dir)
        self.echo_cancellation_enabled = echo_cancellation_enabled


class _FakeCapture:
    def __init__(self, mic_samples, mic_sr=16000):
        self.mic_start_monotonic = None
        self.loopback_start_monotonic = None
        self._stats = {
            "mic_sr": mic_sr, "loopback_sr": mic_sr,
            "mic_samples": mic_samples, "loopback_samples": 0,
            "mic_overflows": 0, "loopback_overflows": 0,
        }

    def stop(self):
        pass

    def get_capture_stats(self):
        return self._stats


def _make_svc_and_session(base_dir, session_id):
    settings = _FakeSettings(base_dir / session_id)
    svc = rs.RecordingService(settings=settings)
    session = rs.Session(session_id=session_id)
    started_at = datetime.now() - timedelta(seconds=10)
    session.started_at = started_at
    svc._session = session
    svc._recording = True
    svc._capture = _FakeCapture(mic_samples=10 * 16000)
    svc._chunk_count = 1
    svc._wav_temp_path = "/tmp/does-not-exist-mic.wav"
    svc._loopback_temp_path = None
    svc._live_transcriber = None
    svc._wav_writer = None
    return svc, session


def test_two_finalizes_cannot_run_concurrently_second_waits(tmp_path, monkeypatch):
    """The field-facing contract: stop a first recording (its finalize
    starts running), then stop a second while the first is still going
    — the second's finalize must not start until the first returns, and
    while it's waiting it must report finalize_status == "queued", not
    "finalizing" (see server.py's _finalize_status_detail)."""
    call_log = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def fake_finalize(**kwargs):
        call_log.append("enter")
        if len(call_log) == 1:
            first_entered.set()
            release_first.wait(timeout=5)
        call_log.append("exit")
        return (10.0, False, None)

    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        staticmethod(fake_finalize))

    svc1, session1 = _make_svc_and_session(tmp_path, "SESS_ONE")
    svc2, session2 = _make_svc_and_session(tmp_path, "SESS_TWO")

    results = {}

    def run1():
        results["out1"] = svc1.stop_recording()

    t1 = threading.Thread(target=run1)
    t1.start()
    assert first_entered.wait(timeout=5), "first finalize never started"

    def run2():
        results["out2"] = svc2.stop_recording()

    t2 = threading.Thread(target=run2)
    t2.start()

    # Poll for session2 to report "queued" — it must reach that state
    # without ever having called fake_finalize (call_log still len 1).
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline and session2.finalize_status != "queued":
        _time.sleep(0.01)
    assert session2.finalize_status == "queued", (
        f"expected queued, got {session2.finalize_status!r}")
    assert call_log == ["enter"], (
        "second finalize must not have started while queued")

    release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert call_log == ["enter", "exit", "enter", "exit"]
    assert results["out1"].finalize_status is None
    assert results["out2"].finalize_status is None
    assert results["out1"].finalize_duration_s is not None
    assert results["out2"].finalize_duration_s is not None


def test_priority_hint_failure_does_not_block_finalize(tmp_path, monkeypatch):
    """If applying the below-normal-priority kwargs makes the spawn
    itself fail, _run_finalize_subprocess must retry at normal priority
    rather than losing the recording's audio over a scheduler nicety."""
    calls = []

    class _FakeCompleted:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        if "preexec_fn" in kwargs or "creationflags" in kwargs:
            raise ValueError("simulated: priority kwarg rejected by platform")
        return _FakeCompleted(
            0, stdout="RESULT duration_s=5.0 loopback_mixed=false\n")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    # Force a deterministic non-empty priority hint regardless of the
    # platform this test actually runs on.
    monkeypatch.setattr(
        rs, "below_normal_priority_kwargs", lambda: {"preexec_fn": lambda: None})

    duration_s, loopback_mixed, aec_outcome = rs.RecordingService._run_finalize_subprocess(
        mic_wav_path=str(tmp_path / "mic.wav"),
        loopback_wav_path=None,
        output_wav_path=str(tmp_path / "out.wav"),
        target_sr=16000,
        loopback_start_offset_s=None,
    )

    assert duration_s == pytest.approx(5.0)
    assert loopback_mixed is False
    assert aec_outcome is None
    assert len(calls) == 2, "expected one failed attempt + one fallback retry"
    assert "preexec_fn" in calls[0]
    assert "preexec_fn" not in calls[1] and "creationflags" not in calls[1]
