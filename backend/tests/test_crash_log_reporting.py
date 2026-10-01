"""
A crash is reported once, not on every start.

Diagnostics bundle 2026-10-01: 96 backend starts, 96 backend.prior_crash
events — every one about the same 35-day-old crash. An event that is
always present carries no information, and would make any crash alert
built on it fire forever.
"""

from __future__ import annotations

from utils import crash_log


def _crash(dir_, stamp="2026-08-20T13:00:35"):
    (dir_ / crash_log.CRASH_LOG_NAME).write_text(
        f"=== faulthandler enabled {stamp} pid=1 python=3.13 "
        f"platform=win32 ===\n"
        "Windows fatal exception: access violation\n\n"
        "Current thread 0x0001 (most recent call first):\n"
        '  File "server.py", line 1 in <module>\n', encoding="utf-8")


def test_a_crash_is_reported_on_the_first_start_after_it(tmp_path):
    _crash(tmp_path)
    assert crash_log.unreported_crash_time(tmp_path) is not None


def test_and_not_again_on_later_starts(tmp_path):
    _crash(tmp_path)
    crash_log.unreported_crash_time(tmp_path)
    assert crash_log.unreported_crash_time(tmp_path) is None
    assert crash_log.unreported_crash_time(tmp_path) is None


def test_a_new_crash_is_reported(tmp_path):
    _crash(tmp_path)
    crash_log.unreported_crash_time(tmp_path)
    with open(tmp_path / crash_log.CRASH_LOG_NAME, "a",
              encoding="utf-8") as f:
        f.write("=== faulthandler enabled 2026-10-01T09:00:00 pid=2 "
                "python=3.13 platform=win32 ===\n"
                "Windows fatal exception: access violation\n")
    assert crash_log.unreported_crash_time(tmp_path) is not None


def test_no_crash_reports_nothing(tmp_path):
    assert crash_log.unreported_crash_time(tmp_path) is None
