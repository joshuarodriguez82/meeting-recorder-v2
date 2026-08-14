"""
`calendar_source` — the Outlook sign-in-prompt kill switch.

Field report 2026-08-14: a user on a locked-down tenant (ttecdigital.com)
got a Microsoft sign-in prompt every time they opened the Record tab,
because /calendar/upcoming's Outlook COM fetch ran unconditionally —
`Dispatch("Outlook.Application")` is enough to trigger a conditional-
access challenge even when Outlook isn't already running. They'd
already switched to the Chrome extension (scrapes Outlook Web from
their real, already-authenticated browser) but had no way to tell the
backend to stop touching Outlook, so the prompt kept firing regardless.
The same bug also meant a slow/blocked Outlook stalled /calendar/upcoming
for up to 45s and withheld the already-on-disk extension events for the
entire wait.

This file covers three layers:

  1. Settings plumbing (config/settings.py): the field exists, defaults
     to "auto", round-trips through all four values via
     Settings.save_to_env -> Settings.from_env, self-heals a garbage
     value back to "auto", and survives the generic round-trip test in
     test_diarization_device.py (that file was updated alongside this
     one to give calendar_source a normalization-safe override, same as
     diarization_device already had).

  2. services/calendar_service.py: the single choke point every caller
     (server.py's calendar endpoints, calendar_monitor.py,
     auto_record_service.py) goes through. "extension"/"off" must never
     call into the platform backend (Outlook COM / EventKit) — this is
     what actually stops the sign-in prompt, independent of whatever
     server.py itself does.

  3. server.py's four calendar endpoints: each must skip the Outlook
     fetch function entirely under "extension"/"off" (verified with a
     spy that fails the test if invoked — not just "returns []", since
     calendar_service already guarantees that; the point here is that
     THIS layer doesn't even make the call), and /calendar/upcoming
     must fetch local + extension CONCURRENTLY so a slow local fetch
     never delays extension events that are already available.

No optional deps beyond what the rest of the suite already stubs
(dotenv, fastapi) — see _app_import.py's docstring for why server.py
can be imported headlessly in CI.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("dotenv", MagicMock())

from config import settings as settings_mod  # noqa: E402
from _app_import import import_app  # noqa: E402
from services import calendar_service  # noqa: E402


# ── 1. Settings plumbing ─────────────────────────────────────────────

def _real_dotenv_values(path) -> dict:
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v
    except OSError:
        pass
    return out


def _patch_dotenv(monkeypatch) -> None:
    monkeypatch.setattr(settings_mod, "dotenv_values", _real_dotenv_values)
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **kw: True)


def _isolate_env_path(monkeypatch, tmp_path: Path) -> Path:
    env_path = tmp_path / "config.env"
    fallback_path = tmp_path / "dev-fallback.env"
    monkeypatch.setattr(settings_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(settings_mod, "_resolve_env_path", lambda: env_path)

    real_write = settings_mod.Settings._write_env_file

    def _redirect_write(target: Path, content: str) -> bool:
        if target != env_path:
            target = fallback_path
        return real_write(target, content)

    monkeypatch.setattr(
        settings_mod.Settings, "_write_env_file", staticmethod(_redirect_write))
    return env_path


def test_calendar_source_is_a_dataclass_field():
    assert "calendar_source" in settings_mod.Settings.__dataclass_fields__


def test_calendar_source_defaults_to_auto_on_fresh_install(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    loaded = settings_mod.Settings.from_env()
    assert loaded.calendar_source == "auto"


def test_calendar_source_defaults_to_auto_when_save_to_env_omits_it(tmp_path, monkeypatch):
    """The exact bug class AGENTS.md calls out: a save_to_env call site
    that forgot to pass a new field silently blanks it. Calling
    save_to_env WITHOUT calendar_source must not corrupt config.env or
    crash from_env — it must resolve the documented default."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.calendar_source == "auto"


@pytest.mark.parametrize("value", ["auto", "outlook", "extension", "off"])
def test_calendar_source_round_trips_through_config_env(value, tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        calendar_source=value,
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.calendar_source == value


def test_calendar_source_self_heals_a_garbage_value(tmp_path, monkeypatch):
    """Hand-edited config.env / an old build's now-removed value must
    not brick settings load — falls back to auto, same as
    diarization_device's precedent."""
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)
    env_path.write_text(
        "RECORDINGS_DIR=" + str(tmp_path / "recordings") + "\n"
        "CALENDAR_SOURCE=totally-not-a-real-value\n",
        encoding="utf-8",
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.calendar_source == "auto"


def test_calendar_source_survives_alongside_other_settings(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    settings_mod.Settings.save_to_env(
        anthropic_api_key="sk-test", hf_token="hf-test", whisper_model="small",
        max_speakers=6, recordings_dir=str(tmp_path / "recordings"),
        live_copilot_enabled=True,
        session_archive_dir=str(tmp_path / "archive"),
        calendar_source="extension",
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.calendar_source == "extension"
    assert loaded.session_archive_dir == str(tmp_path / "archive")
    assert loaded.live_copilot_enabled is True


# ── 2. services/calendar_service.py — the single choke point ────────

class _SpyBackend:
    """Stands in for `_calendar_outlook` / `_calendar_eventkit`. Every
    method records a call and fails the test immediately if invoked
    while calendar_source should have prevented it — a monkeypatched
    calendar_service._current_calendar_source controls that."""

    def __init__(self):
        self.calls: list[str] = []

    def get_todays_meetings(self):
        self.calls.append("get_todays_meetings")
        return [{"subject": "Local Standup", "start": datetime(2026, 8, 14, 9, 0),
                  "end": datetime(2026, 8, 14, 9, 30)}]

    def get_meetings_for_date(self, target_date):
        self.calls.append("get_meetings_for_date")
        return []

    def get_upcoming_meetings(self, hours_ahead=168):
        self.calls.append("get_upcoming_meetings")
        return [{"subject": "Local Sync", "start": datetime(2026, 8, 14, 10, 0),
                  "end": datetime(2026, 8, 14, 10, 30)}]

    def get_meeting_detail(self, subject, start_iso):
        self.calls.append("get_meeting_detail")
        return {"attendees": ["a@x.com"], "body": "agenda", "join_url": None}

    def is_outlook_available(self):
        self.calls.append("is_outlook_available")
        return True

    def invalidate_calendar_cache(self):
        self.calls.append("invalidate_calendar_cache")


@pytest.fixture
def spy_backend(monkeypatch):
    backend = _SpyBackend()
    monkeypatch.setattr(calendar_service, "_BACKEND", backend)
    return backend


@pytest.mark.parametrize("source", ["extension", "off"])
def test_calendar_service_never_touches_backend_when_gated(
        source, spy_backend, monkeypatch):
    monkeypatch.setattr(
        calendar_service, "_current_calendar_source", lambda: source)

    assert calendar_service.get_todays_meetings() == []
    assert calendar_service.get_meetings_for_date(datetime(2026, 8, 14).date()) == []
    assert calendar_service.get_upcoming_meetings(168) == []
    assert calendar_service.get_meeting_detail("x", "2026-08-14T09:00:00") == {
        "attendees": [], "body": "", "join_url": None}
    assert calendar_service.is_outlook_available() is False
    assert spy_backend.calls == [], (
        f"calendar_source={source!r} must never call into the platform "
        f"backend, but got: {spy_backend.calls}")


@pytest.mark.parametrize("source", ["auto", "outlook"])
def test_calendar_service_uses_backend_when_allowed(source, spy_backend, monkeypatch):
    monkeypatch.setattr(
        calendar_service, "_current_calendar_source", lambda: source)

    assert len(calendar_service.get_todays_meetings()) == 1
    assert len(calendar_service.get_upcoming_meetings(168)) == 1
    assert calendar_service.is_outlook_available() is True
    assert "get_todays_meetings" in spy_backend.calls
    assert "get_upcoming_meetings" in spy_backend.calls
    assert "is_outlook_available" in spy_backend.calls


def test_invalidate_calendar_cache_always_safe(spy_backend, monkeypatch):
    """Clearing an in-memory cache dict never touches Outlook COM /
    EventKit itself, so it's allowed regardless of calendar_source."""
    monkeypatch.setattr(
        calendar_service, "_current_calendar_source", lambda: "off")
    calendar_service.invalidate_calendar_cache()
    assert spy_backend.calls == ["invalidate_calendar_cache"]


# ── 3. server.py's four calendar endpoints ───────────────────────────

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402


def _wire(monkeypatch, *, calendar_source: str, extension_svc=None):
    """Point server.svc at a lightweight fake settings object and make
    load_settings() a no-op (mirrors test_model_load_singleflight.py's
    _make_services) so calling the real Settings.from_env() machinery
    isn't needed."""
    monkeypatch.setattr(
        server.svc, "settings",
        SimpleNamespace(calendar_source=calendar_source))
    monkeypatch.setattr(server.svc, "load_settings", lambda: server.svc.settings)
    monkeypatch.setattr(server.svc, "extension_calendar_svc", extension_svc)


def _failing_spy(name: str):
    def _fn(*a, **kw):
        raise AssertionError(
            f"{name} must not be called when calendar_source disallows "
            f"the local calendar backend")
    return _fn


@pytest.mark.parametrize("source", ["extension", "off"])
def test_upcoming_never_calls_outlook_fetch_when_gated(source, monkeypatch):
    _wire(monkeypatch, calendar_source=source, extension_svc=SimpleNamespace(
        get_events=lambda hours: []))
    monkeypatch.setattr(server, "get_upcoming_meetings", _failing_spy("get_upcoming_meetings"))
    result = asyncio.run(server.get_calendar_upcoming(hours=168, refresh=False))
    assert result == []


@pytest.mark.parametrize("source", ["extension", "off"])
def test_today_never_calls_outlook_fetch_when_gated(source, monkeypatch):
    _wire(monkeypatch, calendar_source=source)
    monkeypatch.setattr(server, "get_todays_meetings", _failing_spy("get_todays_meetings"))
    result = asyncio.run(server.get_calendar_today())
    assert result == []


@pytest.mark.parametrize("source", ["extension", "off"])
def test_meeting_detail_never_calls_outlook_fetch_when_gated(source, monkeypatch):
    _wire(monkeypatch, calendar_source=source)
    monkeypatch.setattr(
        calendar_service, "get_meeting_detail", _failing_spy("get_meeting_detail"))
    result = asyncio.run(
        server.calendar_meeting_detail("Some Meeting", "2026-08-14T09:00:00"))
    assert result == {"attendees": [], "body": "", "join_url": None}


def test_available_never_calls_outlook_fetch_when_off(monkeypatch):
    _wire(monkeypatch, calendar_source="off")
    monkeypatch.setattr(server, "is_outlook_available", _failing_spy("is_outlook_available"))
    result = asyncio.run(server.calendar_available())
    assert result == {"available": False, "source": "off"}


def test_available_reports_extension_data_not_outlook(monkeypatch):
    """The point of "extension" mode: /calendar/available answers from
    whether the extension has synced anything, never from Outlook. Also
    carries last_capture_at / event_count so the Record tab's empty
    state can be honest about a stale/empty extension store rather than
    rendering identically to a genuinely free calendar (field report
    2026-08-13)."""
    _wire(monkeypatch, calendar_source="extension", extension_svc=SimpleNamespace(
        capture_status=lambda: {
            "updated_at": "2026-08-13T13:00:00", "event_count": 3,
            "future_event_count": 2,
        }))
    monkeypatch.setattr(server, "is_outlook_available", _failing_spy("is_outlook_available"))
    result = asyncio.run(server.calendar_available())
    assert result == {
        "available": True, "source": "extension",
        "last_capture_at": "2026-08-13T13:00:00",
        "event_count": 3, "future_event_count": 2,
        "last_import_path": None, "last_import_raw": None,
        "last_import_kept": None, "last_import_dropped": None,
        "last_import_fallback_reason": None, "last_import_at": None,
    }

    _wire(monkeypatch, calendar_source="extension", extension_svc=SimpleNamespace(
        capture_status=lambda: {
            "updated_at": None, "event_count": 0, "future_event_count": 0,
        }))
    monkeypatch.setattr(server, "is_outlook_available", _failing_spy("is_outlook_available"))
    result = asyncio.run(server.calendar_available())
    assert result == {
        "available": False, "source": "extension",
        "last_capture_at": None, "event_count": 0, "future_event_count": 0,
        "last_import_path": None, "last_import_raw": None,
        "last_import_kept": None, "last_import_dropped": None,
        "last_import_fallback_reason": None, "last_import_at": None,
    }


def test_available_uses_outlook_when_auto(monkeypatch):
    _wire(monkeypatch, calendar_source="auto")
    monkeypatch.setattr(server, "is_outlook_available", lambda: True)
    result = asyncio.run(server.calendar_available())
    assert result == {"available": True, "source": "auto"}


def test_off_consults_neither_source_and_returns_empty_without_error(monkeypatch):
    calls = {"local": 0, "ext": 0}

    def local_spy(hours):
        calls["local"] += 1
        return [{"subject": "Should not run", "start": datetime.now(),
                  "end": datetime.now() + timedelta(minutes=30)}]

    def ext_spy(hours):
        calls["ext"] += 1
        return [{"subject": "Should not run either", "start": datetime.now(),
                  "end": datetime.now() + timedelta(minutes=30), "source": "extension"}]

    _wire(monkeypatch, calendar_source="off",
          extension_svc=SimpleNamespace(get_events=ext_spy))
    monkeypatch.setattr(server, "get_upcoming_meetings", local_spy)
    result = asyncio.run(server.get_calendar_upcoming(hours=168, refresh=False))
    assert result == []
    assert calls == {"local": 0, "ext": 0}


def test_auto_consults_both_and_merges(monkeypatch):
    now = datetime(2026, 8, 14, 9, 0)

    def local_fetch(hours):
        return [{"subject": "Weekly Sync", "start": now, "end": now + timedelta(minutes=30),
                  "location": "Teams", "organizer": "boss@x.com", "attendees": [], "duration": 30}]

    def ext_fetch(hours):
        return [{"subject": "Extension-Only Meeting",
                  "start": now + timedelta(hours=2),
                  "end": now + timedelta(hours=2, minutes=30),
                  "location": "", "organizer": "", "attendees": [], "duration": 30,
                  "join_url": "", "source": "extension"}]

    _wire(monkeypatch, calendar_source="auto",
          extension_svc=SimpleNamespace(get_events=ext_fetch))
    monkeypatch.setattr(server, "get_upcoming_meetings", local_fetch)
    result = asyncio.run(server.get_calendar_upcoming(hours=168, refresh=False))

    subjects = {m["subject"] for m in result}
    assert subjects == {"Weekly Sync", "Extension-Only Meeting"}
    by_subject = {m["subject"]: m for m in result}
    assert by_subject["Weekly Sync"]["source"] == "outlook"
    assert by_subject["Extension-Only Meeting"]["source"] == "extension"


def test_merge_dedupes_by_subject_and_five_minute_window_local_preferred(monkeypatch):
    """Merge semantics unchanged by the concurrency/gating rework: same
    subject within +-5min collapses to the local copy."""
    now = datetime(2026, 8, 14, 9, 0)

    def local_fetch(hours):
        return [{"subject": "Weekly Sync", "start": now, "end": now + timedelta(minutes=30),
                  "location": "Teams", "organizer": "boss@x.com",
                  "attendees": ["real@x.com"], "duration": 30}]

    def ext_fetch(hours):
        # Same meeting, 3 minutes off and with no organizer/attendees —
        # inside the 5-minute dedup tolerance, so it must be dropped in
        # favor of the richer local copy.
        return [{"subject": "Weekly Sync", "start": now + timedelta(minutes=3),
                  "end": now + timedelta(minutes=33),
                  "location": "", "organizer": "", "attendees": [], "duration": 30,
                  "join_url": "", "source": "extension"}]

    _wire(monkeypatch, calendar_source="auto",
          extension_svc=SimpleNamespace(get_events=ext_fetch))
    monkeypatch.setattr(server, "get_upcoming_meetings", local_fetch)
    result = asyncio.run(server.get_calendar_upcoming(hours=168, refresh=False))

    assert len(result) == 1
    assert result[0]["source"] == "outlook"
    assert result[0]["organizer"] == "boss@x.com"


def test_extension_events_returned_promptly_despite_slow_outlook(monkeypatch):
    """The actual field-reported bug: a slow/hanging Outlook must never
    delay the extension events that are already on disk. Proven by
    ORDER, not wall-clock timing (avoids CI flakiness): the extension
    fetch must complete WHILE the local fetch is still sleeping, which
    is only possible if the two run concurrently rather than the
    extension fetch waiting for the local one to finish first."""
    order: list[str] = []

    def slow_local_fetch(hours):
        order.append("local_start")
        time.sleep(0.3)
        order.append("local_end")
        return [{"subject": "Slow Outlook Meeting", "start": datetime.now(),
                  "end": datetime.now() + timedelta(minutes=30)}]

    def fast_ext_fetch(hours):
        order.append("ext_ran")
        return [{"subject": "Fast Extension Meeting", "start": datetime.now(),
                  "end": datetime.now() + timedelta(minutes=30), "source": "extension"}]

    _wire(monkeypatch, calendar_source="auto",
          extension_svc=SimpleNamespace(get_events=fast_ext_fetch))
    monkeypatch.setattr(server, "get_upcoming_meetings", slow_local_fetch)

    t0 = time.monotonic()
    result = asyncio.run(server.get_calendar_upcoming(hours=168, refresh=False))
    elapsed = time.monotonic() - t0

    # Both sets of events made it back despite the slow local fetch.
    subjects = {m["subject"] for m in result}
    assert subjects == {"Slow Outlook Meeting", "Fast Extension Meeting"}

    # The extension fetch ran and finished BEFORE the local (Outlook)
    # fetch finished sleeping — proof the two ran concurrently, not
    # sequentially. A sequential implementation would show
    # ["local_start", "local_end", "ext_ran"].
    assert order.index("ext_ran") < order.index("local_end"), (
        f"extension fetch did not overlap the slow local fetch: {order}")

    # Sanity bound: concurrent execution takes ~= the slower side
    # (0.3s), not the sum of both. Generous margin for CI jitter.
    assert elapsed < 0.9, (
        f"took {elapsed:.2f}s — looks sequential, not concurrent")
