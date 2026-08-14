"""
`/briefing/extension-import`'s v1.2 calendar behavior.

Field report 2026-08-13: extension-only `calendar_source` showed ZERO
upcoming meetings while Outlook Web had 9 in the next 7 days. Two
defects fixed on the backend side of that:

  1. The endpoint now accepts `calendar_events` — structured events the
     extension parsed CLIENT-SIDE out of Outlook Web's own aria-label
     accessibility strings (see chrome-extension/background.js and
     services/extension_calendar_service.events_from_structured, which
     carries the real parser-correctness tests). No LLM on this path.
  2. A request carrying ONLY calendar data (the periodic calendar-
     refresh alarm, distinct from a full "Capture & Send") takes a fast
     path that updates just the calendar store — no LLM call, and it
     must never touch the day's saved briefing (greeting/top_priority/
     needs_response/fyi), since that alarm fires every 30 minutes.

These tests exercise the endpoint FUNCTION directly (not over HTTP) —
same pattern test_calendar_source_settings.py uses — with server.svc's
collaborators replaced by lightweight spies. No LLM SDK involved.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("dotenv", MagicMock())

from _app_import import import_app  # noqa: E402

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402


class _SaveParsedSpy:
    def __init__(self, stored=None):
        self.calls = []
        self._stored = stored or {"date": "2026-08-13", "agenda": []}

    def __call__(self, parsed, raw_text, date_iso):
        self.calls.append((parsed, raw_text, date_iso))
        return self._stored


class _ReplaceAllSpy:
    def __init__(self, kept=None):
        self.calls = []
        self.import_meta_calls = []
        self._kept = kept if kept is not None else []

    def __call__(self, events, now=None, import_meta=None):
        self.calls.append(list(events))
        self.import_meta_calls.append(import_meta)
        return self._kept


class _RecordVersionSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, version):
        self.calls.append(version)


class _FakeSummarizer:
    def __init__(self, parsed=None, raise_error=False):
        self.calls = []
        self._parsed = parsed or {
            "greeting": "", "top_priority": None, "needs_response": [],
            "agenda": [], "schedule_notes": [], "fyi": [],
        }
        self._raise = raise_error

    async def parse_daily_briefing(self, text, today_iso=""):
        self.calls.append((text, today_iso))
        if self._raise:
            raise RuntimeError("boom")
        return self._parsed


def _wire(monkeypatch, *, summarizer=None, save_parsed=None, replace_all=None,
         record_extension_version=None):
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(
        server.svc, "daily_briefing_svc",
        SimpleNamespace(save_parsed=save_parsed or _SaveParsedSpy()))
    monkeypatch.setattr(
        server.svc, "extension_calendar_svc",
        SimpleNamespace(
            replace_all=replace_all or _ReplaceAllSpy(),
            record_extension_version=(
                record_extension_version or _RecordVersionSpy()),
        ))
    monkeypatch.setattr(server.svc, "summarizer", summarizer)
    monkeypatch.setattr(server.svc, "live_summarizer", None)


REALISTIC_STRUCTURED_EVENTS = [
    {"subject": "AWS Daily Pulse Call",
     "start": "2026-08-13T10:00:00", "end": "2026-08-13T10:15:00"},
    {"subject": "PRIORITY: AWS Sales| Active Project Status Reviews and Escalations",
     "start": "2026-08-13T10:00:00", "end": "2026-08-13T10:30:00"},
    {"subject": "FW: AWS Connect - Italy / ECC next steps: weekly team connect",
     "start": "2026-08-13T11:30:00", "end": "2026-08-13T12:00:00"},
    {"subject": "AWS/[scrubbed] - IVA PoC Sync-up",
     "start": "2026-08-13T13:00:00", "end": "2026-08-13T13:30:00"},
    {"subject": "AI Transformation Stand Up",
     "start": "2026-08-13T07:30:00", "end": "2026-08-13T08:30:00"},
]


def test_calendar_only_structured_path_skips_llm_and_briefing_save(monkeypatch):
    """The periodic calendar-refresh alarm: no narrative text at all,
    just calendar_events. Must not call the LLM and must not touch the
    saved briefing — only the calendar store."""
    summ = _FakeSummarizer()
    save_parsed = _SaveParsedSpy()
    replace_all = _ReplaceAllSpy(kept=REALISTIC_STRUCTURED_EVENTS)
    _wire(monkeypatch, summarizer=summ, save_parsed=save_parsed, replace_all=replace_all)

    req = server.ExtensionImportRequest(calendar_events=REALISTIC_STRUCTURED_EVENTS)
    result = asyncio.run(server.import_briefing_from_extension(req))

    assert result["ok"] is True
    assert result["path"] == "structured"
    assert result["parsed_events"] == 5  # the field regression: 1 -> 5
    assert summ.calls == [], "calendar-only structured path must not call the LLM"
    assert save_parsed.calls == [], "calendar-only path must not touch the saved briefing"
    assert len(replace_all.calls) == 1
    assert len(replace_all.calls[0]) == 5
    subjects = {e["subject"] for e in replace_all.calls[0]}
    assert "FW: AWS Connect - Italy / ECC next steps: weekly team connect" in subjects


def test_calendar_only_text_fallback_uses_llm_but_still_skips_briefing_save(monkeypatch):
    """Structured extraction found nothing client-side; calendar_text is
    the last resort. This DOES call the LLM (only path that must), but
    still must not persist to the daily briefing store."""
    summ = _FakeSummarizer(parsed={
        "greeting": "", "top_priority": None, "needs_response": [],
        "agenda": [{"title": "Ad-hoc Sync", "time": "2:00 PM",
                    "duration": "30 min", "status": "scheduled"}],
        "schedule_notes": [], "fyi": [], "date": "2026-08-13",
    })
    save_parsed = _SaveParsedSpy()
    replace_all = _ReplaceAllSpy(kept=[{"subject": "Ad-hoc Sync"}])
    _wire(monkeypatch, summarizer=summ, save_parsed=save_parsed, replace_all=replace_all)

    req = server.ExtensionImportRequest(
        calendar_text="Ad-hoc Sync 2:00 PM - 2:30 PM", date="2026-08-13")
    result = asyncio.run(server.import_briefing_from_extension(req))

    assert result["ok"] is True
    assert result["path"] == "text-fallback"
    assert len(summ.calls) == 1
    assert save_parsed.calls == [], "calendar-only path must not touch the saved briefing"
    assert len(replace_all.calls) == 1


# ── observability: path= logging + import_meta wiring (field report
#    chain culminating 2026-08-14 — two calendar-parse paths produced
#    identically-shaped output, so neither the log nor the stored JSON
#    ever said which one ran, which caused several wrong diagnoses) ──

def test_calendar_only_structured_path_logs_and_records_import_meta(monkeypatch, caplog):
    replace_all = _ReplaceAllSpy(kept=REALISTIC_STRUCTURED_EVENTS)
    _wire(monkeypatch, replace_all=replace_all)

    req = server.ExtensionImportRequest(calendar_events=REALISTIC_STRUCTURED_EVENTS)
    with caplog.at_level(logging.INFO, logger="server"):
        result = asyncio.run(server.import_briefing_from_extension(req))

    assert result["path"] == "structured"
    assert replace_all.import_meta_calls == [{
        "path": "structured", "raw": 5, "kept": 5, "dropped": 0,
        "fallback_reason": None,
    }]
    log_lines = [r.message for r in caplog.records]
    path_lines = [m for m in log_lines if m.startswith("Extension calendar: path=")]
    assert len(path_lines) == 1
    assert path_lines[0] == "Extension calendar: path=structured raw=5 kept=5 dropped=0"


def test_calendar_only_text_fallback_because_absent_logs_the_reason(monkeypatch, caplog):
    """calendar_events was never sent at all (an old extension, or a
    capture that threw before building a payload) -- distinct from an
    extension that DID run its scan and found nothing."""
    summ = _FakeSummarizer(parsed={
        "greeting": "", "top_priority": None, "needs_response": [],
        "agenda": [{"title": "Ad-hoc Sync", "time": "2:00 PM",
                    "duration": "30 min", "status": "scheduled"}],
        "schedule_notes": [], "fyi": [], "date": "2026-08-13",
    })
    replace_all = _ReplaceAllSpy(kept=[{"subject": "Ad-hoc Sync"}])
    _wire(monkeypatch, summarizer=summ, replace_all=replace_all)

    req = server.ExtensionImportRequest(
        calendar_text="Ad-hoc Sync 2:00 PM - 2:30 PM", date="2026-08-13")
    assert req.calendar_events is None  # never sent -- "absent", not "empty"
    with caplog.at_level(logging.INFO, logger="server"):
        result = asyncio.run(server.import_briefing_from_extension(req))

    assert result["path"] == "text-fallback"
    assert len(replace_all.import_meta_calls) == 1
    meta = replace_all.import_meta_calls[0]
    assert meta["fallback_reason"] == "absent"
    assert meta["raw"] == 1 and meta["kept"] == 1
    path_line = next(
        r.message for r in caplog.records
        if r.message.startswith("Extension calendar: path="))
    assert path_line == (
        "Extension calendar: path=briefing-fallback "
        "(extension sent no structured events) raw=1 kept=1 dropped=0")


def test_calendar_only_text_fallback_because_empty_logs_the_reason(monkeypatch, caplog):
    """calendar_events WAS sent, as an empty list: a current extension
    whose structured DOM scan ran and genuinely found zero candidates
    this capture -- must not be logged the same way as "absent"."""
    summ = _FakeSummarizer(parsed={
        "greeting": "", "top_priority": None, "needs_response": [],
        "agenda": [
            {"title": "Ad-hoc Sync", "time": "2:00 PM",
             "duration": "30 min", "status": "scheduled"},
            {"title": "Cancelled one", "time": "3:00 PM", "status": "cancelled"},
        ],
        "schedule_notes": [], "fyi": [], "date": "2026-08-13",
    })
    replace_all = _ReplaceAllSpy(kept=[{"subject": "Ad-hoc Sync"}])
    _wire(monkeypatch, summarizer=summ, replace_all=replace_all)

    req = server.ExtensionImportRequest(
        calendar_events=[],  # sent, but explicitly empty
        calendar_text="Ad-hoc Sync 2:00 PM - 2:30 PM", date="2026-08-13")
    with caplog.at_level(logging.INFO, logger="server"):
        result = asyncio.run(server.import_briefing_from_extension(req))

    assert result["path"] == "text-fallback"
    meta = replace_all.import_meta_calls[0]
    assert meta["fallback_reason"] == "empty"
    assert meta["raw"] == 2 and meta["kept"] == 1 and meta["dropped"] == 1
    path_line = next(
        r.message for r in caplog.records
        if r.message.startswith("Extension calendar: path="))
    assert path_line == (
        "Extension calendar: path=briefing-fallback "
        "(extension's structured scan found zero events) "
        "raw=2 kept=1 dropped=1 (cancelled: 1)")


def test_full_capture_narrative_summary_line_carries_path(monkeypatch, caplog):
    """The pre-existing 'Chrome-extension import:' summary line must
    carry path= too, so a single grep answers "which path ran" for
    both the calendar-only fast path and a full narrative capture."""
    summ = _FakeSummarizer(parsed={
        "greeting": "hi", "top_priority": None, "needs_response": [],
        "agenda": [], "schedule_notes": [], "fyi": [],
    })
    save_parsed = _SaveParsedSpy(
        stored={"date": "2026-08-13", "agenda": []})
    replace_all = _ReplaceAllSpy(kept=REALISTIC_STRUCTURED_EVENTS)
    _wire(monkeypatch, summarizer=summ, save_parsed=save_parsed, replace_all=replace_all)

    req = server.ExtensionImportRequest(
        owa_text="Today's calendar text", calendar_events=REALISTIC_STRUCTURED_EVENTS)
    with caplog.at_level(logging.INFO, logger="server"):
        asyncio.run(server.import_briefing_from_extension(req))

    summary_line = next(
        r.message for r in caplog.records
        if r.message.startswith("Chrome-extension import:"))
    assert "path=structured" in summary_line


def test_no_content_at_all_is_a_400(monkeypatch):
    _wire(monkeypatch)
    req = server.ExtensionImportRequest()
    with pytest.raises(Exception) as exc_info:
        asyncio.run(server.import_briefing_from_extension(req))
    assert getattr(exc_info.value, "status_code", None) == 400


def test_full_capture_prefers_structured_events_over_briefing_derived(monkeypatch):
    """A manual "Capture & Send" that includes BOTH narrative text and
    calendar_events must use the structured events for the calendar
    store — not derive them from the LLM-parsed briefing agenda, even
    though the briefing itself is still saved normally."""
    summ = _FakeSummarizer(parsed={
        "greeting": "hi", "top_priority": None, "needs_response": [],
        # Deliberately a DIFFERENT, single agenda item — if the endpoint
        # fell back to events_from_briefing this test would see 1 event,
        # not 5.
        "agenda": [{"title": "Should not be used", "time": "9:00 AM",
                    "status": "scheduled"}],
        "schedule_notes": [], "fyi": [],
    })
    save_parsed = _SaveParsedSpy(stored={"date": "2026-08-13", "agenda": [
        {"title": "Should not be used", "time": "9:00 AM", "status": "scheduled"},
    ]})
    replace_all = _ReplaceAllSpy(kept=REALISTIC_STRUCTURED_EVENTS)
    _wire(monkeypatch, summarizer=summ, save_parsed=save_parsed, replace_all=replace_all)

    req = server.ExtensionImportRequest(
        owa_text="Today's calendar text", calendar_events=REALISTIC_STRUCTURED_EVENTS)
    result = asyncio.run(server.import_briefing_from_extension(req))

    assert len(summ.calls) == 1, "narrative text present -> briefing LLM parse still runs"
    assert len(save_parsed.calls) == 1, "briefing is still saved normally"
    assert len(replace_all.calls) == 1
    assert len(replace_all.calls[0]) == 5, (
        "calendar store must be populated from calendar_events (5), "
        "not derived from the 1-item briefing agenda")


def test_extension_version_is_recorded_from_a_post(monkeypatch):
    record_spy = _RecordVersionSpy()
    replace_all = _ReplaceAllSpy(kept=REALISTIC_STRUCTURED_EVENTS[:1])
    _wire(monkeypatch, replace_all=replace_all, record_extension_version=record_spy)

    req = server.ExtensionImportRequest(
        calendar_events=REALISTIC_STRUCTURED_EVENTS[:1], extension_version="1.2.0")
    asyncio.run(server.import_briefing_from_extension(req))

    assert record_spy.calls == ["1.2.0"]


def test_absent_extension_version_is_recorded_as_none_not_assumed_current(monkeypatch):
    record_spy = _RecordVersionSpy()
    replace_all = _ReplaceAllSpy(kept=REALISTIC_STRUCTURED_EVENTS[:1])
    _wire(monkeypatch, replace_all=replace_all, record_extension_version=record_spy)

    # An un-upgraded (pre-1.2.0) extension never sends extension_version.
    req = server.ExtensionImportRequest(calendar_events=REALISTIC_STRUCTURED_EVENTS[:1])
    asyncio.run(server.import_briefing_from_extension(req))

    assert record_spy.calls == [None]


def test_extension_version_is_recorded_even_on_full_narrative_capture(monkeypatch):
    """Version bookkeeping runs unconditionally, not only on the
    calendar-only fast path -- a manual Capture & Send must report its
    version too."""
    summ = _FakeSummarizer()
    record_spy = _RecordVersionSpy()
    _wire(monkeypatch, summarizer=summ, record_extension_version=record_spy)

    req = server.ExtensionImportRequest(
        owa_text="Today's calendar text", extension_version="1.2.0")
    asyncio.run(server.import_briefing_from_extension(req))

    assert record_spy.calls == ["1.2.0"]
