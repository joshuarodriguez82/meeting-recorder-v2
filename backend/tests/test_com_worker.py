"""
Tests for utils/com_worker.py — the single-threaded COM apartment owner.

Context: two Windows subsystems (pycaw in core/audio_format_inspector.py,
and pywin32/Outlook in services/_calendar_outlook.py) used to run COM
calls on the shared default asyncio.to_thread pool. One subsystem would
implicitly initialize a COM apartment on a pool thread; the other would
explicitly tear that apartment back down (Co-Uninitialize) on the SAME
pool thread once reused, out from under still-alive COM proxy objects
from the first subsystem. The next garbage-collection pass would call
IUnknown::Release() on those proxies into a dead apartment ->
STATUS_ACCESS_VIOLATION. com_worker.py fixes this by giving COM exactly
one dedicated thread for the whole process, initializing its apartment
once, and never tearing it down.

None of these tests import pythoncom/pywin32/pycaw/comtypes — the CI
venv doesn't have them. We simulate the "pythoncom is available, route
to the dedicated worker thread" branch by monkeypatching the module's
own `_PYTHONCOM_AVAILABLE` flag; the worker thread itself tolerates a
missing pythoncom module fine (its CoInitialize attempt is wrapped in a
try/except), so this exercises the real threading/queueing machinery
without needing real COM anywhere.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import com_worker as cw  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Fallback path (no pythoncom — macOS / Linux / this CI venv)
# ──────────────────────────────────────────────────────────────────────

def test_fallback_runs_fn_on_caller_thread_and_returns_value(monkeypatch):
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", False)
    caller_ident = threading.get_ident()
    seen = {}

    def fn(a, b, c=0):
        seen["ident"] = threading.get_ident()
        return a + b + c

    result = cw.run_com(fn, 1, 2, c=3)

    assert result == 6
    assert seen["ident"] == caller_ident, (
        "fallback path must execute fn on the CURRENT thread, not spawn one"
    )


def test_fallback_propagates_exceptions(monkeypatch):
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", False)

    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        cw.run_com(boom)


# ──────────────────────────────────────────────────────────────────────
# Worker-thread path (simulated "Windows with pythoncom" branch)
# ──────────────────────────────────────────────────────────────────────

def test_run_com_executes_callable_and_returns_value(monkeypatch):
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", True)
    assert cw.run_com(lambda x, y: x * y, 6, 7) == 42


def test_run_com_reraises_exceptions_in_caller(monkeypatch):
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", True)

    def boom():
        raise RuntimeError("com blew up")

    with pytest.raises(RuntimeError, match="com blew up"):
        cw.run_com(boom)


def test_all_calls_run_on_one_thread_that_is_not_the_caller(monkeypatch):
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", True)
    caller_ident = threading.get_ident()

    idents = [cw.run_com(threading.get_ident) for _ in range(5)]

    assert len(set(idents)) == 1, "every call should land on the SAME thread"
    assert idents[0] != caller_ident, "that thread must not be the caller's"
    # is_com_worker_thread() should agree from inside a submitted job.
    assert cw.run_com(cw.is_com_worker_thread) is True
    assert cw.is_com_worker_thread() is False  # false from the test thread


def test_concurrent_callers_are_serialized_onto_the_one_worker_thread(monkeypatch):
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", True)

    idents: list[int] = []
    lock = threading.Lock()

    def job():
        ident = cw.run_com(threading.get_ident)
        with lock:
            idents.append(ident)

    threads = [threading.Thread(target=job) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(idents) == 10
    assert len(set(idents)) == 1, "all concurrent callers must be funneled onto one thread"


def test_timeout_raises_instead_of_hanging_forever(monkeypatch):
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", True)

    def slow():
        time.sleep(0.5)
        return "done"

    with pytest.raises(cw.ComCallTimeout):
        cw.run_com(slow, timeout=0.05)


# ──────────────────────────────────────────────────────────────────────
# v2.25.1 regression: apartment LIFETIME was not enough — cyclic
# garbage created inside a job must be collected on the WORKER thread,
# not wherever Python's GC happens to run next. See com_worker.py's
# module docstring ("THE FOLLOW-UP FIX") and
# _collect_cyclic_garbage_after_job's docstring for the full mechanism.
# ──────────────────────────────────────────────────────────────────────

def test_job_cyclic_garbage_is_collected_on_worker_thread_not_caller(monkeypatch):
    """This is THE regression test for the bug: a job that creates a
    reference cycle (unreachable except via the cycle itself, so
    ordinary refcounting can never free it) must have that cycle
    collected — __del__ actually invoked — on the COM worker thread,
    before run_com() returns to the caller. If this ever starts
    passing with the collected ident equal to the caller's thread (or
    with no collection happening at all within this test), the
    apartment-affinity crash is back."""
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", True)
    caller_ident = threading.get_ident()
    collected_idents: list[int] = []

    class _Cyclic:
        def __del__(self):
            collected_idents.append(threading.get_ident())

    def make_unreachable_cycle():
        a = _Cyclic()
        b = _Cyclic()
        a.other = b
        b.other = a
        # `a`/`b` go out of scope when this function returns. Because
        # they only reference EACH OTHER, refcounting alone can never
        # free them — only a cyclic-GC pass can. That pass is exactly
        # what com_worker.py's _run() triggers, on this same thread,
        # right after this job completes.
        return None

    cw.run_com(make_unreachable_cycle)

    assert collected_idents, (
        "cyclic garbage from the job was not collected at all before "
        "run_com() returned — the worker-thread gc.collect() call is "
        "missing or not running"
    )
    worker_ident = cw.run_com(threading.get_ident)
    assert all(ident == worker_ident for ident in collected_idents), (
        "cyclic garbage was collected on a thread other than the COM "
        "worker thread — this is the exact apartment-affinity bug: "
        "Release() would be called from outside the owning STA"
    )
    assert all(ident != caller_ident for ident in collected_idents), (
        "cyclic garbage was collected on the CALLER's thread, not the "
        "worker thread"
    )


def test_gc_collect_failure_does_not_break_job_result(monkeypatch):
    """A broken/raising gc.collect() call must never take the job's
    actual result down with it — it's a best-effort safety net, not
    something that should turn a successful COM call into a failure."""
    monkeypatch.setattr(cw, "_PYTHONCOM_AVAILABLE", True)

    def boom():
        raise RuntimeError("gc exploded")

    monkeypatch.setattr(cw, "_collect_cyclic_garbage_after_job", boom)

    assert cw.run_com(lambda: 123) == 123

    # Exceptions from the job itself must still propagate normally even
    # while gc.collect() is broken.
    def raise_value_error():
        raise ValueError("job failed")

    with pytest.raises(ValueError, match="job failed"):
        cw.run_com(raise_value_error)


# ──────────────────────────────────────────────────────────────────────
# Regression: the invariant that caused the crash must never come back.
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("relpath", [
    "services/_calendar_outlook.py",
    "utils/startup_shortcut.py",
])
def test_no_apartment_teardown_call_in_com_touching_modules(relpath):
    """
    These modules used to call pythoncom's apartment-teardown API after
    every Outlook/COM call. That is exactly what caused the crash: it
    tore down the process's COM apartment while pycaw proxy objects from
    core/audio_format_inspector.py were still alive on a shared thread
    pool thread, and the next GC pass segfaulted trying to Release()
    them into a dead apartment.

    All COM work now goes through utils/com_worker.py's single
    long-lived, never-uninitialized apartment. If the teardown call
    reappears in either of these files, someone has reintroduced the
    exact bug this change fixes.

    The banned token is built from two halves at runtime (rather than
    spelled out literally in this file) so that a whole-tree `grep` for
    the literal API name — the sanity check this test exists to
    automate — doesn't just match this assertion.
    """
    banned_token = "Co" + "Uninitialize"
    backend_root = Path(__file__).resolve().parents[1]
    source = (backend_root / relpath).read_text(encoding="utf-8")
    assert banned_token not in source, (
        f"{relpath} must not call the COM apartment-teardown API — see "
        f"utils/com_worker.py for why the apartment is deliberately "
        f"never torn down."
    )
