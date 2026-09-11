"""
Processing a meeting queues its export immediately.

THE REGRESSION
--------------
Exports used to happen the moment a meeting finished processing. They
stopped, and the reason is a missing line rather than a broken one.

Five places enqueue an export:

    patch_session          tagging a meeting to a client
    bulk_tag_sessions      tagging a back-catalogue
    process_session        the individual transcribe/diarize endpoint
    _run_extraction        each individual extractor
    summarize_session      the individual summarize endpoint

``process_full`` is not among them — and ``process_full`` is what
auto-process-after-stop calls (``_auto_process_session`` → 3979). It
runs transcribe, diarize and all five extractions in one pass and saves
once, which is exactly why it exists: five independent extractions each
doing load → set one field → save the whole session clobbered each
other, and the last writer won.

When that consolidation happened, the enqueue that lived on the
individual endpoints did not come with it. So the normal path — stop a
recording, let it auto-process — produces every artifact and tells the
export worker nothing.

The files then reach the Designated Folder only when something else
notices: the periodic reconciliation sweep (up to two minutes), the
startup reconcile, or Sync now. That is the difference between "synced
the minute it was processed" and "synced eventually", and eventually is
what was being observed.

WHY A TEST AND NOT JUST THE LINE
--------------------------------
This is the third time an export has failed to happen because a code
path did not know it was supposed to enqueue one — see
services/export_reconcile.py, whose docstring already says enumeration
is the wrong strategy and convergence is the right one. Reconciliation
is the safety net and stays. But the fast path has to work, and nothing
asserted that it did.
"""

from __future__ import annotations

import asyncio

import pytest

from _app_import import import_app

import_app()
import server  # noqa: E402


class _Segment:
    def __init__(self, text="hello"):
        self.text = text


class _Session:
    def __init__(self, session_id="S1", with_transcript=True):
        self.session_id = session_id
        self.audio_path = "(no such recording)"
        self.segments = [_Segment()] if with_transcript else []
        self.notes = ""
        self.screenshots = []
        self.summary = "a summary"
        self.action_items = []
        self.decisions = []
        self.requirements = []
        self.display_name = "Meeting S1"
        self.client = "Acme"

    def full_transcript(self):
        return "hello"


class _Sessions:
    def __init__(self, session):
        self._session = session
        self.saved = []

    def load_full(self, session_id):
        return self._session

    def save(self, session):
        self.saved.append(session)


class _Recording:
    """Processing succeeds and produces a transcript."""

    def __init__(self, session):
        self._session = session

    # A PROPERTY, matching RecordingService. Defining this as a
    # method is what let `svc.recording_svc.is_recording()` ship:
    # the fakes answered the call, the real object raised
    # "'bool' object is not callable", and the export sweep died
    # on every pass for four hours (field log 2026-09-10).
    @property
    def is_recording(self):
        return False

    async def process_session(self, session=None):
        return self._session


class _Settings:
    is_configured = True


class _Worker:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, session_id, copy_audio=False):
        self.enqueued.append(session_id)

    def pending_count(self):
        return 0

    def pending_since(self):
        return None


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    server.svc.pipeline.reset()
    yield
    server.svc.pipeline.reset()


def _wire(monkeypatch, session=None, extractions_ok=True):
    """process_full, up to and past the extraction step, with a fake
    export worker watching."""
    session = session or _Session()
    worker = _Worker()
    monkeypatch.setattr(server.svc, "settings", _Settings(), raising=False)
    monkeypatch.setattr(server.svc, "load_settings", lambda: None, raising=False)
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None,
                        raising=False)
    monkeypatch.setattr(server.svc, "session_svc", _Sessions(session),
                        raising=False)
    monkeypatch.setattr(server.svc, "recording_svc", _Recording(session),
                        raising=False)
    monkeypatch.setattr(server, "_raise_if_finalizing", lambda s: None,
                        raising=False)
    monkeypatch.setattr(server, "_EXPORT_WORKER", worker, raising=False)
    # The extraction half needs an LLM; short-circuit it so the test is
    # about the export, not about Claude.
    async def _no_extractions(*a, **kw):
        return {}
    monkeypatch.setattr(server, "_extract_and_save", _no_extractions,
                        raising=False)
    return session, worker


def test_a_processed_meeting_is_queued_for_export_immediately(monkeypatch):
    """The regression, stated directly. Without this the meeting waits
    for the reconciliation sweep — up to two minutes — before anything
    reaches the Designated Folder."""
    session, worker = _wire(monkeypatch)

    asyncio.run(server.process_full("S1", server.ProcessFullRequest()))

    assert worker.enqueued == ["S1"], (
        "processing finished and the export worker was never told; the "
        "files wait for the periodic sweep instead of syncing now")


def test_it_is_queued_once_not_once_per_artifact(monkeypatch):
    """process_full exists BECAUSE five separate saves clobbered each
    other. Five separate exports would re-copy the same files five
    times over a network folder for no gain."""
    session, worker = _wire(monkeypatch)
    asyncio.run(server.process_full("S1", server.ProcessFullRequest()))
    assert len(worker.enqueued) == 1


def test_a_run_that_produced_nothing_is_not_queued(monkeypatch):
    """A session with no transcript owes its folder nothing. Queueing it
    burns a job and — worse — counts as a pending export, which holds
    the knowledge indexer off for no reason."""
    session, worker = _wire(monkeypatch, session=_Session(with_transcript=False))

    asyncio.run(server.process_full("S1", server.ProcessFullRequest()))

    assert worker.enqueued == []


def test_a_failed_run_is_not_queued(monkeypatch):
    """Nothing was produced. An export attempt would find nothing to
    copy and retry three times on a cloud mount to discover that.

    The session starts with no transcript, which is what makes
    ``process_session`` run at all — a session that already has
    segments takes the "skipped (already processed)" branch and never
    reaches the transcriber, so a transcriber that raises would prove
    nothing.
    """
    session, worker = _wire(monkeypatch,
                            session=_Session(with_transcript=False))

    class _Failing(_Recording):
        async def process_session(self, session=None):
            raise RuntimeError("Diarization failed")

    monkeypatch.setattr(server.svc, "recording_svc", _Failing(session),
                        raising=False)
    result = asyncio.run(server.process_full("S1", server.ProcessFullRequest()))

    assert result["ok"] is False, (
        "the transcriber raised; this run did not succeed")
    assert worker.enqueued == []


def test_the_export_is_queued_after_the_session_is_saved(monkeypatch):
    """Ordering matters: the worker re-loads the session from disk, so
    enqueueing before the save would export the previous contents and
    look like a partial sync."""
    session, worker = _wire(monkeypatch)
    order = []

    sessions = server.svc.session_svc
    real_save = sessions.save
    def _tracking_save(s):
        order.append("save")
        return real_save(s)
    monkeypatch.setattr(sessions, "save", _tracking_save, raising=False)

    real_enqueue = worker.enqueue
    def _tracking_enqueue(sid, copy_audio=False):
        order.append("enqueue")
        return real_enqueue(sid, copy_audio)
    monkeypatch.setattr(worker, "enqueue", _tracking_enqueue, raising=False)

    asyncio.run(server.process_full("S1", server.ProcessFullRequest()))

    assert "enqueue" in order, "never enqueued"
    assert order.index("save") < order.index("enqueue"), (
        f"enqueued before saving ({order}); the worker re-loads from disk "
        f"and would copy the pre-save contents")


def test_an_export_failure_does_not_fail_the_processing_run(monkeypatch):
    """Transcription and extraction succeeded. A queueing problem must
    not turn a completed meeting into a failed one — the whole point of
    the background worker is that the network folder never blocks the
    pipeline."""
    session, worker = _wire(monkeypatch)

    def _boom(sid, copy_audio=False):
        raise RuntimeError("worker is gone")
    monkeypatch.setattr(worker, "enqueue", _boom, raising=False)

    result = asyncio.run(server.process_full("S1", server.ProcessFullRequest()))
    assert result["ok"] is True


def test_a_reprocess_that_skips_the_llm_calls_still_queues_the_export(
        monkeypatch):
    """Unchanged inputs mean the ARTIFACTS don't need regenerating. It
    does not mean they reached the Designated Folder — an export that
    exhausted its retries, a folder that was offline at the time, or a
    client tag added afterwards all leave a session owing files it
    already has.

    Reprocessing is what someone does when a meeting looks wrong in the
    folder, so this branch is exactly the one that must not decide there
    is nothing to do."""
    from core.prompt_version import extraction_fingerprint

    session = _Session()
    session.notes = ""
    session.extraction_fingerprint = extraction_fingerprint(
        session.full_transcript(), "", "General")
    session, worker = _wire(monkeypatch, session=session)

    result = asyncio.run(server.process_full("S1", server.ProcessFullRequest()))

    assert result.get("skipped") is True, (
        "this test is meaningless unless the extraction was actually "
        "skipped; the fingerprint no longer matches")
    assert worker.enqueued == ["S1"]
