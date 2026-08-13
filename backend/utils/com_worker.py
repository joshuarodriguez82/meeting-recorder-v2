"""
A process-wide, lazily-started, SINGLE-THREADED worker that owns exactly
one COM apartment for the entire life of the process.

WHY THIS EXISTS
----------------
Two subsystems in this codebase talk to COM on Windows:

  1. core/audio_format_inspector.py — pycaw (built on comtypes) reads
     WASAPI mix formats. comtypes implicitly Co-Initializes whatever
     thread it happens to run on; it never explicitly uninitializes.
  2. services/_calendar_outlook.py (+ _follow_up_email_outlook.py,
     utils/startup_shortcut.py) — pywin32 (win32com/pythoncom) talks to
     Outlook, and used to call pythoncom's Co-Initialize / Co-Uninitialize
     pair around every call.

Before this module existed, BOTH subsystems ran via the shared default
`asyncio.to_thread` ThreadPoolExecutor. Any pool thread could be reused
for either job. That meant: pycaw would implicitly initialize a COM
apartment on pool thread #3 and hand back live comtypes proxy objects
(kept alive as Python objects, e.g. tucked into a cache or just pending
garbage collection); later, the SAME pool thread #3 could be reused for
an Outlook call, which explicitly tore its apartment down (Co-Uninitialize)
in a `finally` block, pulling the apartment out from under the still-alive pycaw
proxies. Whenever the garbage collector next ran __del__ on one of those
proxies — often much later, in a completely unrelated frame — it called
IUnknown::Release() into a dead apartment, and the process died with
STATUS_ACCESS_VIOLATION (0xC0000005). This was confirmed from a real
faulthandler crash log: every crash's top frames were
comtypes/_post_coinit/unknwn.py Release()/__del__() during a GC pass,
with a different, unrelated frame underneath (whatever code happened to
trigger that GC cycle) — a classic "the traceback tells you where the
crash landed, not where the bug is" signature.

THE FIX
-------
Give COM exactly ONE thread for the whole process, initialize its
apartment ONCE, and never tear it down. All COM work — pycaw and
Outlook alike — is funneled through `run_com()`, which posts the
callable onto that thread's work queue and blocks the caller for the
result. Because only one thread ever touches COM, and that thread's
apartment is never uninitialized, there is no way for a live proxy to
outlive its apartment.

Deliberately leaking the apartment for the process lifetime is
CORRECT here, not an oversight — tearing the apartment down
(Co-Uninitialize) is exactly what causes the crash. Do not "fix" this
by adding an apartment-teardown call back in, an atexit hook, or a
context manager that tears the apartment down.
The apartment must outlive every COM proxy object that might still be
sitting in a variable, a cache, or waiting for the next GC cycle
anywhere in the process. The OS reclaims it for free on process exit.

Callers must never let a COM object (anything from pycaw, comtypes, or
win32com) escape the callable passed to `run_com`. Extract plain data
(dicts/strings/ints/dataclasses) INSIDE the callable and return only
that — otherwise you've just moved the crash instead of fixing it.

CROSS-PLATFORM / CI SAFETY
---------------------------
`pythoncom` doesn't exist on macOS/Linux or in the CI venv (which has
only numpy/scipy/soundfile/pytest/fastapi). The import is guarded; when
unavailable, `run_com` simply calls `fn` directly on the CURRENT thread
— there's no COM to protect on those platforms anyway.

THE FOLLOW-UP FIX (v2.25.x): APARTMENT LIFETIME WAS NOT ENOUGH
----------------------------------------------------------------
The fix above (never-uninitialize apartment, all work funneled through
one thread) shipped in v2.23.2 and did NOT stop the crash. A v2.25.0
field crash showed the identical STATUS_ACCESS_VIOLATION fault —
comtypes `Release()` -> `__del__` during a GC pass — with this worker
running correctly and idle on its queue at the time. The backend log
showed a pycaw call (`get_device_mix_format`) completing ~4 seconds
before the crash; the crash itself happened inside
`session_service.list_sessions`, a completely unrelated code path on a
completely unrelated (asyncio pool) thread.

The missing piece is apartment AFFINITY, not lifetime. COM interface
pointers created in this worker's single-threaded apartment (STA) may
only have `Release()` called from THIS thread. comtypes proxy objects
participate in reference cycles (e.g. an interface pointer object and
the POINTER instance wrapping it reference each other), so CPython's
plain refcounting does NOT free them when `job.fn`'s local variables
go out of scope at the end of a submitted job — they are handed to the
cyclic garbage collector instead, and sit there until some future
`gc.collect()` runs, on WHATEVER THREAD happens to trigger it. In the
v2.25.0 crash that was a pool thread parsing JSON inside
`session_service.list_sessions`, four seconds after the actual pycaw
call. That thread is not the STA that owns the proxy's apartment, so
`Release()` faults.

The fix (`_collect_cyclic_garbage_after_job`, called from `_run()`
below) forces that collection to happen synchronously, on THIS worker
thread, immediately after each job completes — converting "collected
later, on a random thread" into "collected now, in the owning
apartment." See that function's docstring for the full reasoning; do
not remove the call as a "pointless gc.collect()" without re-reading
it first.
"""

from __future__ import annotations

import gc
import queue
import threading
from typing import Any, Callable, TypeVar

from utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

try:
    import pythoncom  # type: ignore
    _PYTHONCOM_AVAILABLE = True
except Exception:
    pythoncom = None  # type: ignore
    _PYTHONCOM_AVAILABLE = False


class ComCallTimeout(Exception):
    """Raised by run_com() when a COM call doesn't finish within `timeout`.

    The call may still be running on the worker thread after this is
    raised (there is no safe way to interrupt a wedged COM call) — this
    just stops the caller from blocking forever. If Outlook is truly
    hung, subsequent calls will queue up behind it and eventually also
    time out; that's a pre-existing Outlook-hang risk, not something
    this module can fix.
    """


class _Job:
    __slots__ = ("fn", "args", "kwargs", "result_q")

    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.result_q: "queue.Queue[tuple]" = queue.Queue(maxsize=1)


def _collect_cyclic_garbage_after_job() -> None:
    """Force a cyclic-GC pass, synchronously, on the COM worker thread,
    immediately after each submitted job finishes.

    DO NOT REMOVE THIS AS "a pointless gc.collect()". This is the fix
    for a real, confirmed field crash — read the rest of this
    docstring, and the module docstring's "APARTMENT LIFETIME WAS NOT
    ENOUGH" section, before touching it.

    THE PROBLEM THIS SOLVES
    ------------------------
    `pythoncom.CoInitialize()` on this thread (see `_run` above)
    creates a single-threaded apartment (STA). Every COM interface
    pointer minted while that apartment is current — every pycaw
    proxy, every Outlook COM object — is APARTMENT-AFFINE:
    `IUnknown::Release()` on it may only be called from the thread
    that owns the apartment it was created in. Call it from any other
    thread and you get STATUS_ACCESS_VIOLATION (0xC0000005).

    comtypes proxy objects (what pycaw is built on) commonly hold
    reference cycles internally — e.g. the low-level interface pointer
    and the higher-level POINTER() wrapper around it reference each
    other. CPython's ordinary refcounting GC does NOT free objects
    that are part of a cycle, even when every external reference to
    them is gone (e.g. when `job.fn`'s local variables go out of scope
    at the end of a call). Those objects are instead handed to the
    CYCLIC garbage collector, which only runs periodically (allocation-
    count thresholds) or when something explicitly calls
    `gc.collect()` — on whatever thread happens to be running Python
    bytecode at that moment. `gc.collect()` calls `__del__` (and thus
    `Release()`) on the objects it frees, IN THE CALLING THREAD.

    So: mint a comtypes proxy on this worker thread, let it become
    cyclic garbage, and if nothing collects it here, it eventually gets
    swept up by an unrelated `gc.collect()` triggered from a
    completely different thread — and that thread's `Release()` call
    faults, because it isn't the STA that owns the object. This is
    exactly what happened in the confirmed v2.25.0 field crash: a
    `get_device_mix_format()` pycaw call finished cleanly on this
    worker thread, its proxies became cyclic garbage, and ~4 seconds
    later an UNRELATED asyncio pool thread inside
    `session_service.list_sessions` triggered a GC pass that tried to
    `Release()` them and segfaulted the whole backend.

    v2.23.2's fix (this module's never-uninitialize apartment) made
    sure the apartment itself always exists somewhere — but did
    nothing about WHICH THREAD eventually calls `Release()` on the
    proxies it hands out. That's the gap this function closes.

    THE FIX
    -------
    Calling `gc.collect()` here, on the worker thread, right after
    each job finishes (and after `_run` has dropped its own references
    to `job.fn`/`job.args`/`job.kwargs`), forces any cyclic garbage
    left behind by that job to be collected — and every `__del__` /
    `Release()` it triggers to run — RIGHT NOW, ON THIS THREAD, inside
    the apartment that owns it. "Collected eventually, on a random
    thread" becomes "collected immediately, in the owning apartment."

    WHY UNCONDITIONALLY, ON EVERY JOB
    -----------------------------------
    `gc.collect()` is not free (a full generational collection walks
    every tracked container in the process — can be low-to-mid
    single-digit milliseconds depending on heap size). We run it after
    EVERY job anyway rather than throttling/sampling, because:
      - COM work routed through this worker is inherently infrequent
        and non-hot-path — WASAPI device-mix-format reads (polled by
        the sync-risk endpoint, itself now cached — see
        core/audio_format_inspector.py) and Outlook calendar/email
        calls. We are not calling this per-audio-frame or anywhere
        near a tight loop.
      - Each of those calls already costs single-to-double-digit
        milliseconds of real COM/IPC work; a few extra ms of GC is
        noise by comparison.
      - The failure mode being guarded against is a full-process
        native crash (0xC0000005), not a slow response. That asymmetry
        — cheap-and-constant vs. rare-and-catastrophic — is exactly
        the case for paying the cost every time rather than sampling.
    If profiling ever shows this measurably hurts (e.g. a future
    caller starts routing hot-path work through `run_com`), the right
    fix is a call-count/time-based throttle here, NOT removing the
    call — the underlying affinity hazard doesn't go away just because
    a caller got busier.
    """
    gc.collect()


class _ComWorker:
    """One dedicated daemon thread that owns the process's only COM
    apartment. Started lazily on first use."""

    def __init__(self) -> None:
        self._jobs: "queue.Queue[_Job]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._ident: int | None = None

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is not None:
                return
            t = threading.Thread(
                target=self._run, name="com-worker", daemon=True,
            )
            t.start()
            self._thread = t

    def _run(self) -> None:
        self._ident = threading.get_ident()
        if _PYTHONCOM_AVAILABLE:
            # Initialize ONCE, for the life of the process. Deliberately
            # never tear the apartment back down (Co-Uninitialize) — see
            # module docstring. Do not "fix" this.
            try:
                pythoncom.CoInitialize()
            except Exception as e:
                logger.warning(f"CoInitialize failed on COM worker thread: {e}")
        while True:
            job = self._jobs.get()
            try:
                result = job.fn(*job.args, **job.kwargs)
                status, payload = "ok", result
            except BaseException as e:  # noqa: BLE001 - must relay every exception
                status, payload = "err", e

            # Drop OUR references to the job's callable/args/kwargs now
            # that we have the result. Per run_com's contract `payload`
            # itself must be caller-supplied plain data (never a COM
            # object), so this is the last place in this frame that
            # could still be holding a live COM proxy reachable only
            # via cyclic garbage. See _collect_cyclic_garbage_after_job
            # for why the next step is not optional.
            job.fn = None
            job.args = ()
            job.kwargs = {}

            try:
                _collect_cyclic_garbage_after_job()
            except Exception as gc_exc:  # pragma: no cover - defensive
                # A failure here must never swallow/replace the job's
                # actual result. Log and keep going — worst case we're
                # back to "collected later, on a random thread," which
                # is the pre-existing (if still crash-prone) behavior,
                # not a new failure mode.
                logger.warning(
                    "gc.collect() failed on COM worker thread after a "
                    f"job completed; job result is unaffected: {gc_exc!r}"
                )

            job.result_q.put((status, payload))

    def submit(self, fn: Callable[..., T], args: tuple, kwargs: dict,
               timeout: float | None) -> T:
        self._ensure_started()
        job = _Job(fn, args, kwargs)
        self._jobs.put(job)
        try:
            status, payload = job.result_q.get(timeout=timeout)
        except queue.Empty:
            raise ComCallTimeout(
                f"COM call {getattr(fn, '__name__', fn)!r} did not "
                f"complete within {timeout}s"
            ) from None
        if status == "err":
            raise payload
        return payload


_worker = _ComWorker()


def run_com(fn: Callable[..., T], *args: Any,
            timeout: float | None = 60.0, **kwargs: Any) -> T:
    """Run `fn(*args, **kwargs)` on the process's single COM worker
    thread and block the caller until it returns.

    - On Windows with pythoncom available: always executes on the same
      dedicated thread (never the caller's thread), which owns the
      process's one-and-only, never-uninitialized COM apartment.
    - On platforms/environments without pythoncom (macOS, Linux, CI):
      calls `fn` directly on the CURRENT thread — there's no COM
      apartment to protect, so there's nothing to route.

    Args:
        fn: Callable to run. Must not return any COM/comtypes/pycaw/
            win32com object — extract plain data inside `fn` instead.
        timeout: Seconds to wait for the call to finish. `None` waits
            forever. Raises ComCallTimeout on expiry.

    Raises:
        Whatever `fn` raises, re-raised in the caller's thread.
        ComCallTimeout if `timeout` elapses first.
    """
    if not _PYTHONCOM_AVAILABLE:
        return fn(*args, **kwargs)
    return _worker.submit(fn, args, kwargs, timeout)


def is_com_worker_thread() -> bool:
    """True if called from inside the COM worker thread itself. Mostly
    useful for tests/diagnostics."""
    return (
        _worker._ident is not None
        and threading.get_ident() == _worker._ident
    )
