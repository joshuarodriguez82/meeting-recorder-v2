"""Cloud Mirror export worker — folder resolution + background retries.

The invariant under test (2026-07-09 Drive-stall incident): network
copies happen on a background thread with retries and can never block
or crash the caller. Folder resolution: explicit Designated Folder →
<mirror_root>/<client> → <mirror_root>/Unfiled → None.
"""

import threading
import time
from pathlib import Path

import services.export_worker as ew
from services.export_worker import (
    ExportWorker,
    resolve_export_folder,
    sanitize_folder_name,
)


# ── folder resolution ────────────────────────────────────────────────

def test_explicit_designated_folder_wins():
    assert resolve_export_folder(
        r"D:\Clients\ACME", "ACME", r"G:\My Drive\MRv2") == r"D:\Clients\ACME"


def test_mirror_root_plus_client():
    got = resolve_export_folder("", "ACME Corp", r"G:\My Drive\MRv2")
    assert got == str(Path(r"G:\My Drive\MRv2") / "ACME Corp")


def test_mirror_root_no_client_goes_to_unfiled():
    got = resolve_export_folder("", "", r"G:\My Drive\MRv2")
    assert got == str(Path(r"G:\My Drive\MRv2") / "Unfiled")


def test_no_root_no_folder_means_no_export():
    assert resolve_export_folder("", "ACME", "") is None
    assert resolve_export_folder("", "", "   ") is None


def test_client_name_sanitized_for_path_use():
    # Windows-reserved characters and trailing dots can't survive as a
    # folder component; slashes especially must not escape the root.
    assert sanitize_folder_name("ACME / TYR: Phase 2?") == "ACME - TYR- Phase 2-"
    assert sanitize_folder_name("Trailing dots...") == "Trailing dots"
    assert sanitize_folder_name("") == "Unfiled"
    got = resolve_export_folder("", "A/B\\C", "/root")
    assert "/B" not in got.replace("A-B-C", "")  # no path traversal


# ── background worker ────────────────────────────────────────────────

def test_worker_runs_job_off_caller_thread():
    ran = threading.Event()
    seen = {}

    def do_export(session_id, copy_audio):
        seen["args"] = (session_id, copy_audio, threading.current_thread().name)
        ran.set()

    w = ExportWorker(do_export)
    w.enqueue("ABC123", copy_audio=True)
    assert ran.wait(timeout=5.0), "export job never ran"
    sid, copy_audio, thread_name = seen["args"]
    assert (sid, copy_audio) == ("ABC123", True)
    assert thread_name == "export-worker"  # not the caller's thread


def test_worker_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(ew, "_RETRY_DELAYS_S", (0.01, 0.01, 0.01))
    done = threading.Event()
    attempts = []

    def flaky(session_id, copy_audio):
        attempts.append(session_id)
        if len(attempts) < 3:
            raise OSError("cloud mount briefly unavailable")
        done.set()

    w = ExportWorker(flaky)
    w.enqueue("RETRY1")
    assert done.wait(timeout=5.0), "job never succeeded after retries"
    assert len(attempts) == 3


def test_worker_gives_up_after_schedule_without_raising(monkeypatch):
    monkeypatch.setattr(ew, "_RETRY_DELAYS_S", (0.01,))
    attempts = []

    def always_fails(session_id, copy_audio):
        attempts.append(1)
        raise OSError("permanently offline")

    w = ExportWorker(always_fails)
    w.enqueue("DOOMED")
    deadline = time.monotonic() + 5.0
    while len(attempts) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)  # give it a beat to (incorrectly) try again
    assert len(attempts) == 2  # initial + one retry, then gave up


def test_failing_job_does_not_block_others(monkeypatch):
    """A failing job's backoff must NOT be an on-thread sleep — other
    sessions' exports have to keep draining while it waits to retry."""
    monkeypatch.setattr(ew, "_RETRY_DELAYS_S", (10.0,))  # long, on purpose
    good_ran = threading.Event()

    def do_export(session_id, copy_audio):
        if session_id == "BAD":
            raise OSError("dead mount")
        if session_id == "GOOD":
            good_ran.set()

    w = ExportWorker(do_export)
    w.enqueue("BAD")    # fails, schedules a retry 10s out (off-thread)
    w.enqueue("GOOD")   # must run promptly, not wait behind BAD's backoff
    assert good_ran.wait(timeout=3.0), "a failing job head-of-line-blocked others"


def test_enqueue_during_run_is_not_dropped(monkeypatch):
    """The field bug: a re-enqueue while a job is executing must schedule
    a fresh run (newer artifacts), not be silently coalesced away."""
    first_started = threading.Event()
    release_first = threading.Event()
    runs = []

    def do_export(session_id, copy_audio):
        runs.append(session_id)
        if len(runs) == 1:
            first_started.set()
            release_first.wait(timeout=5.0)  # hold the worker inside job 1

    w = ExportWorker(do_export)
    w.enqueue("X")
    assert first_started.wait(timeout=3.0)
    w.enqueue("X")            # arrives WHILE job 1 is running
    release_first.set()
    deadline = time.monotonic() + 5.0
    while len(runs) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(runs) == 2, "re-enqueue during a running job was dropped"


def test_enqueue_coalesces_while_queued_and_ors_copy_audio():
    gate = threading.Event()
    runs = []

    def slow_export(session_id, copy_audio):
        gate.wait(timeout=5.0)
        runs.append((session_id, copy_audio))

    w = ExportWorker(slow_export)
    # First job holds the worker; the next three all target the same
    # session while it's still QUEUED → coalesced into one, with
    # copy_audio OR'd to True.
    w.enqueue("HOLD", copy_audio=False)
    w.enqueue("DUP", copy_audio=False)
    w.enqueue("DUP", copy_audio=False)
    w.enqueue("DUP", copy_audio=True)
    gate.set()
    deadline = time.monotonic() + 5.0
    while len(runs) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)
    dup_runs = [r for r in runs if r[0] == "DUP"]
    assert len(dup_runs) == 1              # three enqueues → one job
    assert dup_runs[0][1] is True          # copy_audio OR'd in


def test_sanitize_handles_windows_reserved_names():
    from services.export_worker import sanitize_folder_name
    assert sanitize_folder_name("CON") == "CON_"
    assert sanitize_folder_name("com1") == "com1_"
    assert sanitize_folder_name("NUL.txt") == "NUL.txt_"
    assert sanitize_folder_name("Contoso") == "Contoso"   # not reserved
