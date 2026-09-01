"""
The auto-indexer stays out of the export worker's way.

THE FIELD REPORT (2026-09-01)
-----------------------------
A user upgraded to v2.78.0 and reported meetings taking noticeably
longer to land in their Designated Folders than they used to. Their
Clients tab read "15 of 16 meetings copied — 1 still missing."

Nothing in the export path changed: ``git diff v2.76.0..v2.78.0`` over
export_worker.py / export_service.py / export_reconcile.py is empty.
What changed is that automatic Knowledge Folder indexing STARTED
WORKING. It was on by default in v2.77.0 and never ran (the ``.all`` /
``get_all`` typo); v2.78.0 fixed that, so a sweep began firing every 15
minutes for the first time on every install.

That sweep walks a client's Knowledge Folder. On the reporting install
— and it is the common shape, because the Knowledge Folder card offers
"Same as Designated Folder" as a one-click option — that folder is the
SAME Google Drive File Stream tree the export worker copies into. On a
streamed mount a ``stat()`` is a network round-trip and a read forces a
download, so the indexer and the exporter contend for one mount.

``_auto_index_busy`` already refused to sweep during recording or
processing, for exactly this reasoning applied to CPU. It did not know
about the export queue, so the one job whose whole design goal is
"never let a network folder stall anything" could be slowed by a
background task with no deadline at all.

The rule: exports win. A sweep deferred a few minutes costs nothing —
the documents are still there next tick — whereas an export is
something the user is watching a counter for.

WHY THESE TESTS DRIVE THE REAL WORKER
-------------------------------------
``pending_count`` has to mean "work the user is still waiting on",
which includes a job that is mid-copy and a job sitting in retry
backoff, not merely one sitting in the queue. Those distinctions only
exist in the real class, so the tests gate a real ``ExportWorker``'s
callback rather than asserting against a hand-built stub that would be
free to disagree with it.
"""

from __future__ import annotations

import threading
import time

from _app_import import import_app

import_app()
import server  # noqa: E402

from services.export_worker import ExportWorker  # noqa: E402

# Long enough to be reliable on a loaded CI runner, short enough that a
# genuine hang fails the test instead of the suite.
_TIMEOUT_S = 5.0


class _Recorder:
    def __init__(self, recording: bool = False) -> None:
        self._recording = recording

    def is_recording(self) -> bool:
        return self._recording


class _GatedWorker:
    """A real ExportWorker whose exports block until released, so a job
    can be observed while it is genuinely outstanding."""

    def __init__(self, fail_forever: bool = False) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self._fail = fail_forever
        self.worker = ExportWorker(do_export=self._export)

    def _export(self, session_id: str, copy_audio: bool) -> None:
        self.started.set()
        self.release.wait(_TIMEOUT_S)
        if self._fail:
            raise OSError("cloud mount unavailable")

    def wait_started(self) -> None:
        assert self.started.wait(_TIMEOUT_S), "export never started"

    def drain(self) -> None:
        """Let everything finish so the daemon thread isn't left holding
        a gate when the test ends."""
        self.release.set()
        deadline = time.monotonic() + _TIMEOUT_S
        while time.monotonic() < deadline:
            if self.worker.pending_count() == 0:
                return
            time.sleep(0.01)


def _idle(monkeypatch, *, exporter) -> None:
    """Nothing recording, nothing processing — so the only thing that
    can make the guard say 'busy' is the export backlog under test."""
    monkeypatch.setattr(server.svc, "recording_svc", _Recorder(False),
                        raising=False)
    monkeypatch.setattr(server, "_EXPORT_WORKER", exporter, raising=False)


def test_idle_with_no_exporter_is_not_busy(monkeypatch):
    """A missing worker must not read as pending work, or an install
    with no Designated Folder configured would never index at all."""
    _idle(monkeypatch, exporter=None)
    assert server._auto_index_busy() is False


def test_idle_with_an_empty_export_queue_is_not_busy(monkeypatch):
    gated = _GatedWorker()
    _idle(monkeypatch, exporter=gated.worker)
    assert server._auto_index_busy() is False
    gated.drain()


def test_an_in_flight_export_makes_the_indexer_stand_down(monkeypatch):
    """The regression itself. A copy the user is waiting on outranks a
    knowledge sweep that has no deadline."""
    gated = _GatedWorker()
    gated.worker.enqueue("SESSION-1", copy_audio=True)
    gated.wait_started()

    _idle(monkeypatch, exporter=gated.worker)
    try:
        assert server._auto_index_busy() is True
    finally:
        gated.drain()


def test_a_queued_export_behind_a_running_one_also_counts(monkeypatch):
    gated = _GatedWorker()
    gated.worker.enqueue("SESSION-1")
    gated.wait_started()
    gated.worker.enqueue("SESSION-2")

    try:
        assert gated.worker.pending_count() == 2
        _idle(monkeypatch, exporter=gated.worker)
        assert server._auto_index_busy() is True
    finally:
        gated.drain()


def test_the_backlog_clears_once_exports_finish(monkeypatch):
    """The guard must not latch. An install that exported once this
    morning has to keep indexing for the rest of the day."""
    gated = _GatedWorker()
    gated.worker.enqueue("SESSION-1")
    gated.wait_started()
    gated.drain()

    _idle(monkeypatch, exporter=gated.worker)
    assert gated.worker.pending_count() == 0
    assert server._auto_index_busy() is False


def test_a_session_in_retry_backoff_still_counts_as_outstanding():
    """The 'N of M copied' gap is a job in backoff, not one in the
    queue — it is scheduled on a timer and the queue is empty while it
    waits. Counting only queued items would report an idle worker with
    work outstanding, which is this repo's signature defect: a result
    you couldn't deliver rendering as no result pending."""
    gated = _GatedWorker(fail_forever=True)
    gated.worker.enqueue("SESSION-1")
    gated.wait_started()
    gated.release.set()  # let the attempt run and fail

    # First retry is scheduled 5s out; the queue is empty in between.
    deadline = time.monotonic() + _TIMEOUT_S
    saw_empty_queue = False
    while time.monotonic() < deadline:
        if gated.worker.queued_count() == 0:
            saw_empty_queue = True
            break
        time.sleep(0.01)

    assert saw_empty_queue, "expected the queue to empty during backoff"
    assert gated.worker.pending_count() == 1


def test_export_worker_coalesces_repeat_enqueues_in_the_count():
    """A backlog that inflated on repeat enqueues would hold the
    indexer off forever on a busy install."""
    gated = _GatedWorker()
    gated.worker.enqueue("SESSION-1")
    gated.wait_started()
    gated.worker.enqueue("SESSION-2")
    gated.worker.enqueue("SESSION-2", copy_audio=True)

    try:
        assert gated.worker.pending_count() == 2
    finally:
        gated.drain()


def test_a_broken_queue_reading_assumes_busy(monkeypatch):
    """Unable to tell => skip the sweep, the same safe direction the
    recording check already takes."""
    class _Exploding:
        def pending_count(self) -> int:
            raise RuntimeError("queue unreadable")

    _idle(monkeypatch, exporter=_Exploding())
    assert server._auto_index_busy() is True


def test_recording_still_wins_regardless_of_the_queue(monkeypatch):
    """The pre-existing guard is not weakened by the new one."""
    monkeypatch.setattr(server.svc, "recording_svc", _Recorder(True),
                        raising=False)
    monkeypatch.setattr(server, "_EXPORT_WORKER", None, raising=False)
    assert server._auto_index_busy() is True
