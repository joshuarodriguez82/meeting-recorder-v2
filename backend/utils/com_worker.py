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
"""

from __future__ import annotations

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
                job.result_q.put(("ok", result))
            except BaseException as e:  # noqa: BLE001 - must relay every exception
                job.result_q.put(("err", e))

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
