"""
Extension-scraped calendar events as a SECOND source for the Record
tab's Upcoming Meetings list.

The bug this covers: the Chrome extension's Outlook Web scrape fed only
the Today tab's daily briefing, while the Record tab read the LOCAL
calendar (Outlook COM / EventKit). A meeting the extension could see
and local Outlook could not — the whole reason the extension exists —
never appeared where you could record it (field report 2026-08-11).

No optional deps: everything under test is pure Python + a JSON file.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from services.extension_calendar_service import (
    DEDUP_TOLERANCE,
    DEFAULT_DURATION_MIN,
    ExtensionCalendarService,
    describe_structured_source,
    events_from_briefing,
    events_from_structured,
    merge_meetings,
    normalize_subject,
    parse_clock_time,
    parse_duration_minutes,
)


def local(subject: str, start: datetime, minutes: int = 30, **extra) -> dict:
    m = {
        "subject": subject,
        "start": start,
        "end": start + timedelta(minutes=minutes),
        "location": "Teams",
        "organizer": "Someone Real",
        "attendees": ["Real Person"],
        "duration": minutes,
    }
    m.update(extra)
    return m


def ext(subject: str, start: datetime, minutes: int = 30, **extra) -> dict:
    m = {
        "subject": subject,
        "start": start,
        "end": start + timedelta(minutes=minutes),
        "location": "",
        "organizer": "",
        "attendees": [],
        "duration": minutes,
        "join_url": "",
        "source": "extension",
    }
    m.update(extra)
    return m


# ── subject normalization ───────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("AWS Town Hall", "aws town hall"),
    ("  AWS   Town    Hall  ", "aws town hall"),
    ("Updated! AWS Town Hall", "aws town hall"),
    ("Updated: AWS Town Hall", "aws town hall"),
    ("RE: AWS Town Hall", "aws town hall"),
    ("FW: AWS Town Hall", "aws town hall"),
    ("Fwd: AWS Town Hall", "aws town hall"),
    ("FW: RE: AWS Town Hall", "aws town hall"),
    ("Canceled: AWS Town Hall", "aws town hall"),
    ("aws town hall", "aws town hall"),
])
def test_normalize_subject(raw, expected):
    assert normalize_subject(raw) == expected


def test_normalize_subject_keeps_distinct_meetings_distinct():
    assert normalize_subject("Weekly Sync") != normalize_subject("Weekly Standup")


# ── dedup / merge ───────────────────────────────────────────────────

def test_dedup_matches_on_normalized_subject_and_prefers_local():
    t = datetime(2026, 8, 11, 9, 0)
    merged = merge_meetings(
        [local("AWS Town Hall", t)],
        [ext("Updated! AWS Town Hall", t)],
    )
    assert len(merged) == 1
    assert merged[0]["source"] == "outlook"
    # The local copy's richer data survives — that's the whole point of
    # preferring it.
    assert merged[0]["organizer"] == "Someone Real"
    assert merged[0]["attendees"] == ["Real Person"]


def test_dedup_tolerance_is_five_minutes_either_way():
    t = datetime(2026, 8, 11, 9, 0)
    for delta in (timedelta(minutes=-5), timedelta(minutes=-1),
                  timedelta(0), timedelta(minutes=4),
                  timedelta(minutes=5)):
        merged = merge_meetings([local("Weekly Sync", t)],
                                [ext("Weekly Sync", t + delta)])
        assert len(merged) == 1, f"should dedup at {delta}"
        assert merged[0]["source"] == "outlook"


def test_start_outside_tolerance_is_a_separate_meeting():
    t = datetime(2026, 8, 11, 9, 0)
    merged = merge_meetings(
        [local("Weekly Sync", t)],
        [ext("Weekly Sync", t + DEDUP_TOLERANCE + timedelta(minutes=1))],
    )
    assert len(merged) == 2
    assert {m["source"] for m in merged} == {"outlook", "extension"}


def test_extension_only_events_survive_the_merge():
    t = datetime(2026, 8, 11, 9, 0)
    merged = merge_meetings(
        [local("Weekly Sync", t)],
        [ext("OWA-only Client Call", t + timedelta(hours=2))],
    )
    assert [m["subject"] for m in merged] == [
        "Weekly Sync", "OWA-only Client Call"]
    assert merged[1]["source"] == "extension"


def test_local_only_event_is_unaffected():
    t = datetime(2026, 8, 11, 9, 0)
    original = local("Weekly Sync", t)
    merged = merge_meetings([original], [])
    assert len(merged) == 1
    m = merged[0]
    # Every pre-existing field keeps its name AND its value — the
    # frontend and auto_record_service read these.
    for key in ("subject", "start", "end", "location", "organizer",
                "attendees", "duration"):
        assert m[key] == original[key]
    assert m["source"] == "outlook"
    # ...and the input dict was not mutated.
    assert "source" not in original


def test_merge_sorts_by_start_across_both_sources():
    base = datetime(2026, 8, 11, 9, 0)
    merged = merge_meetings(
        [local("Third", base + timedelta(hours=3))],
        [ext("First", base), ext("Second", base + timedelta(hours=1))],
    )
    assert [m["subject"] for m in merged] == ["First", "Second", "Third"]


def test_merge_accepts_iso_strings_for_start():
    """Local backends emit datetimes, but the store round-trips through
    ISO text. Both must dedupe against each other."""
    merged = merge_meetings(
        [{"subject": "Weekly Sync", "start": "2026-08-11T09:00:00",
          "end": "2026-08-11T09:30:00"}],
        [ext("Weekly Sync", datetime(2026, 8, 11, 9, 2))],
    )
    assert len(merged) == 1
    assert merged[0]["source"] == "outlook"


def test_merge_drops_extension_events_with_no_usable_start():
    merged = merge_meetings([], [{"subject": "Mystery", "start": "not a date"}])
    assert merged == []


# ── briefing → events ───────────────────────────────────────────────

def test_events_from_briefing_uses_start_iso_when_present():
    briefing = {
        "date": "2026-08-11",
        "agenda": [{
            "title": "Discovery Call",
            "time": "9:30 AM",
            "start_iso": "2026-08-11T09:30:00",
            "end_iso": "2026-08-11T10:15:00",
            "join_url": "https://teams.microsoft.com/l/meetup-join/abc",
            "duration": "30 min",
            "status": "scheduled",
            "attendees": ["Dana"],
        }],
    }
    events = events_from_briefing(briefing)
    assert len(events) == 1
    e = events[0]
    assert e["start"] == datetime(2026, 8, 11, 9, 30)
    assert e["end"] == datetime(2026, 8, 11, 10, 15)
    assert e["duration"] == 45
    assert e["join_url"].endswith("/abc")
    assert e["source"] == "extension"
    assert e["attendees"] == ["Dana"]


def test_events_from_briefing_falls_back_to_date_plus_display_time():
    """Briefings parsed before start_iso existed (and any run where the
    model omits it) must still place on the timeline."""
    briefing = {
        "date": "2026-08-11",
        "agenda": [{"title": "Weekly Sync", "time": "2:00 PM",
                    "duration": "1 hr", "status": "scheduled"}],
    }
    events = events_from_briefing(briefing)
    assert len(events) == 1
    assert events[0]["start"] == datetime(2026, 8, 11, 14, 0)
    assert events[0]["end"] == datetime(2026, 8, 11, 15, 0)


def test_events_from_briefing_skips_untimed_and_cancelled():
    briefing = {
        "date": "2026-08-11",
        "agenda": [
            {"title": "All-day offsite", "time": "All day"},
            {"title": "Dropped call", "time": "3:00 PM",
             "status": "cancelled"},
            {"title": "", "time": "4:00 PM"},
            {"title": "Real one", "time": "4:00 PM"},
        ],
    }
    assert [e["subject"] for e in events_from_briefing(briefing)] == ["Real one"]


def test_events_from_briefing_converts_aware_iso_to_naive_local():
    """The model likes appending 'Z'. Mixing aware and naive datetimes
    would raise on the very first comparison in merge/auto-record."""
    briefing = {
        "date": "2026-08-11",
        "agenda": [{"title": "Sync", "start_iso": "2026-08-11T09:00:00+00:00"}],
    }
    e = events_from_briefing(briefing)[0]
    assert e["start"].tzinfo is None


@pytest.mark.parametrize("raw,expected", [
    ("9:30 AM", (9, 30)), ("9 AM", (9, 0)), ("12:15 AM", (0, 15)),
    ("12:00 PM", (12, 0)), ("1 pm", (13, 0)), ("09:30", (9, 30)),
    ("14:00", (14, 0)), ("All day", None), ("", None), ("25:00", None),
])
def test_parse_clock_time(raw, expected):
    assert parse_clock_time(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("30 min", 30), ("45m", 45), ("1 hr", 60), ("1.5 hours", 90),
    ("2h", 120), ("", 30), ("nonsense", 30),
])
def test_parse_duration_minutes(raw, expected):
    assert parse_duration_minutes(raw) == expected


# ── store ───────────────────────────────────────────────────────────

def test_store_round_trips(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 11, 12, 0)
    kept = svc.replace_all(
        [ext("Client Call", now + timedelta(hours=2), join_url="https://x/y")],
        now=now)
    assert len(kept) == 1

    back = ExtensionCalendarService(tmp_path).get_events()
    assert len(back) == 1
    assert back[0]["subject"] == "Client Call"
    assert back[0]["start"] == now + timedelta(hours=2)
    assert back[0]["end"] == now + timedelta(hours=2, minutes=30)
    assert back[0]["join_url"] == "https://x/y"
    assert back[0]["source"] == "extension"


def test_store_write_is_atomic_and_leaves_no_temp_files(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 11, 12, 0)
    svc.replace_all([ext("A", now + timedelta(hours=1))], now=now)
    svc.replace_all([ext("B", now + timedelta(hours=1))], now=now)

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "extension_calendar.json"]
    # Replaced wholesale — stale events must not accumulate.
    assert [e["subject"] for e in svc.get_events()] == ["B"]
    # And the file on disk is complete, parseable JSON.
    data = json.loads((tmp_path / "extension_calendar.json").read_text())
    assert data["events"][0]["subject"] == "B"


def test_events_outside_retention_window_are_dropped_on_import(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 11, 12, 0)
    kept = svc.replace_all([
        ext("Too old", now - timedelta(days=2)),
        ext("Yesterday, still in window", now - timedelta(hours=6)),
        ext("Soon", now + timedelta(hours=3)),
        ext("Next week", now + timedelta(days=7)),
        ext("Too far out", now + timedelta(days=20)),
    ], now=now)
    assert [e["subject"] for e in kept] == [
        "Yesterday, still in window", "Soon", "Next week"]
    assert len(svc.get_events()) == 3


def test_get_events_within_hours_clips_and_keeps_in_progress(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 11, 12, 0)
    svc.replace_all([
        ext("Already finished", now - timedelta(hours=4)),
        ext("In progress", now - timedelta(minutes=10), minutes=60),
        ext("Later today", now + timedelta(hours=3)),
        ext("Beyond horizon", now + timedelta(days=5)),
    ], now=now)

    got = svc.get_events(within_hours=24, now=now)
    assert [e["subject"] for e in got] == ["In progress", "Later today"]


def test_get_events_tolerates_missing_and_corrupt_store(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    assert svc.get_events() == []
    (tmp_path / "extension_calendar.json").write_text("{not json",
                                                      encoding="utf-8")
    # A corrupt store must never take the local calendar down with it.
    assert svc.get_events() == []


# ── structured events (client-parsed from Outlook Web aria-labels) ──
#
# The extension parses aria-label strings itself (see
# chrome-extension/background.js's parseMeetingLabel /
# extractEventsFromCandidates, exercised directly by the Node harness
# in that change) and POSTs already-structured JSON. This is what the
# backend receives on that path — validate/coerce only, no LLM.

def test_events_from_structured_realistic_day_all_five_captured():
    """The regression that matters: the field report was 1 of 5 real
    meetings surviving the old text/LLM path. These are shaped exactly
    like the user's real subjects (pipe, slash, FW: prefix, trailing
    space) as already parsed client-side into start/end ISO strings."""
    raw = [
        {"subject": "AWS Daily Pulse Call",
         "start": "2026-08-13T10:00:00", "end": "2026-08-13T10:15:00",
         "location": "Microsoft Teams Meeting", "organizer": "Gülşah Göymen"},
        {"subject": "PRIORITY: AWS Sales| Active Project Status Reviews and Escalations",
         "start": "2026-08-13T10:00:00", "end": "2026-08-13T10:30:00",
         "organizer": "Will Treacy"},
        {"subject": "FW: AWS Connect - Italy / ECC next steps: weekly team connect",
         "start": "2026-08-13T11:30:00", "end": "2026-08-13T12:00:00"},
        {"subject": "AWS/PGE - IVA PoC Sync-up",
         "start": "2026-08-13T13:00:00", "end": "2026-08-13T13:30:00"},
        {"subject": "AI Transformation Stand Up",
         "start": "2026-08-13T07:30:00", "end": "2026-08-13T08:30:00"},
    ]
    events = events_from_structured(raw)
    assert len(events) == 5
    subjects = {e["subject"] for e in events}
    assert "PRIORITY: AWS Sales| Active Project Status Reviews and Escalations" in subjects
    assert "FW: AWS Connect - Italy / ECC next steps: weekly team connect" in subjects
    assert all(e["source"] == "extension" for e in events)


def test_events_from_structured_derives_duration_and_defaults_missing_end():
    events = events_from_structured([
        {"subject": "Ad-hoc", "start": "2026-08-13T09:00:00"},
    ])
    assert len(events) == 1
    assert events[0]["duration"] == DEFAULT_DURATION_MIN
    assert events[0]["end"] == events[0]["start"] + timedelta(minutes=DEFAULT_DURATION_MIN)


def test_events_from_structured_drops_items_without_subject_or_start():
    events = events_from_structured([
        {"subject": "", "start": "2026-08-13T09:00:00"},
        {"subject": "No start"},
        {"subject": "not a dict"},  # ignored by the isinstance guard
        None,
        {"subject": "Good", "start": "2026-08-13T09:00:00"},
    ])
    assert [e["subject"] for e in events] == ["Good"]


def test_events_from_structured_tolerates_end_before_start():
    events = events_from_structured([
        {"subject": "Backwards", "start": "2026-08-13T10:00:00",
         "end": "2026-08-13T09:00:00"},
    ])
    assert events[0]["end"] > events[0]["start"]


# ── observability: stats + path classification (field report chain
#    culminating 2026-08-14 — two calendar-parse paths produced
#    identically-shaped output with no way to tell which one ran) ────

def test_events_from_structured_stats_report_raw_kept_and_drop_reasons():
    stats: dict = {}
    events = events_from_structured([
        {"subject": "Good", "start": "2026-08-13T09:00:00"},
        {"subject": "", "start": "2026-08-13T09:00:00"},   # no subject
        {"subject": "No start"},                           # no start
        "not a dict",                                       # not a dict
        None,                                               # not a dict
    ], stats=stats)
    assert len(events) == 1
    assert stats["raw"] == 5
    assert stats["kept"] == 1
    assert stats["dropped_no_subject"] == 1
    assert stats["dropped_no_start"] == 1
    assert stats["dropped_not_dict"] == 2


def test_events_from_briefing_stats_report_raw_kept_and_drop_reasons():
    stats: dict = {}
    briefing = {
        "date": "2026-08-11",
        "agenda": [
            {"title": "Real one", "time": "4:00 PM"},
            {"title": "Dropped call", "time": "3:00 PM", "status": "cancelled"},
            {"title": "", "time": "4:00 PM"},
            {"title": "All-day offsite", "time": "All day"},
        ],
    }
    events = events_from_briefing(briefing, stats=stats)
    assert len(events) == 1
    assert stats["raw"] == 4
    assert stats["kept"] == 1
    assert stats["dropped_cancelled"] == 1
    assert stats["dropped_no_subject"] == 1
    assert stats["dropped_no_start"] == 1


def test_describe_structured_source_distinguishes_absent_empty_present():
    # Old extension (or a request that never built calendar_events at
    # all) — the key is missing, not an empty list.
    assert describe_structured_source(None) == "absent"
    # A current extension whose structured DOM scan ran and genuinely
    # found nothing this capture.
    assert describe_structured_source([]) == "empty"
    # At least one structured event was sent.
    assert describe_structured_source([{"subject": "x"}]) == "present"


# ── replace_all shrink guard (field report 2026-08-13) ──────────────

def test_replace_all_merges_instead_of_shrinking_on_partial_capture(tmp_path: Path):
    """A capture that returns fewer events than the store already has
    in-window must not wipe the extras — it merges instead."""
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 13, 12, 0)
    svc.replace_all([
        ext("A", now + timedelta(hours=1)),
        ext("B", now + timedelta(hours=2)),
        ext("C", now + timedelta(hours=3)),
    ], now=now)

    # New capture only saw A (fresh copy) and a brand new D — 2 < 3.
    kept = svc.replace_all([
        ext("A", now + timedelta(hours=1), location="Room 5"),
        ext("D", now + timedelta(hours=4)),
    ], now=now)

    subjects = sorted(e["subject"] for e in kept)
    assert subjects == ["A", "B", "C", "D"]
    # New capture's data wins on the A/A collision.
    a = next(e for e in kept if e["subject"] == "A")
    assert a["location"] == "Room 5"
    assert sorted(e["subject"] for e in svc.get_events()) == ["A", "B", "C", "D"]


def test_replace_all_recovers_store_on_empty_capture(tmp_path: Path):
    """The literal field failure: a capture that finds NOTHING must
    not erase a store that still has good, still-future data."""
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 13, 12, 0)
    svc.replace_all([
        ext("Keep me", now + timedelta(hours=1)),
        ext("Keep me too", now + timedelta(days=1)),
    ], now=now)

    kept = svc.replace_all([], now=now)
    assert len(kept) == 2
    assert len(svc.get_events()) == 2


def test_replace_all_replaces_normally_when_capture_is_equal_or_larger(tmp_path: Path):
    """The common case (including every existing test above, which
    seeds an empty store first) must keep working exactly as before —
    a capture that matches or exceeds the prior count replaces
    outright, which is how a cancelled meeting actually disappears."""
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 13, 12, 0)
    svc.replace_all([ext("Only one", now + timedelta(hours=1))], now=now)

    kept = svc.replace_all([
        ext("Fresh one", now + timedelta(hours=1)),
        ext("Fresh two", now + timedelta(hours=2)),
    ], now=now)
    # "Only one" is gone — it wasn't re-reported and the new capture
    # was not smaller, so this is a legitimate replace, not a merge.
    assert sorted(e["subject"] for e in kept) == ["Fresh one", "Fresh two"]


def test_replace_all_shrink_guard_only_compares_within_the_capture_window(tmp_path: Path):
    """An old event that has legitimately aged out of the retention
    window by the time of the new capture must NOT be resurrected by
    the shrink guard — the comparison is windowed to the new capture's
    own lo/hi, not the old capture's."""
    svc = ExtensionCalendarService(tmp_path)
    t0 = datetime(2026, 8, 1, 9, 0)
    svc.replace_all([ext("Old one", t0 + timedelta(hours=1))], now=t0)

    # Ten days later: "Old one" is long past RETAIN_PAST (1 day), so a
    # smaller-looking new capture must still just replace normally.
    t1 = t0 + timedelta(days=10)
    kept = svc.replace_all([], now=t1)
    assert kept == []


# ── replace_all's import_meta (which parse path produced the store) ──

def test_replace_all_persists_import_meta_and_capture_status_exposes_it(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 14, 9, 0, 0)
    svc.replace_all(
        [ext("Standup", now + timedelta(hours=1))], now=now,
        import_meta={"path": "structured", "raw": 1, "kept": 1,
                    "dropped": 0, "fallback_reason": None})

    status = svc.capture_status(now=now)
    assert status["last_import_path"] == "structured"
    assert status["last_import_raw"] == 1
    assert status["last_import_kept"] == 1
    assert status["last_import_dropped"] == 0
    assert status["last_import_fallback_reason"] is None
    assert status["last_import_at"] == now.isoformat()


def test_replace_all_without_import_meta_preserves_the_prior_one():
    """The calendar-refresh alarm's plain event write (no import_meta
    passed) must not blank out import bookkeeping a different POST
    already recorded — same preserve contract as last_seen_version."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        svc = ExtensionCalendarService(Path(d))
        t0 = datetime(2026, 8, 14, 9, 0, 0)
        svc.replace_all(
            [ext("Standup", t0 + timedelta(hours=1))], now=t0,
            import_meta={"path": "text-fallback", "raw": 5, "kept": 1,
                        "dropped": 4, "fallback_reason": "absent"})

        t1 = t0 + timedelta(minutes=30)
        svc.replace_all([ext("Standup", t1 + timedelta(hours=1))], now=t1)

        status = svc.capture_status(now=t1)
        assert status["last_import_path"] == "text-fallback"
        assert status["last_import_raw"] == 5
        assert status["last_import_fallback_reason"] == "absent"


# ── capture_status (Record tab empty-state honesty) ─────────────────

def test_capture_status_reports_never_captured(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    status = svc.capture_status()
    assert status == {
        "updated_at": None, "event_count": 0, "future_event_count": 0,
        "last_seen_version": None, "last_seen_version_at": None,
        "last_import_path": None, "last_import_raw": None,
        "last_import_kept": None, "last_import_dropped": None,
        "last_import_fallback_reason": None, "last_import_at": None,
    }


def test_capture_status_reports_counts_and_last_capture_time(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    now = datetime(2026, 8, 13, 12, 0)
    svc.replace_all([
        ext("Past today", now - timedelta(hours=2)),
        ext("Later today", now + timedelta(hours=2)),
        ext("Tomorrow", now + timedelta(days=1)),
    ], now=now)

    status = svc.capture_status(now=now)
    assert status["updated_at"] == now.isoformat()
    assert status["event_count"] == 3
    assert status["future_event_count"] == 2


def test_capture_status_tolerates_corrupt_store(tmp_path: Path):
    svc = ExtensionCalendarService(tmp_path)
    (tmp_path / "extension_calendar.json").write_text("{not json", encoding="utf-8")
    status = svc.capture_status()
    assert status["event_count"] == 0
