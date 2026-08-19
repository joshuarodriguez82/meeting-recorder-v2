"""
backend.log rotation.

THE BUG THIS COVERS
-------------------
Field report: ``backend.log`` reached 231 MB in a single file with no
rotation, and a scan of the last 20,000 lines missed the user's
recordings entirely — producing a confidently wrong "no AEC decisions
anywhere in the log" conclusion.

THE CONSTRAINT THESE TESTS PIN
------------------------------
``backend.log`` is opened by the Tauri shell in APPEND mode and handed
to Python as its stdout (src-tauri/src/lib.rs). A rename-based rotation
would leave the shell's handle bound to the renamed file and silently
divert the entire stream into the archive. So rotation here must
copy-then-truncate **in place**, preserving the file's identity.

``test_rotation_preserves_file_identity`` is the one that fails if
somebody "simplifies" this back to a stock RotatingFileHandler.
"""

import os
import subprocess
import sys
from pathlib import Path

from utils import logger as logger_mod

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, nbytes: int) -> None:
    path.write_bytes(b"x" * nbytes)


def test_no_rotation_below_cap(tmp_path: Path):
    log = tmp_path / "backend.log"
    _write(log, 100)
    assert not logger_mod.rotate_log_in_place(
        log, max_bytes=1000, backup_count=3)
    assert log.stat().st_size == 100
    assert not (tmp_path / "backend.log.1").exists()


def test_rotation_caps_live_file_size(tmp_path: Path):
    log = tmp_path / "backend.log"
    _write(log, 5000)
    assert logger_mod.rotate_log_in_place(
        log, max_bytes=1000, backup_count=2)
    assert log.stat().st_size == 0
    assert (tmp_path / "backend.log.1").stat().st_size == 5000


def test_total_disk_use_is_bounded(tmp_path: Path):
    """The actual promise: live file + N backups, never more.

    Rotates far more times than there are backup slots. The ceiling
    includes one check-interval of overshoot per file, because the size
    check runs every N bytes of output rather than on every line — here
    that interval is the 1500-byte write chunk.
    """
    log = tmp_path / "backend.log"
    max_bytes, backups, chunk = 1000, 3, 1500
    for _ in range(10):
        with open(log, "ab") as f:
            f.write(b"y" * chunk)
        logger_mod.rotate_log_in_place(
            log, max_bytes=max_bytes, backup_count=backups)

    files = sorted(tmp_path.iterdir())
    # No unbounded fan-out: exactly live + backups, after ten rotations.
    assert [p.name for p in files] == [
        "backend.log", "backend.log.1", "backend.log.2", "backend.log.3",
    ]
    # And no single file ran away either.
    for p in files:
        assert p.stat().st_size <= max_bytes + chunk

    total = sum(p.stat().st_size for p in files)
    ceiling = (max_bytes + chunk) * (backups + 1)
    assert total <= ceiling, f"{total} bytes on disk, ceiling {ceiling}"


def test_backups_shift_oldest_out(tmp_path: Path):
    log = tmp_path / "backend.log"
    for marker in (b"A", b"B", b"C"):
        log.write_bytes(marker * 2000)
        logger_mod.rotate_log_in_place(log, max_bytes=100, backup_count=2)
    # .1 is the newest archive, .2 the one before it, and the oldest
    # (the "A" pass) has aged out entirely.
    assert (tmp_path / "backend.log.1").read_bytes()[:1] == b"C"
    assert (tmp_path / "backend.log.2").read_bytes()[:1] == b"B"
    assert not (tmp_path / "backend.log.3").exists()


def test_rotation_preserves_file_identity(tmp_path: Path):
    """The whole reason this is not a RotatingFileHandler.

    Another process (the Tauri shell) holds this file open in append
    mode. Rotation must NOT rename it — the archive is a copy and the
    live file keeps its inode, so the foreign handle keeps writing to
    the file that still has the name ``backend.log``.
    """
    log = tmp_path / "backend.log"
    _write(log, 3000)
    before = log.stat()

    # Stand in for the Tauri shell: an independent append-mode handle,
    # opened before rotation and written through after it.
    foreign = os.open(str(log), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        assert logger_mod.rotate_log_in_place(
            log, max_bytes=100, backup_count=1)
        after = log.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

        os.write(foreign, b"post-rotation line\n")
    finally:
        os.close(foreign)

    # The foreign writer's bytes landed in the LIVE file, not the archive.
    assert log.read_bytes() == b"post-rotation line\n"
    assert b"post-rotation" not in (tmp_path / "backend.log.1").read_bytes()


def test_rotate_never_raises_on_missing_file(tmp_path: Path):
    assert logger_mod.rotate_log_in_place(
        tmp_path / "does-not-exist.log", max_bytes=1, backup_count=1) is False


def test_governor_stands_down_when_stdout_is_not_backend_log(tmp_path,
                                                             monkeypatch):
    """Under pytest / a console / a pipe we do not own backend.log, and
    must not touch whatever file happens to sit at that path."""
    monkeypatch.setenv("MEETING_RECORDER_LOG_DIR", str(tmp_path))
    decoy = tmp_path / "backend.log"
    _write(decoy, 50_000)

    gov = logger_mod.backend_log_governor()
    gov.reset()
    gov.max_bytes = 10
    gov.check_interval_bytes = 1
    try:
        gov.note_written(1000)
    finally:
        gov.reset()

    # Untouched: pytest's captured stdout is not this file.
    assert decoy.stat().st_size == 50_000
    assert not (tmp_path / "backend.log.1").exists()


# ── end-to-end, in the real production topology ──────────────────────

_CHILD = """
import sys
sys.path.insert(0, {root!r})
from utils.logger import get_logger
log = get_logger("rotation.e2e")
for i in range(4000):
    log.info("filler line %05d %s" % (i, "z" * 200))
# One short line after the last rotation. It cannot itself trigger a
# rotation (well under the check interval), so it MUST land in the live
# file — that is what proves the stream was not diverted.
log.info("FINAL-MARKER")
"""


def test_end_to_end_rotation_when_stdout_is_backend_log(tmp_path: Path):
    """The topology that actually ships.

    The Tauri shell opens backend.log in APPEND mode and hands it to the
    Python child as stdout; the child never opens the file itself. This
    reproduces exactly that — parent opens append-mode, child inherits —
    and asserts the log is capped anyway.

    This is the test that would have caught the 231 MB file.
    """
    log_dir = tmp_path
    log_path = log_dir / "backend.log"

    env = dict(os.environ)
    env["MEETING_RECORDER_LOG_DIR"] = str(log_dir)
    env["MEETING_RECORDER_LOG_MAX_BYTES"] = "20000"
    env["MEETING_RECORDER_LOG_BACKUPS"] = "2"
    env["MEETING_RECORDER_LOG_CHECK_BYTES"] = "5000"

    # Same call shape as src-tauri/src/lib.rs: an append-mode handle,
    # used for both stdout and stderr of the child.
    with open(log_path, "ab") as handle:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD.format(root=str(BACKEND_ROOT))],
            stdout=handle, stderr=handle, env=env, timeout=120,
        )
    assert proc.returncode == 0

    # Uncapped, 4000 lines x ~250 bytes would be ~1 MB in one file.
    assert log_path.exists()
    live = log_path.stat().st_size
    assert live <= 20000 + 5000

    archives = sorted(p.name for p in log_dir.glob("backend.log.*"))
    assert archives, "nothing rotated — the live file was never capped"
    assert archives == ["backend.log.1", "backend.log.2"], archives

    # The stream was never diverted into an archive and lost, which is
    # exactly what rename-based rotation would have done here: the
    # child kept writing through the handle it inherited, and its
    # post-rotation line is in the LIVE file.
    assert b"FINAL-MARKER" in log_path.read_bytes()
    for name in archives:
        assert b"FINAL-MARKER" not in (log_dir / name).read_bytes()
    # And nothing was thrown away — the archives hold the earlier lines.
    assert b"filler line" in (log_dir / "backend.log.1").read_bytes()


def test_get_logger_keeps_existing_handler_contract():
    """Formatting other tooling greps for must not change, and the
    handler must still be a StreamHandler on stdout."""
    import logging
    import sys

    lg = logger_mod.get_logger("tests.rotation.contract")
    assert len(lg.handlers) == 1
    handler = lg.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout
    assert handler.formatter._fmt == (
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    assert lg.level == logging.INFO


def test_default_caps_are_bounded_and_sane():
    """Documented ceiling: 16 MiB live + 4 backups = 80 MiB, ~85 MiB
    once the 1 MiB check-interval overshoot is allowed for."""
    assert logger_mod.DEFAULT_MAX_BYTES == 16 * 1024 * 1024
    assert logger_mod.DEFAULT_BACKUP_COUNT == 4
    slots = logger_mod.DEFAULT_BACKUP_COUNT + 1
    assert logger_mod.DEFAULT_MAX_BYTES * slots == 80 * 1024 * 1024
    worst_case = (logger_mod.DEFAULT_MAX_BYTES
                  + logger_mod.DEFAULT_CHECK_INTERVAL_BYTES) * slots
    # Comfortably under the 231 MB single file the field report found.
    assert worst_case < 231 * 1000 * 1000
