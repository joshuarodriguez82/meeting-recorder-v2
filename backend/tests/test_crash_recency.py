"""
The "backend crashed at least once" banner that never went away.

``crash.log`` is append-only and never deleted (utils/crash_log.py,
design constraint 2 — a crash-looping machine must not overwrite the
first, usually most diagnostic, crash). The Settings → Diagnostics panel
used to warn purely on the file's *existence*, so a single crash on day
one left the banner up forever, nagging about a bug that may have been
fixed and released weeks ago.

The fix separates "is there crash history" (still surfaced via
crash_tail / Show-Copy, unconditionally) from "is there a RECENT crash"
(drives the warning banner). ``last_crash_time`` finds the timestamp of
the most recent crash *inside* the file; ``is_recent_crash`` compares it
against a threshold (7 days — see DEFAULT_CRASH_RECENCY_DAYS docstring:
long enough to cover an active investigation, short enough that a fixed
crash from weeks ago stops nagging on its own).

faulthandler's fatal-error dump has no timestamp of its own — only the
per-enable header does, and that header is written on EVERY backend
start, crash or not. So a "crash" is recognized as a header followed by
non-blank content before the next header/EOF, and the header's own
timestamp is used (a slight underestimate, but the only real data
available, and it errs toward "not recent" rather than the reverse).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from utils import crash_log


def _write_header(path: Path, ts: datetime, pid: int = 1234, mode: str = "a"):
    with open(path, mode, encoding="utf-8") as f:
        f.write(
            f"\n=== faulthandler enabled {ts.isoformat(timespec='seconds')} "
            f"pid={pid} python=3.11.5 platform=linux ===\n"
        )


# ── last_crash_time ──────────────────────────────────────────────────

def test_no_file_means_no_crash(tmp_path: Path):
    assert crash_log.last_crash_time(tmp_path) is None


def test_header_with_no_content_after_it_is_not_a_crash(tmp_path: Path):
    """Every backend start writes a header — a clean shutdown (or a
    still-healthy running process) leaves nothing after it. That must
    NOT be read as a crash, or the banner would show on every restart
    regardless of whether anything ever went wrong."""
    path = tmp_path / "crash.log"
    _write_header(path, datetime.now(), mode="w")
    assert crash_log.last_crash_time(tmp_path) is None


def test_header_followed_by_traceback_is_a_crash(tmp_path: Path):
    path = tmp_path / "crash.log"
    # Headers are written with timespec="seconds" (see crash_log._header),
    # so round-trip precision is 1s — truncate before comparing.
    ts = (datetime.now() - timedelta(hours=2)).replace(microsecond=0)
    _write_header(path, ts, mode="w")
    with open(path, "a", encoding="utf-8") as f:
        f.write("Fatal Python error: Segmentation fault\n\nCurrent thread...\n")
    found = crash_log.last_crash_time(tmp_path)
    assert found == ts


def test_most_recent_crash_wins_over_an_older_one(tmp_path: Path):
    path = tmp_path / "crash.log"
    old_ts = (datetime.now() - timedelta(days=30)).replace(microsecond=0)
    new_ts = (datetime.now() - timedelta(hours=1)).replace(microsecond=0)
    _write_header(path, old_ts, pid=1, mode="w")
    with open(path, "a", encoding="utf-8") as f:
        f.write("Fatal Python error: Aborted\n")
    _write_header(path, new_ts, pid=2)
    with open(path, "a", encoding="utf-8") as f:
        f.write("Fatal Python error: Segmentation fault\n")
    assert crash_log.last_crash_time(tmp_path) == new_ts


def test_clean_restart_after_a_crash_does_not_hide_the_crash(tmp_path: Path):
    """Crash, then a clean subsequent boot with no new fault — the
    crash from the earlier lifetime must still be findable."""
    path = tmp_path / "crash.log"
    crash_ts = (datetime.now() - timedelta(days=1)).replace(microsecond=0)
    _write_header(path, crash_ts, pid=1, mode="w")
    with open(path, "a", encoding="utf-8") as f:
        f.write("Fatal Python error: Segmentation fault\n")
    _write_header(path, datetime.now(), pid=2)  # clean run, no content after
    assert crash_log.last_crash_time(tmp_path) == crash_ts


def test_malformed_header_timestamp_does_not_raise(tmp_path: Path):
    path = tmp_path / "crash.log"
    path.write_text(
        "=== faulthandler enabled not-a-real-date pid=1 python=3 "
        "platform=linux ===\nFatal Python error: Aborted\n",
        encoding="utf-8",
    )
    # Can't date it -> can't safely call it recent, but must not raise.
    assert crash_log.last_crash_time(tmp_path) is None


def test_truncated_binary_garbage_does_not_raise(tmp_path: Path):
    path = tmp_path / "crash.log"
    path.write_bytes(b"\xff\xfe garbage === faulthandler enab")
    assert crash_log.last_crash_time(tmp_path) is None


def test_unreadable_file_does_not_raise(tmp_path: Path):
    """crash.log exists as a directory — read_text raises IsADirectoryError
    internally; last_crash_time must degrade to None, not propagate."""
    (tmp_path / "crash.log").mkdir()
    assert crash_log.last_crash_time(tmp_path) is None


# ── is_recent_crash ──────────────────────────────────────────────────

def test_no_timestamp_is_never_recent():
    assert crash_log.is_recent_crash(None) is False


def test_crash_within_threshold_is_recent():
    now = datetime(2026, 8, 13, 12, 0, 0)
    ts = now - timedelta(days=1)
    assert crash_log.is_recent_crash(ts, now=now) is True


def test_crash_older_than_threshold_is_not_recent():
    now = datetime(2026, 8, 13, 12, 0, 0)
    ts = now - timedelta(days=30)
    assert crash_log.is_recent_crash(ts, now=now) is False


def test_crash_exactly_at_threshold_boundary_is_recent():
    now = datetime(2026, 8, 13, 12, 0, 0)
    ts = now - timedelta(days=crash_log.DEFAULT_CRASH_RECENCY_DAYS)
    assert crash_log.is_recent_crash(ts, now=now) is True


def test_custom_threshold_is_respected():
    now = datetime(2026, 8, 13, 12, 0, 0)
    ts = now - timedelta(days=2)
    assert crash_log.is_recent_crash(ts, now=now, threshold_days=1) is False
    assert crash_log.is_recent_crash(ts, now=now, threshold_days=3) is True


# ── End-to-end: weeks-old crash stops warning, a fresh one still does ──

def test_end_to_end_old_crash_is_not_recent(tmp_path: Path):
    path = tmp_path / "crash.log"
    old_ts = datetime.now() - timedelta(days=45)
    _write_header(path, old_ts, mode="w")
    with open(path, "a", encoding="utf-8") as f:
        f.write("Fatal Python error: Segmentation fault\n")
    ts = crash_log.last_crash_time(tmp_path)
    assert ts is not None
    assert crash_log.is_recent_crash(ts) is False


def test_end_to_end_fresh_crash_is_recent(tmp_path: Path):
    path = tmp_path / "crash.log"
    fresh_ts = datetime.now() - timedelta(minutes=10)
    _write_header(path, fresh_ts, mode="w")
    with open(path, "a", encoding="utf-8") as f:
        f.write("Fatal Python error: Segmentation fault\n")
    ts = crash_log.last_crash_time(tmp_path)
    assert ts is not None
    assert crash_log.is_recent_crash(ts) is True
