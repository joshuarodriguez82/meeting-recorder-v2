"""
Crash-capture (faulthandler) wiring — utils/crash_log.py.

These tests never trigger a real fault. What they pin is the plumbing
that has historically been the difference between "we have a crash
dump" and "the file is empty":

  - the header is actually written (so consecutive process lifetimes in
    one file are attributable),
  - a second enable APPENDS rather than truncating (a crash loop must
    not erase the first crash — that first one is usually the honest
    one),
  - a file that can't be opened degrades to "no crash logging", never
    to "backend won't boot".

Background: the 0xC0000005 / 3221225477 Windows exit has been reported
across v2.0.18, v2.11.1, v2.12.0, v2.19.1 and v2.19.3 and was never
diagnosed because backend.log simply stops mid-line (field report
2026-08-11).
"""

from __future__ import annotations

import faulthandler
from pathlib import Path

import pytest

from utils import crash_log


@pytest.fixture(autouse=True)
def _restore_faulthandler():
    """Never leave the process-wide handler pointing at a tmp_path fd —
    the directory is deleted after the test and the fd would be recycled
    (that's design constraint 1 in crash_log.py, and it applies to the
    test process too)."""
    yield
    crash_log.disable_crash_logging()


def test_enable_writes_header(tmp_path: Path):
    path = crash_log.enable_crash_logging(tmp_path)
    assert path == tmp_path / "crash.log"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "faulthandler enabled" in text
    assert f"pid=" in text
    # faulthandler is actually armed, not just a file we wrote to.
    assert faulthandler.is_enabled()
    assert crash_log.active_crash_log_path() == path


def test_enable_twice_appends_and_does_not_truncate(tmp_path: Path):
    crash_log.enable_crash_logging(tmp_path)
    path = tmp_path / "crash.log"
    # Stand in for a previous process's crash dump.
    with open(path, "a", encoding="utf-8") as f:
        f.write("Windows fatal exception: access violation\n")

    crash_log.enable_crash_logging(tmp_path)

    text = path.read_text(encoding="utf-8")
    assert "Windows fatal exception: access violation" in text
    assert text.count("faulthandler enabled") == 2


def test_enable_survives_unopenable_crash_file(tmp_path: Path):
    # crash.log exists as a DIRECTORY — open() raises IsADirectoryError
    # (PermissionError on Windows). Stands in for the real-world cases:
    # read-only profile dir, antivirus holding the handle.
    (tmp_path / "crash.log").mkdir()

    assert crash_log.enable_crash_logging(tmp_path) is None
    # ...and the stderr fallback is still armed, so a fault is not lost.
    assert faulthandler.is_enabled()


def test_enable_survives_faulthandler_enable_failure(tmp_path: Path, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("sys.stderr has no fileno (embedded interpreter)")

    monkeypatch.setattr(faulthandler, "enable", _boom)
    # Must not propagate — startup continues either way.
    assert crash_log.enable_crash_logging(tmp_path) is None


def test_read_crash_tail_empty_when_no_file(tmp_path: Path):
    assert crash_log.read_crash_tail(tmp_path) == ""


def test_read_crash_tail_returns_last_lines(tmp_path: Path):
    path = tmp_path / "crash.log"
    path.write_text("\n".join(f"line {i}" for i in range(500)),
                    encoding="utf-8")
    tail = crash_log.read_crash_tail(tmp_path, max_lines=100)
    lines = tail.splitlines()
    assert len(lines) == 100
    assert lines[-1] == "line 499"
    assert lines[0] == "line 400"


def test_default_log_dir_matches_settings_convention():
    """crash.log must land next to backend.log. crash_log duplicates the
    USER_DATA_DIR resolution (it can't import config.settings — dotenv
    isn't in the CI venv), so pin the shape here: the leaf directory is
    MeetingRecorder on every platform."""
    assert crash_log.default_log_dir().name == "MeetingRecorder"
