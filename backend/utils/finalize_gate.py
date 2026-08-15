"""Process-wide serialization + CPU-priority hint for finalize work.

Field data (2026-08): with echo cancellation enabled, finalize_audio.py
(WAV merge, multi-threaded resamplers, AEC cross-correlation) takes
192-278s — roughly 40x its pre-AEC cost of 3-15s. That is a much wider
window in which a SECOND finalize can end up running at the same time:
a back-to-back meeting's stop racing this one, a bulk re-process/export
touching many sessions, or the startup orphan-recovery sweep in
services/recovery_service.py racing a stop_recording that happens right
after backend launch. Two finalizes contending for the same handful of
CPU cores as a still-in-progress LIVE recording means that recording
drops frames — the meeting that's actually happening loses to
background cleanup work for a meeting that already ended.

This module is the ONE gate for that. Every finalize call site routes
through it — see services/recording_service.py's stop_recording (the
``_run_finalize_subprocess`` spawn) and services/recovery_service.py's
recover_orphans (the in-process ``finalize_recording_streaming`` call
on startup). Do not add a second, parallel gate for a new call site;
import ``finalize_slot`` here instead.

Two complementary protections:

  (a) ``finalize_slot()`` — a process-wide mutex. At most one finalize
      runs at a time; everything else waits. Callers surface the wait
      via their own state (see ``Session.finalize_status ==
      "queued"``) using the ``on_queued`` callback below — this module
      has no opinion on Session at all, keeping it a plain, cheap-to-
      import utility with no heavy dependencies.

  (b) ``below_normal_priority_kwargs()`` — even the ONE finalize that's
      allowed to run should never be able to outrank a live recording's
      capture thread for CPU. This asks the OS scheduler to run the
      finalize child at below-normal priority. It is a hint, not a
      guarantee, and it degrades to normal priority (never a failure)
      on any platform/condition where the mechanism isn't available.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from typing import Callable, Optional

# One process-wide slot. A plain Lock — not a Semaphore — because "at
# most one finalize at a time" is the entire contract; no call site
# should ever need more than 1, and a Lock makes double-acquire-by-the-
# same-thread (a bug) raise instead of silently deadlocking forever.
_lock = threading.Lock()


@contextlib.contextmanager
def finalize_slot(on_queued: Optional[Callable[[], None]] = None):
    """Block until the caller is the only finalize running process-wide.

    If the slot is already held by another finalize, ``on_queued()`` is
    invoked exactly once — best-effort, any exception it raises is
    swallowed — BEFORE this call blocks. Callers use that hook to flip
    their own persisted state to "queued" (see recording_service.py's
    ``_mark_queued``) so a concurrent reader sees the true reason for
    the delay instead of the request looking silently stuck.

    Usage::

        with finalize_slot(on_queued=mark_queued):
            # only one caller across the whole process is ever here
            run_the_actual_finalize_work()
    """
    acquired = _lock.acquire(blocking=False)
    if not acquired:
        if on_queued is not None:
            try:
                on_queued()
            except Exception:
                pass
        _lock.acquire(blocking=True)
    try:
        yield
    finally:
        _lock.release()


def _posix_lower_priority() -> None:
    """``preexec_fn`` target for the finalize child on POSIX (Linux /
    macOS). Runs INSIDE the forked child, after fork but before exec,
    per ``subprocess``'s preexec_fn contract — so raising here would
    only ever affect the child, never the parent backend. Still wrapped
    defensively: a nice() call that fails (e.g. an already-min-priority
    parent, or a sandboxed/restricted environment that denies it) must
    never prevent finalize from actually running, so any failure here
    just means the child inherits the parent's normal priority instead
    of getting the below-normal hint."""
    try:
        os.nice(10)
    except Exception:
        pass


def below_normal_priority_kwargs() -> dict:
    """Extra ``subprocess.run``/``Popen`` kwargs that ask the OS
    scheduler to run the finalize child at below-normal CPU priority,
    so a live recording's capture thread always wins contention for
    cores over a background finalize.

    Windows: ``creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS``.
    POSIX (Linux/macOS): ``preexec_fn`` that raises the child's nice
    value (lower scheduling priority) right after fork.

    Guarded by platform, and returns ``{}`` (i.e. spawn at normal
    priority, same as before this feature existed) on any
    platform/condition where the mechanism isn't available — a
    priority hint that can't be applied must never block finalize from
    running at all. Callers should ALSO be ready for the actual spawn
    call to reject these kwargs at runtime (see
    ``services/recording_service.py``'s retry-without-priority-kwargs
    fallback) since that failure mode can only be observed at call
    time, not by inspecting the platform up front.
    """
    if sys.platform.startswith("win"):
        creationflag = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", None)
        if creationflag is None:
            return {}
        return {"creationflags": creationflag}
    if os.name == "posix":
        return {"preexec_fn": _posix_lower_priority}
    # Unknown platform — no known hint, spawn at normal priority.
    return {}
