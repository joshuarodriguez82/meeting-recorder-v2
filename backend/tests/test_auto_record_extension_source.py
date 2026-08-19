"""
Auto-record must fire for extension-sourced meetings.

THE BUG
-------
The global Auto-record toggle was on, every row in the Record tab said
"Manual only", and nothing ever auto-started. The user's entire calendar
came from the Chrome extension (they'd lost local Outlook access on that
machine), and `AutoRecordService` was constructed with
`calendar_service.get_upcoming_meetings` / `get_todays_meetings` — the
LOCAL calendar only. So the panel listed every meeting while the trigger
loop could see none of them: a display path and a trigger path
disagreeing about what exists.

WHAT THIS FILE PINS DOWN
------------------------
  1. ONE SOURCE OF TRUTH. server.py wires AutoRecordService to the very
     same `_merged_upcoming` / `_merged_today` that `GET
     /calendar/upcoming` renders (services/calendar_feed.py). Asserted by
     identity, not by behavior — two implementations that agree today
     are free to drift tomorrow, and drift between what the panel shows
     and what auto-record acts on is the whole bug.
  2. An extension-only meeting in window actually starts a recording.
  3. The same meeting arriving from BOTH sources starts exactly one.
  4. Naive-local extension timestamps ("2026-08-14T08:30:00", the real
     on-disk shape) fire at the right wall-clock moment, and an aware
     timestamp for the same instant fires identically (it's converted,
     never compared raw — an aware-vs-naive comparison raises TypeError
     inside the tick, which is swallowed as "tick failed" and silently
     kills auto-record for good).
  5. calendar_source="off" suppresses the extension path entirely,
     matching the endpoint.
  6. The pre-existing guards still hold: already handled, already
     recording, already over, all-day, blocklisted.
  7. A per-meeting opt-out survives a re-import of the extension
     calendar — including the start-time jitter and the "Updated!"
     subject decoration a real re-capture introduces. An opt-out that
     resets itself is worse than none.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("dotenv", MagicMock())

from _app_import import import_app  # noqa: E402
from services import calendar_feed  # noqa: E402
from services.auto_record_blocklist_service import (  # noqa: E402
    AutoRecordBlocklistService,
)
from services.auto_record_service import AutoRecordService  # noqa: E402
from services.extension_calendar_service import (  # noqa: E402
    ExtensionCalendarService,
)

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402


# ── harness ──────────────────────────────────────────────────────────

def _ext_event(subject: str, start: datetime, minutes: int = 30) -> dict:
    """One event in the shape `events_from_structured` produces (what
    the extension's Outlook Web scrape actually delivers)."""
    return {
        "subject": subject,
        "start": start,
        "end": start + timedelta(minutes=minutes),
        "location": "", "organizer": "", "attendees": [],
        "duration": minutes, "join_url": "", "source": "extension",
    }


def _local_event(subject: str, start: datetime, minutes: int = 30) -> dict:
    """One event in the shape the local backends produce — NAIVE LOCAL
    datetimes (see calendar_feed's TIMEZONES section: Outlook COM's
    `_to_local_naive` and EventKit's `fromtimestamp` both land here)."""
    return {
        "subject": subject,
        "start": start,
        "end": start + timedelta(minutes=minutes),
        "location": "Teams", "organizer": "boss@x.com",
        "attendees": ["real@x.com"], "duration": minutes,
    }


class _Harness:
    """server.py's real wiring, with only the outermost edges faked:
    the local calendar function, the extension store (a REAL
    ExtensionCalendarService on tmp_path — the JSON round trip is where
    naive-local ISO strings actually come from), the recording flag and
    the start call.
    """

    def __init__(self, tmp_path, monkeypatch, *, calendar_source="auto"):
        self.started: list[dict] = []
        self.recording = False
        self.local_events: list[dict] = []
        self.ext_svc = ExtensionCalendarService(tmp_path)
        self.blocklist = AutoRecordBlocklistService(tmp_path)

        monkeypatch.setattr(
            server.svc, "settings",
            SimpleNamespace(calendar_source=calendar_source,
                            auto_record_enabled=True))
        monkeypatch.setattr(
            server.svc, "load_settings", lambda: server.svc.settings)
        monkeypatch.setattr(
            server.svc, "extension_calendar_svc", self.ext_svc)
        monkeypatch.setattr(
            server.svc, "auto_record_blocklist_svc", self.blocklist)
        monkeypatch.setattr(
            server, "get_upcoming_meetings", lambda hours: list(self.local_events))
        monkeypatch.setattr(
            server, "get_todays_meetings", lambda: list(self.local_events))

        self.service = AutoRecordService(
            # EXACTLY what server._ensure_auto_record_service injects —
            # see test_auto_record_is_wired_to_the_shared_merged_view.
            get_upcoming_meetings=server._merged_upcoming,
            get_todays_meetings=server._merged_today,
            is_recording=lambda: self.recording,
            start_recording=self.started.append,
            is_enabled=lambda: True,
            is_blocked=self.blocklist.is_blocked,
        )

    def import_extension_calendar(self, events, now=None) -> None:
        """Whole-store re-import, exactly like /briefing/extension-import
        does on every capture."""
        self.ext_svc.replace_all(events, now=now)

    def tick(self) -> None:
        asyncio.run(self.service._tick())

    def panel(self, hours: int = 168) -> list[dict]:
        """What the Record tab would render right now."""
        return asyncio.run(server.get_calendar_upcoming(hours=hours, refresh=False))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


def _in_window(minutes_ago: int = 5, length: int = 30) -> datetime:
    """A start time whose meeting window is open RIGHT NOW. Seconds are
    zeroed the way both calendar sources deliver them."""
    return (datetime.now() - timedelta(minutes=minutes_ago)).replace(
        second=0, microsecond=0)


# ── 1. one source of truth ───────────────────────────────────────────

def test_auto_record_is_wired_to_the_shared_merged_view(monkeypatch):
    """The anti-drift assertion. AutoRecordService must be fed the SAME
    functions /calendar/upcoming renders — not a second implementation
    that happens to agree today."""
    monkeypatch.setattr(
        server.svc, "settings",
        SimpleNamespace(calendar_source="auto", auto_record_enabled=False))
    monkeypatch.setattr(server.svc, "load_settings", lambda: server.svc.settings)
    monkeypatch.setattr(server.svc, "auto_record_svc", None)

    server._ensure_auto_record_service()

    auto = server.svc.auto_record_svc
    assert auto is not None
    assert auto._get_upcoming is server._merged_upcoming
    assert auto._get_todays is server._merged_today


def test_panel_and_trigger_see_the_same_extension_meeting(harness):
    """Same meeting, both views, one merge."""
    start = _in_window()
    harness.import_extension_calendar([_ext_event("Extension Only", start)])

    panel_subjects = {m["subject"] for m in harness.panel()}
    trigger_subjects = {
        m["subject"] for m in asyncio.run(server._merged_today())}

    assert "Extension Only" in panel_subjects
    assert "Extension Only" in trigger_subjects


# ── 2. an extension-only meeting triggers auto-record ────────────────

def test_extension_only_meeting_starts_a_recording(harness):
    start = _in_window()
    harness.import_extension_calendar([_ext_event("Extension Only", start)])
    assert harness.local_events == []  # no local calendar at all

    harness.tick()

    assert [m["subject"] for m in harness.started] == ["Extension Only"]
    started = harness.started[0]
    assert started["source"] == "extension"
    # The start adapter builds a session name and a scheduled_end_iso off
    # these, so they must be real datetimes, not the ISO strings the
    # store holds.
    assert isinstance(started["start"], datetime)
    assert isinstance(started["end"], datetime)


def test_extension_only_meeting_triggers_under_extension_source(tmp_path, monkeypatch):
    """calendar_source="extension" — the mode this user is actually in.
    The local calendar is never consulted (a stray Outlook touch is what
    re-triggers their tenant's sign-in prompt) and auto-record still
    fires."""
    h = _Harness(tmp_path, monkeypatch, calendar_source="extension")

    def _must_not_run(*a, **kw):
        raise AssertionError("local calendar must not be touched")

    monkeypatch.setattr(server, "get_todays_meetings", _must_not_run)
    monkeypatch.setattr(server, "get_upcoming_meetings", _must_not_run)
    h.import_extension_calendar([_ext_event("Extension Only", _in_window())])

    h.tick()

    assert [m["subject"] for m in h.started] == ["Extension Only"]


# ── 3. present in both sources → exactly one start ───────────────────

def test_meeting_in_both_sources_starts_exactly_once(harness):
    start = _in_window()
    # Same meeting, 3 minutes apart and subject-decorated by OWA —
    # inside the ±5-minute dedup tolerance, so one meeting, not two.
    harness.local_events = [_local_event("Weekly Sync", start)]
    harness.import_extension_calendar(
        [_ext_event("Updated! Weekly Sync", start + timedelta(minutes=3))])

    harness.tick()

    assert len(harness.started) == 1, harness.started
    # The local copy wins the collision — it carries the organizer and
    # real attendee list the scrape can't reconstruct.
    assert harness.started[0]["source"] == "outlook"
    assert harness.started[0]["organizer"] == "boss@x.com"


def test_a_second_tick_does_not_start_the_same_meeting_again(harness):
    harness.import_extension_calendar([_ext_event("Extension Only", _in_window())])
    harness.tick()
    harness.tick()
    harness.tick()
    assert len(harness.started) == 1


def test_re_imported_meeting_with_jittered_start_does_not_start_twice(harness):
    """Extension events are re-imported on every capture and the start
    can move a minute or two (OWA rounding / an LLM-recovered time). The
    handled-ledger matches with the same ±5min tolerance the merge uses,
    so the second capture is recognized as the same occurrence rather
    than a brand-new meeting to pounce on."""
    start = _in_window()
    harness.import_extension_calendar([_ext_event("Standup", start)])
    harness.tick()
    assert len(harness.started) == 1

    # Recording ended (user stopped it), calendar re-imported 2 min off.
    harness.import_extension_calendar(
        [_ext_event("Standup", start + timedelta(minutes=2))])
    harness.tick()

    assert len(harness.started) == 1, harness.started


# ── 4. timezones ─────────────────────────────────────────────────────

def test_naive_local_extension_timestamp_fires_at_the_right_wall_clock(harness):
    """The on-disk shape is a NAIVE LOCAL ISO string
    ("2026-08-14T08:30:00"), and the trigger compares against
    datetime.now() — also naive local. A meeting whose local wall-clock
    window is open fires; one written with the same digits but shifted
    six hours does not."""
    start = _in_window()
    harness.import_extension_calendar([_ext_event("Now Meeting", start)])
    stored = harness.ext_svc.get_events()[0]
    # Round-tripped through JSON as a naive ISO string — no offset, no Z.
    assert stored["start"].tzinfo is None

    harness.tick()
    assert [m["subject"] for m in harness.started] == ["Now Meeting"]

    # Same digits, six hours out of window: must NOT fire.
    harness.started.clear()
    harness.import_extension_calendar(
        [_ext_event("Six Hours Off", start + timedelta(hours=6))])
    harness.tick()
    assert harness.started == []


def test_aware_extension_timestamp_is_converted_not_compared_raw(harness):
    """An aware timestamp for the SAME instant must fire identically.
    If it reached the trigger still aware, `start <= now` would raise
    TypeError, the tick would swallow it as "tick failed", and
    auto-record would silently never fire again."""
    local_start = _in_window()
    # Same instant, expressed in a tz six hours ahead of local.
    aware = local_start.astimezone().astimezone(
        timezone(timedelta(hours=6))) if local_start.tzinfo else \
        local_start.astimezone(timezone.utc).astimezone(
            timezone(timedelta(hours=6)))
    event = _ext_event("Aware Meeting", local_start)
    event["start"] = aware.isoformat()
    event["end"] = (aware + timedelta(minutes=30)).isoformat()
    harness.import_extension_calendar([event])

    harness.tick()

    assert [m["subject"] for m in harness.started] == ["Aware Meeting"]
    assert harness.started[0]["start"].tzinfo is None
    assert harness.started[0]["start"] == local_start


def test_aware_local_calendar_event_does_not_kill_the_tick(harness):
    """Defense for the other direction: a local backend handing back an
    aware datetime must not poison the comparison for every OTHER
    meeting on the list."""
    start = _in_window()
    aware_local = _local_event("Aware Local", start)
    aware_local["start"] = start.astimezone()
    aware_local["end"] = (start + timedelta(minutes=30)).astimezone()
    harness.local_events = [aware_local]
    harness.import_extension_calendar([_ext_event("Extension Only", start)])

    harness.tick()

    assert len(harness.started) == 1
    assert harness.started[0]["start"].tzinfo is None


# ── 5. calendar_source="off" ─────────────────────────────────────────

def test_calendar_source_off_suppresses_the_extension_path(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch, calendar_source="off")
    h.local_events = [_local_event("Local Meeting", _in_window())]
    h.import_extension_calendar([_ext_event("Extension Only", _in_window())])

    h.tick()

    assert h.started == []
    assert h.panel() == []
    assert h.service.next_event is None


def test_calendar_source_off_does_not_even_read_the_extension_store(
        tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch, calendar_source="off")
    reads: list[str] = []

    def _spy(hours=None):
        reads.append("get_events")
        return [_ext_event("Extension Only", _in_window())]

    monkeypatch.setattr(server.svc, "extension_calendar_svc",
                        SimpleNamespace(get_events=_spy))
    h.tick()

    assert h.started == []
    assert reads == []


# ── 6. the pre-existing guards still hold ────────────────────────────

def test_does_not_start_while_a_recording_is_in_progress(harness):
    """"Manual recordings always win" — and the meeting is marked
    handled so auto-record doesn't pounce the instant the user stops."""
    harness.recording = True
    harness.import_extension_calendar([_ext_event("Extension Only", _in_window())])

    harness.tick()
    assert harness.started == []

    harness.recording = False
    harness.tick()
    assert harness.started == [], "auto-record pounced after Stop"


def test_does_not_start_a_meeting_that_is_already_over(harness):
    over = (datetime.now() - timedelta(hours=3)).replace(second=0, microsecond=0)
    harness.local_events = [_local_event("Finished Meeting", over, minutes=30)]
    # replace_all clips to now-1d..now+14d, so a 3h-old event is still
    # retained in the store; it just must never trigger.
    harness.import_extension_calendar(
        [_ext_event("Finished Extension Meeting", over, minutes=30)])

    harness.tick()

    assert harness.started == []


def test_does_not_start_a_meeting_that_has_not_begun(harness):
    later = (datetime.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
    harness.import_extension_calendar([_ext_event("Later Meeting", later)])

    harness.tick()

    assert harness.started == []
    # …but it IS the "next: …" hint, from the same merged view.
    assert harness.service.next_event is not None
    assert harness.service.next_event["subject"] == "Later Meeting"


def test_all_day_extension_event_never_triggers(harness):
    """The one row that still honestly says "Manual only" in the UI."""
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    harness.import_extension_calendar(
        [_ext_event("Out of Office", midnight, minutes=24 * 60)])

    harness.tick()

    assert harness.started == []


# ── 7. per-meeting opt-out survives a re-import ──────────────────────

def test_opt_out_blocks_an_extension_meeting(harness):
    start = _in_window()
    harness.import_extension_calendar([_ext_event("Weekly Sync", start)])
    harness.blocklist.add("Weekly Sync")

    harness.tick()

    assert harness.started == []


def test_opt_out_survives_a_calendar_re_import(harness):
    """Extension events are replaced wholesale on every capture, and the
    re-captured copy can come back with a jittered start AND an OWA
    "Updated!" subject decoration. The opt-out is keyed on the subject
    (the only thing that survives a re-import — the store carries no
    stable id) and matched canonically, so it still holds."""
    start = _in_window()
    harness.import_extension_calendar([_ext_event("Weekly Sync", start)])
    harness.blocklist.add("Weekly Sync")

    # Re-import: same meeting, decorated subject, 2 minutes off.
    harness.import_extension_calendar(
        [_ext_event("Updated! Weekly Sync", start + timedelta(minutes=2))])
    # A fresh service instance stands in for a backend restart: the
    # opt-out must be on disk, not in the handled-ledger.
    harness.service = AutoRecordService(
        get_upcoming_meetings=server._merged_upcoming,
        get_todays_meetings=server._merged_today,
        is_recording=lambda: harness.recording,
        start_recording=harness.started.append,
        is_enabled=lambda: True,
        is_blocked=AutoRecordBlocklistService(
            harness.blocklist.path.parent).is_blocked,
    )

    harness.tick()

    assert harness.started == []


def test_opt_out_can_be_lifted_after_the_subject_was_decorated(harness):
    """A block the tile SHOWS must be one the tile can lift — otherwise
    the button renders as "Auto-record off" and does nothing."""
    harness.blocklist.add("Weekly Sync")
    assert harness.blocklist.is_blocked({"subject": "Updated! Weekly Sync"})

    assert harness.blocklist.remove("Updated! Weekly Sync") is True
    assert harness.blocklist.is_blocked({"subject": "Weekly Sync"}) is False
    assert harness.blocklist.list_all() == []


def test_blocklist_add_is_idempotent_across_subject_decoration(harness):
    harness.blocklist.add("Weekly Sync")
    harness.blocklist.add("Updated! Weekly Sync")
    assert harness.blocklist.list_all() == ["Weekly Sync"]


# ── degradation ──────────────────────────────────────────────────────

def test_broken_local_calendar_does_not_withhold_extension_meetings(
        harness, monkeypatch):
    """The user's actual machine state: local Outlook is unreachable.
    That must degrade to "extension only", never take the trigger loop
    down with it."""
    def _boom(*a, **kw):
        raise RuntimeError("Outlook COM is not available")

    harness.local_events = []
    harness.import_extension_calendar([_ext_event("Extension Only", _in_window())])
    monkeypatch.setattr(server, "get_todays_meetings", _boom)
    monkeypatch.setattr(server, "get_upcoming_meetings", _boom)

    harness.tick()

    assert [m["subject"] for m in harness.started] == ["Extension Only"]


def test_aware_meeting_that_bypassed_the_feed_is_skipped_not_fatal():
    """Belt and braces for a caller that injects meetings WITHOUT going
    through calendar_feed: the un-comparable meeting is skipped
    individually. If it were allowed through, `start <= now` would raise
    TypeError and the tick would abandon every other meeting on the
    list — the silent, total failure mode."""
    started: list[dict] = []
    good_start = _in_window()
    bad = _local_event("Aware Meeting", good_start)
    bad["start"] = bad["start"].astimezone()
    bad["end"] = bad["end"].astimezone()
    good = _local_event("Naive Meeting", good_start)

    service = AutoRecordService(
        get_upcoming_meetings=lambda hours: [],
        get_todays_meetings=lambda: [bad, good],
        is_recording=lambda: False,
        start_recording=started.append,
        is_enabled=lambda: True,
    )
    asyncio.run(service._tick())

    assert [m["subject"] for m in started] == ["Naive Meeting"]


def test_merged_today_returns_naive_local_datetimes(harness):
    """The invariant every comparison downstream depends on — asserted
    with an AWARE local event in the list, since that's the shape
    `_normalize_times` exists to absorb (the extension side is already
    coerced by the merge itself)."""
    start = _in_window()
    aware_local = _local_event("Local", start + timedelta(minutes=10))
    aware_local["start"] = aware_local["start"].astimezone()
    aware_local["end"] = aware_local["end"].astimezone()
    harness.local_events = [aware_local]
    harness.import_extension_calendar([_ext_event("Extension", start)])

    for m in asyncio.run(server._merged_today()):
        assert isinstance(m["start"], datetime) and m["start"].tzinfo is None
        assert isinstance(m["end"], datetime) and m["end"].tzinfo is None


def test_trigger_horizon_default_is_documented_and_wide_enough():
    """A shrunken horizon would silently drop in-progress meetings from
    the trigger view."""
    assert calendar_feed.TRIGGER_HORIZON_HOURS >= 24
