"""
A processing run that failed must not render as one that succeeded.

THE REGRESSION (shipped in v2.79.0, found 2026-09-02)
-----------------------------------------------------
v2.79.0 replaced the sidebar status strip with an activity centre backed
by a stage model (services/pipeline_progress.py). That model has a
``fail()`` method, a per-stage FAILED state, and a panel that paints a
failed stage red.

Nothing in the application ever called ``fail()``.

``process_full`` catches a transcribe/diarize exception, logs it,
records ``stages["transcribe_diarize"] = "failed: …"`` and returns a
**200** with ``ok: False`` — it does not raise. The endpoint's
``finally`` then runs ``Services._end_processing()``, which sees
``pipeline.error() is None``, concludes the run finished cleanly, and
calls ``pipeline.complete()``. Every stage goes green.

Reproduced in-process before the fix::

    svc._begin_processing()
    svc._record_status("__stage:transcribe:done____stage:diarize:active__")
    svc._end_processing()          # what the finally does
    → done: True | error: None | stages: all "done"

So the failure mode this whole release was built to end — work that did
not happen rendering as work that did — was reintroduced by the release
itself, one layer up. The stage model was honest; nobody told it the
truth.

Both entry points are covered here because they fail differently:

* ``process_full`` returns ``ok: False`` without raising, so an
  exception-based guard would never see it.
* ``_auto_process_session`` retries, then gives up with a log line and
  no user-visible signal at all.
"""

from __future__ import annotations

import asyncio

import pytest

from _app_import import import_app

import_app()
import server  # noqa: E402


class _Sessions:
    """Enough SessionService for process_full to reach the ML call."""

    def __init__(self, session):
        self._session = session
        self.saved = []

    def load_full(self, session_id):
        return self._session

    def save(self, session):
        self.saved.append(session)


class _Session:
    def __init__(self, session_id="S1", segments=None):
        self.session_id = session_id
        self.audio_path = "/nonexistent/audio.wav"
        self.segments = segments or []
        self.notes = ""
        self.screenshots = []
        self.summary = ""
        self.action_items = []
        self.decisions = []
        self.requirements = []

    def full_transcript(self):
        return "\n".join(getattr(s, "text", "") for s in self.segments)


class _Recording:
    """A recording service whose processing pass fails part-way.

    It emits the same stage tokens the real one does (see
    RecordingService.process_session) before raising, because WHERE it
    failed is half of what the panel has to get right. A fake that
    raised immediately would let a fix pass that marks the wrong stage.
    """

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError("Diarization failed: CUDA out of memory")

    def is_recording(self):
        return False

    async def process_session(self, session=None):
        # Verbatim from recording_service.py:1262 and :1290.
        server.svc._record_status("__stage:transcribe:active__")
        server.svc._record_status(
            "__stage:transcribe:done____stage:diarize:active__")
        raise self.exc


class _Settings:
    """Only what process_full's guards read before the ML call."""
    is_configured = True


@pytest.fixture(autouse=True)
def _reset_pipeline():
    """Each test starts from a clean model, and leaves one behind."""
    server.svc.pipeline.reset()
    yield
    server.svc.pipeline.reset()


def _wire_a_failing_run(monkeypatch, exc=None):
    """Get process_full past its guards and up to the ML call, with a
    processing pass that raises. Everything stubbed here is a guard, not
    the behaviour under test."""
    session = _Session()
    monkeypatch.setattr(server.svc, "settings", _Settings(), raising=False)
    monkeypatch.setattr(server.svc, "load_settings", lambda: None, raising=False)
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None,
                        raising=False)
    monkeypatch.setattr(server.svc, "session_svc", _Sessions(session),
                        raising=False)
    monkeypatch.setattr(server.svc, "recording_svc", _Recording(exc),
                        raising=False)
    monkeypatch.setattr(server, "_raise_if_finalizing", lambda s: None,
                        raising=False)
    return session


def _mid_run(status="__stage:transcribe:done____stage:diarize:active__"):
    """Put the model where a real run would be when diarization blows
    up: transcription done, diarization running."""
    server.svc._begin_processing()
    server.svc._record_status(status)


def test_the_endpoint_marks_the_pipeline_failed(monkeypatch):
    """The regression, stated directly. process_full returns ok:False
    rather than raising, so nothing downstream can infer the failure —
    it has to be recorded here."""
    _wire_a_failing_run(monkeypatch)

    result = asyncio.run(server.process_full("S1", server.ProcessFullRequest()))

    assert result["ok"] is False
    payload = server.svc.pipeline.payload()
    assert payload["done"] is False, (
        "a failed run rendered as complete — the exact defect this "
        "release was supposed to end")
    assert payload["error"], "the failure must carry a reason for the panel"


def test_the_failed_stage_is_the_one_that_was_running(monkeypatch):
    """A red mark on the wrong stage sends the user to the wrong place.
    Diarization died, so diarization is what failed — and the stages
    after it never ran and must not claim they did."""
    _wire_a_failing_run(monkeypatch)
    asyncio.run(server.process_full("S1", server.ProcessFullRequest()))

    states = {s["key"]: s["state"]
              for s in server.svc.pipeline.payload()["stages"]}
    assert states["diarize"] == "failed"
    assert states["speakers"] == "pending"


def test_the_reason_reaches_the_user_not_just_the_log(monkeypatch):
    """`logger.exception` is not a user-visible signal. The message the
    panel shows has to name what went wrong."""
    _wire_a_failing_run(
        monkeypatch,
        RuntimeError("Diarization failed: check the audio file is 16kHz"))

    asyncio.run(server.process_full("S1", server.ProcessFullRequest()))
    assert "16kHz" in (server.svc.pipeline.error() or "")


def test_end_processing_never_overwrites_a_recorded_failure():
    """The mechanism that caused the bug. `_end_processing` completes
    the run when the counter reaches zero; it must respect a failure
    already recorded, or the `finally` silently repaints the panel
    green on its way out."""
    _mid_run()
    server.svc.pipeline.fail("Diarization failed")
    server.svc._end_processing()

    payload = server.svc.pipeline.payload()
    assert payload["done"] is False
    assert payload["error"] == "Diarization failed"


def test_a_clean_run_still_completes():
    """The guard must not be so eager that ordinary success stops
    reading as success."""
    _mid_run()
    server.svc._end_processing()

    payload = server.svc.pipeline.payload()
    assert payload["done"] is True
    assert payload["error"] is None


def test_a_new_run_clears_the_previous_failure():
    """A failed meeting must not paint the NEXT meeting red. `reset` on
    the 0->1 transition already does this; asserted so a future change
    to _begin_processing cannot quietly drop it."""
    _mid_run()
    server.svc.pipeline.fail("Diarization failed")
    server.svc._end_processing()

    server.svc._begin_processing()
    try:
        payload = server.svc.pipeline.payload()
        assert payload["error"] is None
        assert all(s["state"] == "pending" for s in payload["stages"])
    finally:
        server.svc._end_processing()


def test_auto_process_giving_up_is_recorded_as_a_failure(monkeypatch):
    """The path nobody watches. Auto-process retries and then gives up
    with `logger.error` and nothing else — the user is looking at a tab
    that says the meeting processed fine. Exhausting the retries is the
    moment that has to become visible."""
    monkeypatch.setattr(server, "_AUTO_PROCESS_RETRY_DELAYS", (), raising=False)

    async def _always_fails(session_id, req):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr(server, "process_full", _always_fails, raising=False)

    asyncio.run(server._auto_process_session("S1", "default", False))

    payload = server.svc.pipeline.payload()
    assert payload["done"] is False
    assert payload["error"], "auto-process failure left no user-visible trace"
