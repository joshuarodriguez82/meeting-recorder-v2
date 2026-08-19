"""Structured application logger, and size management for ``backend.log``.

WHY ROTATION LOOKS ODD HERE (read this before "simplifying" it)
---------------------------------------------------------------
``backend.log`` is **not written by Python**. The Tauri shell opens it
itself and hands the Python child the resulting handle as its stdout AND
stderr::

    // src-tauri/src/lib.rs (~line 2052)
    let log_file = OpenOptions::new()
        .create(true).append(true).open(backend_log_path())?;
    ...
    .stdout(Stdio::from(log_file))
    .stderr(Stdio::from(log_file2));

Nothing on either side ever truncates it, and Rust re-opens the SAME
file in append mode on every launch (it only writes a
``=== backend spawn @ ... ===`` separator first). That is the whole
mechanism behind the 231 MB single-file ``backend.log`` from the field
report — it is not one long session, it is every session this machine
has ever run, concatenated.

That ownership is what rules out the two obvious fixes:

* ``logging.handlers.RotatingFileHandler`` rotates by **renaming** the
  active file. Both POSIX and Windows keep an open handle bound to the
  file it was opened on, not to the name — Rust's std ``OpenOptions``
  defaults to ``FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE``,
  so the rename would even *succeed* on Windows and then silently divert
  the backend's entire stdout stream into ``backend.log.1`` while the
  new ``backend.log`` stayed empty forever. A rotation that quietly
  produces nothing is exactly the failure mode this repo keeps getting
  bitten by.
* Truncating through our own fd 1 does not work on Windows either:
  Rust's ``append(true)`` maps to ``FILE_GENERIC_WRITE & !FILE_WRITE_DATA``,
  and ``SetEndOfFile`` needs ``FILE_WRITE_DATA``.

So rotation here is **copy-then-truncate-in-place**:

1. copy the current contents to ``backend.log.1`` (shifting older
   backups down, ``RotatingFileHandler`` naming so existing habits and
   tooling still apply),
2. truncate ``backend.log`` to zero length through a **separate**
   read/write handle (Rust shares the file, so this is permitted),
3. leave the inode / file object alone.

Because the handle Rust gave us is in APPEND mode, every subsequent
write — from Python's logging, from an uncaught traceback on stderr,
from pip during a dependency self-heal — resolves the write offset at
write time and lands at the new end of file (0). No hole, no lost
stream, no rename.

CAPS
----
16 MiB live + 4 backups = **80 MiB**, or ~85 MiB worst case: the size
check runs every ``DEFAULT_CHECK_INTERVAL_BYTES`` (1 MiB) of output
rather than on every line, so a file can overshoot the cap by up to one
check interval before it rotates. That overshoot is bounded and is the
price of not calling ``stat()`` once per log line.

Rationale for the numbers, from the field data rather than taste:

* The observed failure was a *single* 231 MB file that no tool could
  usefully scan — "the last 20,000 lines" missed the user's recordings
  completely. 16 MiB is roughly 130k lines at this formatter's typical
  line length, i.e. the whole live file is greppable in one pass and a
  single ``.1`` archive still covers the previous window.
* 5 windows of 16 MiB comfortably spans several days of real use
  including the 1 Hz status-poll chatter, while capping total disk at
  about a third of the single file we already know one machine produced.

Both numbers are overridable with ``MEETING_RECORDER_LOG_MAX_BYTES`` /
``MEETING_RECORDER_LOG_BACKUPS`` for support scenarios that need a
deeper window, and the check granularity with
``MEETING_RECORDER_LOG_CHECK_BYTES``.

Rotation is only ever attempted when our stdout really is a regular
file that really is the ``backend.log`` next to ``crash.log`` (verified
by device+inode, not by name). Running the backend from a console, under
pytest, or with stdout on a pipe leaves behaviour byte-for-byte as it
was before this module grew a rotation section.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat as _stat
import sys
import threading
from pathlib import Path
from typing import Optional

# Force stdout/stderr to UTF-8 so unicode characters in log messages don't
# crash the cp1252 charmap codec on Windows (which is what you get when
# stdout is redirected to a file by the Tauri shell).
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


BACKEND_LOG_NAME = "backend.log"

#: Live-file ceiling. See the CAPS section in the module docstring.
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
#: ``backend.log.1`` … ``backend.log.4``.
DEFAULT_BACKUP_COUNT = 4
#: How many bytes we let the handler write between ``stat()`` calls. The
#: estimate is deliberately rough — the stat that follows is the ground
#: truth, this only bounds how often we pay for one.
DEFAULT_CHECK_INTERVAL_BYTES = 1024 * 1024

# The formatter every other tool in this repo greps against. Do not
# change this string without auditing the callers listed in AGENTS.md.
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def default_log_dir() -> Path:
    """The directory ``backend.log`` lives in.

    Delegates to :mod:`utils.crash_log`, which resolves it stdlib-only
    (no ``config.settings``, no python-dotenv) precisely so logging keeps
    working on a machine whose venv is mid-repair.
    """
    override = os.environ.get("MEETING_RECORDER_LOG_DIR")
    if override:
        return Path(override)
    try:
        from utils.crash_log import default_log_dir as _crash_dir
        return _crash_dir()
    except Exception:  # pragma: no cover - defensive
        from pathlib import Path as _P
        return _P.home() / ".config" / "MeetingRecorder"


def backend_log_path() -> Path:
    """Absolute path of ``backend.log`` (the file Rust owns)."""
    return default_log_dir() / BACKEND_LOG_NAME


def _backup_path(path: Path, index: int) -> Path:
    # NOT Path.with_suffix — "backend.log".with_suffix(".1") is
    # "backend.1", which would orphan every archive.
    return Path(f"{path}.{index}")


def rotate_log_in_place(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    fd: Optional[int] = None,
) -> bool:
    """Copy-then-truncate ``path`` if it has grown past ``max_bytes``.

    Returns True when a rotation happened. Never raises: losing rotation
    is a disk-space problem, losing the log is a diagnosis problem, and
    crashing the backend over either is worse than both.

    ``fd``, when given, is our own writing file descriptor (normally
    stdout). It is flushed before the truncate and repositioned after,
    which is a no-op for the APPEND-mode handle Rust actually hands us
    but keeps the file from growing a multi-megabyte NUL hole in the
    plain ``python server.py > backend.log`` case, where the shell used
    ``O_TRUNC`` and the offset would otherwise survive the truncate.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= max_bytes:
        return False

    try:
        # Everything the caller has buffered belongs in the archive, not
        # at offset 0 of the fresh file.
        if fd is not None:
            try:
                sys.stdout.flush()
            except Exception:
                pass

        if backup_count > 0:
            oldest = _backup_path(path, backup_count)
            try:
                if oldest.exists():
                    oldest.unlink()
            except OSError:
                pass
            for i in range(backup_count - 1, 0, -1):
                src = _backup_path(path, i)
                if not src.exists():
                    continue
                dst = _backup_path(path, i + 1)
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
            # Copy, never rename: the live file must keep its identity
            # so the handle Rust gave the process keeps writing to it.
            shutil.copyfile(path, _backup_path(path, 1))

        # A SEPARATE read/write handle. Truncating through fd 1 fails on
        # Windows because Rust masks FILE_WRITE_DATA off an append-mode
        # open; opening the path afresh gets full write access, and
        # Rust's share mode permits it.
        with open(path, "r+b") as f:
            f.truncate(0)

        if fd is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
            except OSError:
                # Expected on an APPEND handle on some platforms, and on
                # anything that isn't seekable. Harmless either way.
                pass
        return True
    except Exception:
        return False


class _BackendLogGovernor:
    """One rotation decision-maker shared by every module logger.

    ``get_logger`` builds a handler per logger name, so the byte
    accounting cannot live on the handler — forty loggers would each
    reach the check threshold forty times too late. All handlers report
    into this single object instead.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resolved = False
        self._path: Optional[Path] = None
        self._fd: Optional[int] = None
        self._pending = 0
        self.max_bytes = _env_int(
            "MEETING_RECORDER_LOG_MAX_BYTES", DEFAULT_MAX_BYTES)
        self.backup_count = _env_int(
            "MEETING_RECORDER_LOG_BACKUPS", DEFAULT_BACKUP_COUNT)
        self.check_interval_bytes = _env_int(
            "MEETING_RECORDER_LOG_CHECK_BYTES", DEFAULT_CHECK_INTERVAL_BYTES)

    def reset(self) -> None:
        """Force re-detection. Tests only."""
        with self._lock:
            self._resolved = False
            self._path = None
            self._fd = None
            self._pending = 0

    def _resolve(self) -> None:
        """Decide, once, whether we are allowed to rotate at all.

        We are, only if stdout is a *regular file* that is the very same
        file (device + inode, not name) as the ``backend.log`` sitting
        next to ``crash.log``. Anything else — a console, a pytest
        capture buffer, a pipe, a dev redirect somewhere unexpected —
        means we do not own that file and must not touch it.
        """
        self._resolved = True
        try:
            stream = sys.stdout
            fd = stream.fileno()
        except Exception:
            return
        try:
            st = os.fstat(fd)
        except OSError:
            return
        if not _stat.S_ISREG(st.st_mode):
            return
        if st.st_ino == 0:
            # Some filesystems do not report a usable inode; without one
            # we cannot prove the path we are about to truncate is the
            # file we are writing to. Refuse rather than guess.
            return
        path = backend_log_path()
        try:
            pst = path.stat()
        except OSError:
            return
        if (pst.st_dev, pst.st_ino) != (st.st_dev, st.st_ino):
            return
        self._path = path
        self._fd = fd

    def note_written(self, nbytes: int) -> None:
        with self._lock:
            if not self._resolved:
                self._resolve()
            if self._path is None:
                return
            self._pending += nbytes
            if self._pending < self.check_interval_bytes:
                return
            self._pending = 0
            path, fd = self._path, self._fd
        # Rotate outside the lock's hot path but still serialized by it
        # for the counter; the copy itself is idempotent-safe because a
        # second caller re-stats and finds the file under the cap.
        rotate_log_in_place(
            path,
            max_bytes=self.max_bytes,
            backup_count=self.backup_count,
            fd=fd,
        )


_GOVERNOR = _BackendLogGovernor()


def backend_log_governor() -> "_BackendLogGovernor":
    """The shared rotation governor (exposed for tests)."""
    return _GOVERNOR


class RotatingStdoutHandler(logging.StreamHandler):
    """``StreamHandler`` on stdout that also keeps ``backend.log`` bounded.

    Emission is byte-for-byte what it always was — same stream, same
    formatter. The only addition is telling the shared governor how much
    was written so it can stat-and-rotate on a schedule.
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            # Cheap estimate; the governor's stat() is the real measure.
            # 96 bytes approximates the timestamp/level/name prefix.
            _GOVERNOR.note_written(len(record.getMessage()) + 96)
        except Exception:
            pass


def get_logger(name: str) -> logging.Logger:
    """
    Return a configured logger for the given module name.

    Args:
        name: Typically __name__ from the calling module.

    Returns:
        Configured Logger instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = RotatingStdoutHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
