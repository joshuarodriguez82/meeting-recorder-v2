"""
Extension self-reported version bookkeeping.

The v2.28.0 field bug: the extension never sent its own version and
nothing on the backend recorded one, so a stale extension posting
stale data looked identical to a current one. Fixed by
``ExtensionCalendarService.record_extension_version`` — called on
every extension POST from ``/briefing/extension-import`` (see
test_extension_calendar_import_endpoint.py for the endpoint-level
coverage) — plus ``capture_status`` surfacing what it recorded.

No optional deps: pure Python + a JSON file, same pattern
test_extension_calendar_merge.py uses.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from services.extension_calendar_service import ExtensionCalendarService


def test_record_extension_version_then_capture_status(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 14, 9, 0, 0)

    svc.record_extension_version("1.2.0", now=now)
    status = svc.capture_status(now=now)

    assert status["last_seen_version"] == "1.2.0"
    assert status["last_seen_version_at"] == now.isoformat()


def test_absent_version_recorded_as_unknown_not_assumed_current(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 14, 9, 0, 0)

    svc.record_extension_version(None, now=now)
    status = svc.capture_status(now=now)

    # A POST DID happen (last_seen_version_at is set) but its version
    # is unknown -- must read as neither "1.2.0" nor "never posted".
    assert status["last_seen_version"] is None
    assert status["last_seen_version_at"] == now.isoformat()


def test_blank_version_string_also_recorded_as_unknown(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    svc.record_extension_version("   ", now=datetime(2026, 8, 14, 9, 0, 0))
    status = svc.capture_status()
    assert status["last_seen_version"] is None


def test_never_recorded_is_distinct_from_recorded_unknown(tmp_path: Path):
    """capture_status on a store that has NEVER been POSTed to at all
    must be distinguishable from one that HAS been posted to by a
    version-less (pre-1.2.0) extension."""
    never = ExtensionCalendarService(tmp_path / "never")
    posted_unknown = ExtensionCalendarService(tmp_path / "posted")
    posted_unknown.record_extension_version(
        None, now=datetime(2026, 8, 14, 9, 0, 0))

    never_status = never.capture_status()
    posted_status = posted_unknown.capture_status()

    assert never_status["last_seen_version_at"] is None
    assert posted_status["last_seen_version_at"] is not None
    assert never_status["last_seen_version"] == posted_status["last_seen_version"] is None


def test_a_later_post_updates_the_recorded_version(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    svc.record_extension_version("1.1.0", now=datetime(2026, 8, 1, 8, 0, 0))
    svc.record_extension_version("1.2.0", now=datetime(2026, 8, 14, 8, 0, 0))

    status = svc.capture_status()
    assert status["last_seen_version"] == "1.2.0"


def test_replace_all_preserves_a_previously_recorded_version(tmp_path: Path):
    """The calendar-refresh alarm's plain event write (replace_all)
    must not clobber version bookkeeping a different POST already
    recorded on the same store -- they're independent concerns sharing
    one JSON file."""
    svc = ExtensionCalendarService(tmp_path)
    svc.record_extension_version("1.2.0", now=datetime(2026, 8, 14, 8, 0, 0))

    svc.replace_all([], now=datetime(2026, 8, 14, 8, 30, 0))

    status = svc.capture_status()
    assert status["last_seen_version"] == "1.2.0"


def test_record_extension_version_preserves_existing_events(tmp_path: Path):
    """The reverse direction: recording a version must not clobber
    events a prior replace_all wrote."""
    svc = ExtensionCalendarService(tmp_path)
    start = datetime(2026, 8, 14, 10, 0, 0)
    svc.replace_all(
        [{"subject": "Standup", "start": start,
          "end": start.replace(hour=10, minute=15)}],
        now=datetime(2026, 8, 14, 9, 0, 0))

    svc.record_extension_version("1.2.0", now=datetime(2026, 8, 14, 9, 5, 0))

    events = svc.get_events()
    assert len(events) == 1
    assert events[0]["subject"] == "Standup"
