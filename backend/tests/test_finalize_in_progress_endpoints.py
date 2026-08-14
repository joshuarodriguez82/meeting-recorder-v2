"""
/sessions/{id}/process (and its siblings) must stop lying about audio
that is still being written.

Field repro (2026-08-14): a user clicked Process 36s into a 192s
AEC-enabled finalize. The WAV wasn't at its final path yet — finalize
was still running — and the app told them the recording "may have been
moved, deleted, or not yet synced down from the cloud." All three were
false; the file landed intact 156s later. No data was lost, but the
user had no way to tell "still being processed" from "gone" and clicked
Process twice in 15 seconds.

These tests pin the three-way split every audio-reading endpoint must
now make (see server.py's `_finalize_status_detail` module comment):

  1. finalize_status == "finalizing" -> 409, not logged as ERROR, tells
     the user how long it's been running and that echo cancellation
     makes this take a while.
  2. finalize_status == "failed"     -> 422, surfaces the recorded
     finalize_error verbatim rather than a generic "missing" claim.
  3. finalize_status is None and the audio is genuinely absent -> the
     pre-existing "missing" RuntimeError-based message, unchanged.

Same headless-import + stub pattern as test_processing_busy_signal.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from _app_import import import_app

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402


MISSING_AUDIO_MSG = (
    "The audio file for this recording is missing — it may have "
    "been moved, deleted, or not yet synced down from the cloud."
)


@pytest.fixture(autouse=True)
def _reset_processing_counter():
    server.svc._processing_count = 0
    yield
    server.svc._processing_count = 0


def _stub_settings(echo_cancellation_enabled=False):
    return SimpleNamespace(
        is_configured=True, anthropic_api_key="sk-x", hf_token="hf_x",
        echo_cancellation_enabled=echo_cancellation_enabled,
    )


def _make_finalizing_session(seconds_ago=36.0, **extra):
    return SimpleNamespace(
        session_id="S1", segments=[], speakers={},
        finalize_status="finalizing",
        finalize_started_at=datetime.now() - timedelta(seconds=seconds_ago),
        finalize_error=None,
        **extra,
    )


def _make_failed_session(reason="finalize subprocess exited with code -11",
                          **extra):
    return SimpleNamespace(
        session_id="S1", segments=[], speakers={},
        finalize_status="failed",
        finalize_started_at=datetime.now() - timedelta(seconds=200),
        finalize_error=reason,
        **extra,
    )


def _make_clean_session(**extra):
    """No finalize history at all — the common/legacy case."""
    return SimpleNamespace(
        session_id="S1", segments=[], speakers={},
        finalize_status=None, finalize_started_at=None, finalize_error=None,
        **extra,
    )


# ── /sessions/{id}/process ─────────────────────────────────────────────

def test_process_session_returns_409_while_finalizing(monkeypatch, caplog):
    session = _make_finalizing_session(seconds_ago=36.0)
    monkeypatch.setattr(server.svc, "load_settings",
                        lambda: setattr(server.svc, "settings", _stub_settings()))
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session))

    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(server.process_session("S1"))

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail.lower()
    assert "finaliz" in detail
    assert "36s" in detail or "0m 36s" in detail
    # Must not be logged as an ERROR — this is a normal, expected state.
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    # The counter must still release.
    assert server.svc.is_processing is False


def test_process_session_mentions_echo_cancellation_when_enabled(monkeypatch):
    session = _make_finalizing_session()
    monkeypatch.setattr(
        server.svc, "load_settings",
        lambda: setattr(server.svc, "settings",
                        _stub_settings(echo_cancellation_enabled=True)))
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.process_session("S1"))

    assert "echo cancellation" in exc_info.value.detail.lower()


def test_process_session_returns_422_when_finalize_failed(monkeypatch):
    session = _make_failed_session(reason="finalize subprocess exited with code -11")
    monkeypatch.setattr(server.svc, "load_settings",
                        lambda: setattr(server.svc, "settings", _stub_settings()))
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.process_session("S1"))

    assert exc_info.value.status_code == 422
    assert "finalize subprocess exited with code -11" in exc_info.value.detail
    assert server.svc.is_processing is False


def test_process_session_still_reports_genuinely_missing_audio(monkeypatch):
    """Case 3, unchanged: no finalize in flight, no finalize failure on
    record, and the audio pipeline itself discovers the file is gone.
    The pre-existing message/500 must survive exactly as it is."""
    session = _make_clean_session()
    monkeypatch.setattr(server.svc, "load_settings",
                        lambda: setattr(server.svc, "settings", _stub_settings()))
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session))
    monkeypatch.setattr(server.svc, "search_svc", None)

    async def fake_process_session(sess):
        raise RuntimeError(MISSING_AUDIO_MSG)

    monkeypatch.setattr(
        server.svc, "recording_svc",
        SimpleNamespace(process_session=fake_process_session))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.process_session("S1"))

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == MISSING_AUDIO_MSG


def test_process_session_proceeds_normally_when_not_finalizing(monkeypatch):
    """Sanity check: a session with no finalize marker set at all must
    not be blocked by the new guard."""
    session = _make_clean_session()
    monkeypatch.setattr(server.svc, "load_settings",
                        lambda: setattr(server.svc, "settings", _stub_settings()))
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session, save=lambda s: None))
    monkeypatch.setattr(server.svc, "search_svc", None)

    async def fake_process_session(sess):
        return SimpleNamespace(session_id="S1", segments=[1], speakers={"a": 1})

    monkeypatch.setattr(
        server.svc, "recording_svc",
        SimpleNamespace(process_session=fake_process_session))

    async def fake_auto_identify(*_a, **_k):
        return None
    monkeypatch.setattr(server, "_auto_identify_and_save_speakers", fake_auto_identify)
    monkeypatch.setattr(server, "_auto_export_to_client", lambda *_a, **_k: None)

    async def fake_auto_extract_commitments(*_a, **_k):
        return None
    monkeypatch.setattr(server, "_auto_extract_commitments", fake_auto_extract_commitments)

    result = asyncio.run(server.process_session("S1"))
    assert result["ok"] is True


# ── /sessions/{id}/summarize ────────────────────────────────────────────

def test_summarize_returns_409_while_finalizing(monkeypatch):
    session = _make_finalizing_session()
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace())
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.summarize_session("S1", server.TemplateRequest()))

    assert exc_info.value.status_code == 409
    assert "finaliz" in exc_info.value.detail.lower()


def test_summarize_returns_422_when_finalize_failed(monkeypatch):
    session = _make_failed_session(reason="disk full mid-merge")
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace())
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.summarize_session("S1", server.TemplateRequest()))

    assert exc_info.value.status_code == 422
    assert "disk full mid-merge" in exc_info.value.detail


def test_summarize_no_transcript_message_unchanged_when_not_finalizing(monkeypatch):
    """Case 3 for summarize: a fully-finalized session that simply has
    no transcript yet keeps the pre-existing plain 400."""
    session = _make_clean_session()
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace())
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load_full=lambda sid: session))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.summarize_session("S1", server.TemplateRequest()))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Session has no transcript"


# ── /sessions/{id}/audio (playback) ─────────────────────────────────────

def test_get_session_audio_returns_409_while_finalizing(monkeypatch):
    data = {
        "session_id": "S1",
        "audio_path": "/recordings/session_S1.wav",
        "finalize_status": "finalizing",
        "finalize_started_at": datetime.now().isoformat(),
        "finalize_error": None,
    }
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load=lambda sid: data))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_session_audio("S1"))

    assert exc_info.value.status_code == 409
    assert "finaliz" in exc_info.value.detail.lower()


def test_get_session_audio_returns_422_when_finalize_failed(monkeypatch):
    data = {
        "session_id": "S1",
        "audio_path": "/recordings/session_S1.wav",
        "finalize_status": "failed",
        "finalize_started_at": datetime.now().isoformat(),
        "finalize_error": "merge crashed",
    }
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load=lambda sid: data))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_session_audio("S1"))

    assert exc_info.value.status_code == 422
    assert "merge crashed" in exc_info.value.detail


def test_get_session_audio_still_404s_when_genuinely_missing(monkeypatch):
    """Case 3 for playback: no finalize marker at all, file really
    isn't there — pre-existing plain 404 survives."""
    data = {
        "session_id": "S1",
        "audio_path": None,
        "finalize_status": None,
        "finalize_started_at": None,
        "finalize_error": None,
    }
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(load=lambda sid: data))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_session_audio("S1"))

    assert exc_info.value.status_code == 404
