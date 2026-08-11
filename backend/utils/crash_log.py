"""
Native-crash capture (``faulthandler``) for the Python backend.

WHY THIS EXISTS
---------------
The backend has been dying on Windows with exit code 3221225477
(0xC0000005, STATUS_ACCESS_VIOLATION) since at least v2.0.18. The same
symptom is called out in the release notes for v2.0.18, v2.11.1,
v2.12.0, v2.19.1 and v2.19.3 and has never once been diagnosed, for one
structural reason: an access violation is a *native* fault. The CPython
interpreter is torn down by the OS before any Python-level ``except``
or logging handler runs, so ``backend.log`` simply stops mid-line. Five
hypotheses have been proposed and "disproved" purely by reasoning about
restart timing — none of them from evidence, because there was no
evidence to read (field report 2026-08-11).

``faulthandler`` fixes exactly this. It installs an OS-level signal /
structured-exception handler that dumps the Python traceback of every
thread using pre-allocated buffers and a raw ``write()`` to a file
descriptor. It needs no interpreter state, so it still produces output
from inside a segfault.

DESIGN CONSTRAINTS (each one is a way this silently produces nothing)
---------------------------------------------------------------------
1. The file object must stay alive for the whole process lifetime.
   faulthandler stores the *file descriptor*, not the Python object.
   If the file gets garbage-collected (or closed) the fd is recycled
   and the crash dump goes to whatever now owns that number — usually
   nowhere. Hence the module-level ``_CRASH_FH`` reference.
2. Append, never truncate. Consecutive crashes are the whole point;
   a crash-loop rewriting the file over the previous crash would erase
   the exact evidence we're after.
3. A per-enable header with timestamp + PID, so consecutive crash
   dumps in one file are attributable to a specific backend process.
   The Rust watchdog respawns the backend, so one crash.log will hold
   several process lifetimes.
4. Everything is best-effort. A backend that refuses to start because
   its crash logger couldn't open a file is strictly worse than a
   backend with no crash logger.

WHERE IT WRITES
---------------
``USER_DATA_DIR/crash.log`` — the same directory ``backend.log`` lives
in (config/settings.py ``USER_DATA_DIR``; mirrored on the Rust side by
lib.rs ``log_dir()``/``backend_log_path()``). A DEDICATED file, not
backend.log, because backend.log is a redirected stdout/stderr stream
owned by the Rust shell — a dump interleaved into a half-flushed stream
buffer is exactly the kind of evidence that goes missing.

This module is deliberately **stdlib-only**. It must be importable
before the dependency self-heal, before numpy/torch/pyannote, and
inside the CI test venv (which has no python-dotenv, so importing
``config.settings`` here is not an option).
"""

from __future__ import annotations

import faulthandler
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

CRASH_LOG_NAME = "crash.log"

# Module-level reference — see design constraint 1. Never reassign this
# without closing/replacing deliberately; never let it fall out of scope.
_CRASH_FH = None  # type: ignore[var-annotated]
_CRASH_PATH: Optional[Path] = None


def default_log_dir() -> Path:
    """The directory ``backend.log`` lives in, resolved WITHOUT importing
    ``config.settings``.

    Duplicating the handful of lines from ``config.settings._user_data_dir``
    is deliberate: this runs before the dependency self-heal, and
    ``config.settings`` imports python-dotenv. If dotenv is missing (the
    exact venv-corruption case the self-heal exists to repair) we would
    lose crash logging on precisely the machines most likely to crash.
    Keep the two in sync — Windows LOCALAPPDATA (never APPDATA: OneDrive
    Known Folder Move redirects Roaming), macOS Application Support,
    Linux XDG_CONFIG_HOME.
    """
    if os.name == "nt":
        base = (os.getenv("LOCALAPPDATA")
                or os.getenv("APPDATA")
                or os.getenv("USERPROFILE")
                or str(Path.home()))
        return Path(base) / "MeetingRecorder"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MeetingRecorder"
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "MeetingRecorder"


def crash_log_path(log_dir: Optional[Path] = None) -> Path:
    """Absolute path of crash.log, defaulting to next to backend.log."""
    root = Path(log_dir) if log_dir is not None else default_log_dir()
    return root / CRASH_LOG_NAME


def _header(pid: int) -> str:
    """One line per enable. Timestamps are LOCAL — every other log the
    user copies into a bug report is local time, and correlating
    crash.log against rust.log by eye is the entire workflow this
    unblocks."""
    return (
        f"\n=== faulthandler enabled {datetime.now().isoformat(timespec='seconds')} "
        f"pid={pid} python={sys.version.split()[0]} platform={sys.platform} ===\n"
    )


def _register_extra_signals(fh) -> None:
    """Register the signals ``faulthandler.enable()`` does not cover.

    ``enable()`` already handles SIGSEGV / SIGFPE / SIGABRT / SIGBUS /
    SIGILL. ``register()`` is Unix-only (it needs sigaction), so on
    Windows the whole block is skipped silently — that is expected, not
    a failure. On macOS/Linux we add SIGTERM so a watchdog kill also
    leaves a traceback showing what the backend was doing when it was
    killed, which distinguishes "hung in model load" from "hung in a
    COM call" without any extra instrumentation.
    """
    if not hasattr(faulthandler, "register"):
        return
    try:
        import signal
    except Exception:  # noqa: BLE001 — signal is stdlib, but be paranoid
        return
    for name in ("SIGTERM", "SIGABRT", "SIGQUIT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            faulthandler.register(signum, file=fh, all_threads=True, chain=True)
        except (OSError, RuntimeError, ValueError, AttributeError):
            # Not supported for this signal on this platform — skip it.
            # Windows never gets here (no faulthandler.register at all).
            continue


def enable_crash_logging(log_dir: Optional[Path] = None) -> Optional[Path]:
    """Turn on faulthandler, writing to ``<log_dir>/crash.log``.

    Returns the crash-log path on success, or ``None`` if crash logging
    could not be enabled. NEVER raises — the backend must boot either
    way (design constraint 4).

    Call this as early as possible in ``server.py``, before numpy /
    torch / faster-whisper / pyannote are imported, so a fault during
    native model-library init is still captured. That path matters: the
    2026-07-21 rust.log shows repeated 0xC0000005 exits during "Loading
    transcription engine" (field report 2026-08-11).
    """
    global _CRASH_FH, _CRASH_PATH

    # Step 1: stderr first. This is one syscall-free call and it means
    # that if step 2 fails for any reason (read-only dir, antivirus
    # holding the file) we STILL capture the dump — stderr is redirected
    # into backend.log by the Rust shell, so the traceback lands there.
    try:
        faulthandler.enable(all_threads=True)
    except Exception:  # noqa: BLE001
        pass

    # Step 2: point the handler at the dedicated file. NOTE: faulthandler
    # keeps exactly ONE fatal-error destination, so this supersedes the
    # stderr target set above rather than adding to it — that is the
    # documented CPython behaviour and there is no supported way to fan
    # out to two fds. crash.log wins because backend.log is the stream
    # that has been going silent for five releases; the Rust watchdog
    # writes a "=== BACKEND EXITED code=… ===" marker into backend.log
    # (lib.rs) so the two files cross-reference at the same instant.
    path = crash_log_path(log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered text append. "a" is load-bearing (constraint 2).
        fh = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        fh.write(_header(os.getpid()))
        fh.flush()
        faulthandler.enable(file=fh, all_threads=True)
    except Exception:  # noqa: BLE001 — OSError, ValueError, anything
        # Leave the stderr handler from step 1 in place and carry on.
        return None

    # Only now drop the previous handle: the old fd must stay open until
    # faulthandler has been re-pointed at the new one.
    old = _CRASH_FH
    _CRASH_FH = fh
    _CRASH_PATH = path
    if old is not None and old is not fh:
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass

    _register_extra_signals(fh)
    return path


def disable_crash_logging() -> None:
    """Tear down — used by tests so a temp-dir crash.log fd never stays
    wired into the process-wide handler after the tmp_path is removed.
    Not called in production; the fd is meant to live forever there."""
    global _CRASH_FH, _CRASH_PATH
    try:
        faulthandler.disable()
    except Exception:  # noqa: BLE001
        pass
    fh, _CRASH_FH = _CRASH_FH, None
    _CRASH_PATH = None
    if fh is not None:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass


def active_crash_log_path() -> Optional[Path]:
    """Path currently wired into faulthandler, or None if disabled."""
    return _CRASH_PATH


def read_crash_tail(log_dir: Optional[Path] = None,
                    max_lines: int = 100) -> str:
    """Last ``max_lines`` lines of crash.log, or "" when there is no
    crash log (the healthy case). Reads only the tail so a crash-looping
    machine with a huge file doesn't blow up the /diagnostics response.
    Never raises — diagnostics degrades to a note, not a 500."""
    path = crash_log_path(log_dir)
    try:
        if not path.exists():
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 64 * 1024)
            f.seek(size - back)
            chunk = f.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()[-max_lines:]
        return "\n".join(lines).strip()
    except Exception as e:  # noqa: BLE001
        return f"(could not read crash.log: {e})"
