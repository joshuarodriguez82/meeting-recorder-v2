"""
The sidebar spinner that never stopped (field report: "that spinning dial
at the top left will go on for a long while even if the processing is
complete").

Root cause: ``Services.current_status`` is a write-once mailbox
(``server.py`` ``_record_status``) that the frontend used to treat as a
busy signal — "current_status is non-empty" was read as "still working".
But the backend never clears it, so a TERMINAL message ("Processing
complete.", "Recording saved. Ready to process.", "Error saving audio:
…") parks there forever and the spinner never stops.

The fix is a real busy signal — ``Services._processing_count`` /
``is_processing`` — maintained around every place that does genuine
pipeline work: the manual ``/process`` endpoint (``process_session``),
the manual-or-auto ``/process_full`` endpoint (``process_full``, also
used by the backend auto-process-after-stop path via
``_auto_process_session``), and the stop/finalize path
(``_stop_recording_sync``, which is also where the terminal status
strings above get set).

These tests pin the two properties that matter:

  - the indicator is released in a ``finally``, so an exception mid-
    processing can't leave it stuck True forever (that failure mode
    would just be this exact bug, one layer down — see the module
    comment on ``Services._end_processing``);
  - it's a COUNTER, not a bool, because pipeline work can genuinely
    overlap (different sessions processing at once — see the
    ``_PROCESSING_LOCK`` docstring in server.py), so one job finishing
    must not flip the shared indicator back to False while another is
    still running.

All ML/LLM dependencies are stubbed out with ``SimpleNamespace`` /
monkeypatched functions — these tests exercise only the counter
plumbing, not the transcription/diarization/summarization pipeline
itself (that's what the CI venv's ``numpy scipy soundfile pytest
fastapi`` allows).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from _app_import import import_app

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_processing_counter():
    """`server.svc` is a module-level singleton shared across the whole
    test session (import happens once) — make sure no test leaks a
    stuck counter into the next one."""
    server.svc._processing_count = 0
    yield
    server.svc._processing_count = 0


def _stub_settings():
    return SimpleNamespace(
        is_configured=True, anthropic_api_key="sk-x", hf_token="hf_x",
    )


def _install_process_session_stubs(monkeypatch, *, raise_error=None):
    """Wire server.svc so process_session can run without any real
    ML/LLM deps. If raise_error is set, the (stubbed) transcribe/diarize
    stage raises it instead of succeeding. Returns a dict the caller can
    inspect after the call to see whether is_processing was actually
    True *during* the stubbed pipeline call."""
    settings = _stub_settings()

    def fake_load_settings():
        server.svc.settings = settings
        return settings
    monkeypatch.setattr(server.svc, "load_settings", fake_load_settings)
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)

    fake_session = SimpleNamespace(session_id="S1", segments=[], speakers={})
    session_svc = SimpleNamespace(
        load_full=lambda sid: fake_session,
        save=lambda s: None,
    )
    monkeypatch.setattr(server.svc, "session_svc", session_svc)
    monkeypatch.setattr(server.svc, "search_svc", None)

    seen = {"busy_during_call": False}

    async def fake_process_session(session):
        # The important observation: is_processing must already be True
        # by the time the actual pipeline work runs.
        seen["busy_during_call"] = server.svc.is_processing
        if raise_error is not None:
            raise raise_error
        return SimpleNamespace(
            session_id="S1", segments=[1, 2, 3], speakers={"a": 1},
        )

    monkeypatch.setattr(
        server.svc, "recording_svc",
        SimpleNamespace(process_session=fake_process_session,
                        is_recording=False, current_session=None),
    )

    async def fake_auto_identify(*_a, **_k):
        return None
    monkeypatch.setattr(server, "_auto_identify_and_save_speakers", fake_auto_identify)
    monkeypatch.setattr(server, "_auto_export_to_client", lambda *_a, **_k: None)

    async def fake_auto_extract_commitments(*_a, **_k):
        return None
    monkeypatch.setattr(server, "_auto_extract_commitments", fake_auto_extract_commitments)

    return seen


# ── Counter primitive ────────────────────────────────────────────────

def test_processing_counter_true_while_outstanding_false_when_drained():
    s = server.Services()
    assert s.is_processing is False
    s._begin_processing()
    assert s.is_processing is True
    s._end_processing()
    assert s.is_processing is False


def test_processing_counter_survives_overlap():
    """Two units of work in flight at once: releasing ONE must not clear
    the indicator while the other is still outstanding. This is the
    exact property a plain bool would get wrong — see the module
    docstring on why a counter is required here."""
    s = server.Services()
    s._begin_processing()
    s._begin_processing()
    assert s.is_processing is True
    s._end_processing()
    assert s.is_processing is True  # one still outstanding
    s._end_processing()
    assert s.is_processing is False


def test_processing_counter_cannot_go_negative():
    """A stray extra release (a bug elsewhere, or a test) must not drive
    the counter negative and make is_processing lie False when a real
    `_begin_processing` follows."""
    s = server.Services()
    s._end_processing()
    s._end_processing()
    assert s._processing_count == 0
    assert s.is_processing is False
    s._begin_processing()
    assert s.is_processing is True


# ── /sessions/{id}/process (process_session) ────────────────────────

def test_process_session_busy_during_call_and_clear_after_success(monkeypatch):
    seen = _install_process_session_stubs(monkeypatch)
    assert server.svc.is_processing is False
    result = asyncio.run(server.process_session("S1"))
    assert result["ok"] is True
    assert seen["busy_during_call"] is True
    assert server.svc.is_processing is False


def test_process_session_releases_indicator_on_exception(monkeypatch):
    """THE important one: an exception mid-processing must not leave
    is_processing stuck True forever — that failure mode is this exact
    bug, just one layer down from current_status never clearing."""
    seen = _install_process_session_stubs(
        monkeypatch, raise_error=RuntimeError("native transcribe blew up"))
    assert server.svc.is_processing is False
    with pytest.raises(HTTPException):
        asyncio.run(server.process_session("S1"))
    assert seen["busy_during_call"] is True
    assert server.svc.is_processing is False


def test_process_session_releases_indicator_on_fast_400(monkeypatch):
    """Even the early "API keys not configured" 400 — raised before any
    real work starts — must still release the indicator; begin/end wrap
    the WHOLE handler, not just the pipeline call."""
    def fake_load_settings():
        server.svc.settings = SimpleNamespace(
            is_configured=False, anthropic_api_key="", hf_token="")
        return server.svc.settings
    monkeypatch.setattr(server.svc, "load_settings", fake_load_settings)
    assert server.svc.is_processing is False
    with pytest.raises(HTTPException):
        asyncio.run(server.process_session("S1"))
    assert server.svc.is_processing is False


def test_overlapping_process_session_and_stop_dont_clear_indicator_early(monkeypatch):
    """A manual /process for one session and the stop-recording finalize
    for a DIFFERENT session can genuinely overlap in the real app — the
    stop path never touches `_PROCESSING_LOCK` (only the shared
    transcribe/diarize stage does; see its docstring). If the busy
    indicator were a bool instead of a counter, whichever job finished
    first would flip it back to False while the other was still
    genuinely running."""
    seen = _install_process_session_stubs(monkeypatch)
    release_process = asyncio.Event()

    async def fake_process_session_blocking(session):
        seen["busy_during_call"] = server.svc.is_processing
        await release_process.wait()
        return SimpleNamespace(session_id="S1", segments=[1], speakers={})

    monkeypatch.setattr(
        server.svc, "recording_svc",
        SimpleNamespace(
            process_session=fake_process_session_blocking,
            stop_recording=lambda: SimpleNamespace(session_id="S2"),
            # /process's still-recording guard (the 409) reads these
            # before the pipeline call; the stub models the interface.
            is_recording=False, current_session=None,
        ),
    )

    async def run():
        proc_task = asyncio.create_task(server.process_session("S1"))
        # Let process_session actually start and reach the blocking await.
        await asyncio.sleep(0.05)
        assert server.svc.is_processing is True

        # Stop-recording finalize for a DIFFERENT session, running
        # concurrently — it doesn't hold/wait on _PROCESSING_LOCK, so
        # it's never blocked by the still-running process_session above.
        stop_result = await asyncio.to_thread(server._stop_recording_sync)
        assert stop_result.session_id == "S2"

        # The stop finished, but process_session is STILL genuinely
        # running. A bool indicator would read False here; the counter
        # must not.
        assert server.svc.is_processing is True

        release_process.set()
        return await proc_task

    result = asyncio.run(run())
    assert result["ok"] is True
    assert server.svc.is_processing is False


# ── /recording/status ────────────────────────────────────────────────

def test_recording_status_reports_is_processing_false_when_idle(monkeypatch):
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "recording_svc", None)
    result = asyncio.run(server.recording_status())
    assert result.is_processing is False


def test_recording_status_reports_is_processing_true_when_busy(monkeypatch):
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "recording_svc", None)
    server.svc._begin_processing()
    try:
        result = asyncio.run(server.recording_status())
        assert result.is_processing is True
    finally:
        server.svc._end_processing()


# ── /sessions/{id}/process_full (process_full) ──────────────────────
#
# process_full is the OTHER entry point named in the bug report — it's
# both the manual "Process full" button AND the function the backend
# auto-process-after-stop path calls (_auto_process_session ->
# process_full, on its own retry loop). One wrap covers both callers.

def _install_process_full_stubs(monkeypatch, *, configured=True):
    """Wire server.svc so process_full can run its extraction stage
    (stage 1, transcribe/diarize, is skipped by giving the session
    pre-existing segments) without any real LLM deps."""
    settings = _stub_settings() if configured else SimpleNamespace(
        is_configured=False)

    def fake_load_settings():
        server.svc.settings = settings
        return settings
    monkeypatch.setattr(server.svc, "load_settings", fake_load_settings)
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)

    fake_session = SimpleNamespace(
        session_id="S1",
        segments=[{"speaker_id": "A", "start": 0, "end": 1, "text": "hi"}],
        notes="", screenshots=[], started_at=None,
        full_transcript=lambda: "hi",
    )
    session_svc = SimpleNamespace(
        load_full=lambda sid: fake_session,
        save=lambda s: None,
    )
    monkeypatch.setattr(server.svc, "session_svc", session_svc)
    monkeypatch.setattr(server.svc, "search_svc", None)
    monkeypatch.setattr(server.svc, "commitments_svc", None)
    monkeypatch.setattr(server.svc, "engagement_svc", None)

    seen = {"busy_during_call": False}

    async def fake_summarize(*_a, **_k):
        seen["busy_during_call"] = server.svc.is_processing
        return "summary text"

    async def fake_markdown(*_a, **_k):
        return "- item"

    async def fake_extract_structured(*_a, **_k):
        return {}

    monkeypatch.setattr(
        server.svc, "summarizer",
        SimpleNamespace(
            summarize=fake_summarize,
            extract_action_items=fake_markdown,
            extract_decisions=fake_markdown,
            extract_requirements=fake_markdown,
            extract_structured=fake_extract_structured,
        ),
    )
    monkeypatch.setattr(
        server.svc, "template_svc",
        SimpleNamespace(get_prompt=lambda name: "prompt"),
    )
    return seen


def test_process_full_busy_during_call_and_clear_after_success(monkeypatch):
    seen = _install_process_full_stubs(monkeypatch)
    assert server.svc.is_processing is False
    result = asyncio.run(server.process_full("S1", server.ProcessFullRequest()))
    assert result["ok"] is True
    assert seen["busy_during_call"] is True
    assert server.svc.is_processing is False


def test_process_full_releases_indicator_on_not_configured(monkeypatch):
    _install_process_full_stubs(monkeypatch, configured=False)
    assert server.svc.is_processing is False
    with pytest.raises(HTTPException):
        asyncio.run(server.process_full("S1", server.ProcessFullRequest()))
    assert server.svc.is_processing is False


def test_process_full_releases_indicator_when_extraction_raises(monkeypatch):
    """The extraction stage failing (not just transcribe/diarize) must
    still release the indicator — process_full applies exceptions
    per-extraction (asyncio.gather(..., return_exceptions=True)) rather
    than propagating, but a bug in the surrounding bookkeeping (e.g. the
    session save, or the fire-and-forget task setup) must not be able to
    leave the flag stuck either."""
    seen = _install_process_full_stubs(monkeypatch)

    async def boom(*_a, **_k):
        seen["busy_during_call"] = server.svc.is_processing
        raise RuntimeError("Claude 500")

    monkeypatch.setattr(server.svc.summarizer, "summarize", boom)
    result = asyncio.run(server.process_full("S1", server.ProcessFullRequest()))
    # Per-extraction failures don't fail the whole request (see
    # process_full's docstring) — but the indicator must still clear.
    assert result["ok"] is True
    assert result["stages"]["summary"].startswith("failed")
    assert seen["busy_during_call"] is True
    assert server.svc.is_processing is False
