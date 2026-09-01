"""
The periodic export sweep actually enqueues the missing sessions.

Companion to test_export_sweep.py, which covers WHICH sessions a pass
considers. This covers what the pass DOES with them — the half that
v2.77.0's auto-indexer got wrong, where fifteen tests of the scheduling
all passed while the body did nothing at all.

The bug being closed (field report 2026-09-01): a meeting recorded two
hours earlier had not reached its Designated Folder and nothing would
ever try again. Exports are enqueued on mutation and retried three
times over ~2.5 minutes, then dropped; reconciliation would have caught
that but only ran at startup and on demand.

There is a second failure this file pins, which is subtler and was
introduced by the fix itself. The auto-indexer now stands down while
exports are outstanding. A session that can NEVER export — a source
file deleted, a folder pointing somewhere unwritable — would be
re-enqueued by every sweep, keeping the backlog permanently non-zero
and starving knowledge indexing forever. One background job must not be
able to permanently disable another, so deference to the exporter is
bounded in time.
"""

from __future__ import annotations

import time

from _app_import import import_app

import_app()
import server  # noqa: E402

from services.export_worker import ExportWorker  # noqa: E402


class _Sessions:
    def __init__(self, rows):
        self._rows = rows

    def list_sessions(self):
        return list(self._rows)


class _Recorder:
    def __init__(self, recording: bool = False):
        self._recording = recording

    def is_recording(self) -> bool:
        return self._recording


class _CountingWorker:
    """Records enqueues without doing any I/O."""

    def __init__(self):
        self.enqueued: list[str] = []

    def enqueue(self, session_id: str, copy_audio: bool = False) -> None:
        self.enqueued.append(session_id)

    def pending_count(self) -> int:
        return 0


def _row(sid: str, client: str = "Acme", **extra) -> dict:
    row = {
        "session_id": sid,
        "display_name": f"Meeting {sid}",
        "client": client,
        "ended_at": "2026-09-01T10:00:00",
        "has_transcript": True,
    }
    row.update(extra)
    return row


# Never touched: missing_artifacts is stubbed in every test here, so
# this only has to be a non-empty string that reads as a folder.
_FAKE_FOLDER = "(designated-folder)"


def _wire(monkeypatch, rows, *, folder=_FAKE_FOLDER,
          missing=("transcript_Meeting S1.txt",)):
    worker = _CountingWorker()
    monkeypatch.setattr(server.svc, "session_svc", _Sessions(rows),
                        raising=False)
    monkeypatch.setattr(server, "_EXPORT_WORKER", worker, raising=False)
    monkeypatch.setattr(server, "_export_folder_for_client",
                        lambda client: folder if client else "")
    monkeypatch.setattr(server.export_reconcile, "missing_artifacts",
                        lambda row, f: list(missing))
    monkeypatch.setattr(server.export_reconcile, "expected_artifacts",
                        lambda row: ["transcript_x.txt"])
    return worker


def test_a_session_missing_its_artifacts_is_re_enqueued(monkeypatch):
    """The reported bug, stated as a test: the sweep is what makes a
    dropped export a delay rather than a permanent hole."""
    worker = _wire(monkeypatch, [_row("S1")])
    assert server._sweep_recent_exports() == 1
    assert worker.enqueued == ["S1"]


def test_a_fully_mirrored_session_is_left_alone(monkeypatch):
    """Idempotence. A sweep that re-copied everything every two minutes
    would be a worse stat storm than the one it replaced."""
    worker = _wire(monkeypatch, [_row("S1")], missing=())
    assert server._sweep_recent_exports() == 0
    assert worker.enqueued == []


def test_a_session_with_nothing_processed_yet_owes_nothing(monkeypatch):
    """A just-stopped recording has no transcript to copy. Enqueueing it
    would burn a job and, worse, count as a pending export that holds
    the indexer off."""
    worker = _wire(monkeypatch, [_row("S1")])
    monkeypatch.setattr(server.export_reconcile, "expected_artifacts",
                        lambda row: [])
    assert server._sweep_recent_exports() == 0
    assert worker.enqueued == []


def test_an_untagged_session_with_no_folder_is_skipped(monkeypatch):
    """No client tag and no cloud mirror root => nowhere to copy to.
    Not a failure, just nothing owed."""
    worker = _wire(monkeypatch, [_row("S1", client="")])
    assert server._sweep_recent_exports() == 0
    assert worker.enqueued == []


def test_the_folder_is_resolved_once_per_client(monkeypatch):
    """Resolution reads client config; doing it per session would make a
    20-session sweep 20 config reads for no new information."""
    rows = [_row(f"S{i}", client="Acme") for i in range(5)]
    worker = _wire(monkeypatch, rows)
    calls: list[str] = []

    def _folder(client):
        calls.append(client)
        return _FAKE_FOLDER

    monkeypatch.setattr(server, "_export_folder_for_client", _folder)
    assert server._sweep_recent_exports() == 5
    assert calls == ["Acme"]
    assert len(worker.enqueued) == 5


def test_one_unreadable_session_does_not_abandon_the_rest(monkeypatch):
    """A cloud mount that throws on one stat must not cost every other
    session its copy. This is the same isolation list_sessions() learned
    the hard way in the 2026-08-07 report."""
    rows = [_row("BAD"), _row("GOOD")]
    worker = _wire(monkeypatch, rows)

    def _missing(row, folder):
        if row["session_id"] == "BAD":
            raise OSError("stream hiccup")
        return ["transcript_x.txt"]

    monkeypatch.setattr(server.export_reconcile, "missing_artifacts",
                        _missing)
    assert server._sweep_recent_exports() == 1
    assert worker.enqueued == ["GOOD"]


def test_the_sweep_survives_an_unreadable_session_list(monkeypatch):
    """Returns 0 rather than raising into the loop, which would log a
    warning and try again on the next tick either way — but a sweep that
    raises past its own guard is one nobody can reason about."""
    class _Exploding:
        def list_sessions(self):
            raise OSError("root unavailable")

    monkeypatch.setattr(server.svc, "session_svc", _Exploding(),
                        raising=False)
    assert server._sweep_recent_exports() == 0


# ── The starvation guard ────────────────────────────────────────────

def test_a_wedged_export_backlog_eventually_releases_the_indexer(
        monkeypatch):
    """A session that can never export would otherwise keep the backlog
    non-zero forever and permanently disable knowledge indexing. One
    background job must not be able to switch another one off."""
    class _StuckWorker:
        def pending_count(self) -> int:
            return 1

        def pending_since(self) -> float:
            # Outstanding since well past the deference ceiling.
            return time.monotonic() - (server._EXPORT_DEFERENCE_MAX_S + 60)

    monkeypatch.setattr(server.svc, "recording_svc", _Recorder(False),
                        raising=False)
    monkeypatch.setattr(server, "_EXPORT_WORKER", _StuckWorker(),
                        raising=False)
    assert server._auto_index_busy() is False


def test_a_fresh_export_backlog_still_defers_the_indexer(monkeypatch):
    """The ceiling must not weaken the normal case, which is the whole
    reason the deference exists."""
    class _BusyWorker:
        def pending_count(self) -> int:
            return 1

        def pending_since(self) -> float:
            return time.monotonic()

    monkeypatch.setattr(server.svc, "recording_svc", _Recorder(False),
                        raising=False)
    monkeypatch.setattr(server, "_EXPORT_WORKER", _BusyWorker(),
                        raising=False)
    assert server._auto_index_busy() is True


def test_the_worker_reports_when_its_backlog_started():
    """Asserted against the real worker rather than a stub, so the
    accessor the guard depends on cannot drift from it."""
    worker = ExportWorker(do_export=lambda sid, audio: None)
    assert worker.pending_since() is None

    import threading
    release = threading.Event()
    started = threading.Event()

    def _slow(session_id, copy_audio):
        started.set()
        release.wait(5.0)

    worker = ExportWorker(do_export=_slow)
    worker.enqueue("S1")
    assert started.wait(5.0)
    since = worker.pending_since()
    assert since is not None and since <= time.monotonic()

    release.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if worker.pending_count() == 0:
            break
        time.sleep(0.01)
    # Cleared once the backlog drains, so the next backlog is timed from
    # its own start and not from an hours-old one.
    assert worker.pending_since() is None
