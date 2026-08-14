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
        self._kept = kept if kept is not None else []

    def __call__(self, events):
        self.calls.append(list(events))
        return self._kept


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


def _wire(monkeypatch, *, summarizer=None, save_parsed=None, replace_all=None):
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(
        server.svc, "daily_briefing_svc",
        SimpleNamespace(save_parsed=save_parsed or _SaveParsedSpy()))
    monkeypatch.setattr(
        server.svc, "extension_calendar_svc",
        SimpleNamespace(replace_all=replace_all or _ReplaceAllSpy()))
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
