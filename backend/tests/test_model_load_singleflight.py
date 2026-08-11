"""Single-flight model loading.

The 2026-07-21 logs showed the backend segfault-crash-looping
(0xC0000005) during "Loading transcription engine": several endpoints
spawn model-load threads at app-open, the old `ready or loading` guard
was a non-atomic check-then-set, and two threads could both enter the
native torch/ctranslate2 init concurrently. This pins the fix: no
matter how many threads call ensure_models_loaded at once, the load
body runs EXACTLY once, with zero concurrent entries.
"""

import threading
import time
from types import SimpleNamespace

from _app_import import import_app

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402


def _make_services(monkeypatch):
    svc_obj = server.Services()

    stub_settings = SimpleNamespace(
        is_configured=True, whisper_model="base", hf_token="hf_x",
        max_speakers=4, diarization_device="auto",
    )

    def fake_load_settings():
        svc_obj.settings = stub_settings
        svc_obj.recording_svc = SimpleNamespace(
            set_engines=lambda *a, **k: None)
        return stub_settings

    monkeypatch.setattr(svc_obj, "load_settings", fake_load_settings)

    # Counters shared by the fake engines.
    state = {"concurrent": 0, "max_concurrent": 0, "constructions": 0}
    lock = threading.Lock()

    class FakeEngine:
        def __init__(self, *a, **k):
            with lock:
                state["concurrent"] += 1
                state["constructions"] += 1
                state["max_concurrent"] = max(
                    state["max_concurrent"], state["concurrent"])
            time.sleep(0.05)  # widen the race window like a real native init
            with lock:
                state["concurrent"] -= 1

    monkeypatch.setattr(server, "TranscriptionEngine", FakeEngine)
    monkeypatch.setattr(server, "DiarizationEngine", FakeEngine)
    return svc_obj, state


def test_concurrent_callers_load_exactly_once(monkeypatch):
    svc_obj, state = _make_services(monkeypatch)

    threads = [
        threading.Thread(target=lambda: svc_obj.ensure_models_loaded())
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert svc_obj.models_ready is True
    # 2 constructions total (transcription + diarization), from ONE winner.
    assert state["constructions"] == 2, (
        f"expected exactly one load pass, got "
        f"{state['constructions']} engine constructions")
    # And never two native inits overlapping — the crash condition.
    assert state["max_concurrent"] == 1


def test_second_call_after_success_is_noop(monkeypatch):
    svc_obj, state = _make_services(monkeypatch)
    svc_obj.ensure_models_loaded()
    assert state["constructions"] == 2
    svc_obj.ensure_models_loaded()
    assert state["constructions"] == 2  # unchanged


def test_failed_load_can_be_retried(monkeypatch):
    svc_obj, _ = _make_services(monkeypatch)

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("native init blew up")

    monkeypatch.setattr(server, "TranscriptionEngine", Boom)
    try:
        svc_obj.ensure_models_loaded()
    except RuntimeError:
        pass
    assert svc_obj.models_ready is False
    assert svc_obj.models_loading is False       # flag released on failure
    assert svc_obj.models_error is not None      # surfaced to /status


# ── Crash-resilient auto-process resume (same incident class) ────────

from datetime import datetime, timedelta

from models.session import Session


def test_auto_process_marker_round_trips_through_json():
    s = Session(session_id="AP1")
    s.auto_process_pending = {
        "resumes": 1, "template": "General", "follow_up": False,
        "started_at": "2026-07-21T08:00:00",
    }
    restored = Session.from_dict(s.to_dict())
    assert restored.auto_process_pending == s.auto_process_pending
    # And absence stays absent (legacy JSONs).
    bare = Session.from_dict(Session(session_id="AP2").to_dict())
    assert bare.auto_process_pending is None


def test_resume_decision_fresh_marker_resumes():
    now = datetime.now()
    marker = {"resumes": 0, "started_at": (now - timedelta(hours=1)).isoformat()}
    assert server._auto_process_resume_decision(marker, now) == "resume"


def test_resume_decision_poison_pill_gives_up():
    now = datetime.now()
    marker = {"resumes": 2, "started_at": now.isoformat()}
    assert server._auto_process_resume_decision(marker, now) == "give_up"


def test_resume_decision_stale_marker():
    now = datetime.now()
    marker = {"resumes": 0,
              "started_at": (now - timedelta(hours=72)).isoformat()}
    assert server._auto_process_resume_decision(marker, now) == "stale"


def test_resume_decision_malformed_marker_still_resumes():
    # Garbage fields must not crash the startup pass.
    assert server._auto_process_resume_decision({}, datetime.now()) == "resume"
    assert server._auto_process_resume_decision(
        {"resumes": "x", "started_at": 42}, datetime.now()) == "resume"
