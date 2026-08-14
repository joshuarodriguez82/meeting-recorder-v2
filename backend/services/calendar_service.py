"""
Calendar reader — platform router.

Picks the right backend based on the OS and re-exports its public API:

    Windows → _calendar_outlook.py  (Outlook COM via pywin32)
    macOS   → _calendar_eventkit.py (EventKit via pyobjc; reads any
                                     calendar synced into Calendar.app —
                                     iCloud, Exchange / Outlook for Mac,
                                     Google, etc.)
    Linux   → no-op stub returning empty results

The two backends present the same surface so the rest of the app —
server.py, calendar_monitor.py — never has to branch on platform.

If the platform-specific backend fails to import (e.g. pyobjc not yet
installed on a Mac venv), we fall back to the stub and log a warning so
the rest of the app still boots; calendar features just appear empty.

Every exported function additionally gates on the `calendar_source`
Settings field ("auto" / "outlook" / "extension" / "off" — see
config/settings.py). When it's "extension" or "off" none of them call
into the platform backend at all, so no Outlook COM / EventKit call
happens — this is what stops the Microsoft sign-in prompt some users
hit from `Dispatch("Outlook.Application")` on a locked-down tenant.
This module is the single choke point for that gate: every caller
(server.py's calendar endpoints, calendar_monitor.py, auto_record_
service.py) imports from here rather than the platform backends
directly, so gating it here is sufficient.
"""

from __future__ import annotations

import datetime
import sys
from typing import List

from utils.logger import get_logger

logger = get_logger(__name__)


def _load_backend():
    if sys.platform == "darwin":
        try:
            from . import _calendar_eventkit as backend  # type: ignore
            logger.info("Calendar backend: macOS EventKit")
            return backend
        except ImportError as e:
            logger.warning(
                f"EventKit calendar backend not available ({e}). "
                f"Calendar features will be disabled. "
                f"Install pyobjc-framework-EventKit in the backend venv "
                f"to enable.")
            return _StubBackend()
    if sys.platform.startswith("win"):
        try:
            from . import _calendar_outlook as backend  # type: ignore
            logger.info("Calendar backend: Windows Outlook COM")
            return backend
        except ImportError as e:
            logger.warning(
                f"Outlook calendar backend not available ({e}). "
                f"Calendar features will be disabled.")
            return _StubBackend()
    logger.info("Calendar backend: stub (no calendar support on this platform)")
    return _StubBackend()


class _StubBackend:
    """No-op backend used when no real calendar provider is available.
    Keeps server.py from crashing on `from services.calendar_service
    import get_todays_meetings` etc."""

    @staticmethod
    def get_todays_meetings() -> List[dict]:
        return []

    @staticmethod
    def get_meetings_for_date(target_date: datetime.date) -> List[dict]:
        return []

    @staticmethod
    def get_upcoming_meetings(hours_ahead: int = 168) -> List[dict]:
        return []

    @staticmethod
    def get_meeting_detail(subject: str, start_iso: str) -> dict:
        return {"attendees": [], "body": "", "join_url": None}

    @staticmethod
    def is_outlook_available() -> bool:
        return False

    @staticmethod
    def invalidate_calendar_cache() -> None:
        return None

    @staticmethod
    def make_session_name(meeting: dict) -> str:
        date_str = meeting["start"].strftime("%Y-%m-%d")
        time_str = meeting["start"].strftime("%H%M")
        subject = meeting.get("subject", "Meeting")
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in subject)
        safe = safe.strip().replace("  ", " ")[:48]
        return f"{date_str} {time_str} {safe}"


_BACKEND = _load_backend()

_EMPTY_DETAIL = {"attendees": [], "body": "", "join_url": None}


def _current_calendar_source() -> str:
    """Live-read the `calendar_source` setting.

    Read fresh from disk on every call, same as every other setting in
    this app (Settings.from_env's own docstring explains why — Tauri
    spawn contexts can deliver a partial/stale process environment).
    This module is the SINGLE choke point every caller — server.py's
    calendar endpoints, calendar_monitor.py's poll loop,
    auto_record_service.py's poll loop — goes through, so gating here
    is enough to guarantee no Outlook COM / EventKit call happens
    anywhere when the user has opted out, without threading a settings
    parameter through every one of those call sites.
    """
    try:
        from config.settings import Settings
        return Settings.from_env().calendar_source or "auto"
    except Exception:
        # A settings read failing must not itself crash calendar
        # access — fall back to the pre-existing "auto" behavior.
        return "auto"


def _local_calendar_allowed() -> bool:
    """False when the user has told us to never touch the local
    calendar backend (Outlook COM on Windows / EventKit on macOS) —
    calendar_source "extension" (Chrome-extension-scraped events only)
    or "off" (no calendar data at all). This is what stops the
    Microsoft sign-in prompt: `is_outlook_available()` and
    `Dispatch("Outlook.Application")` both launch/attach to Outlook,
    which is enough to trigger a locked-down tenant's conditional-
    access challenge — so none of the wrapped functions below may call
    into `_BACKEND` at all while this is False."""
    return _current_calendar_source() not in ("extension", "off")


# Re-export the same names the old single-file module exposed so server.py,
# calendar_monitor.py, and auto_record_service.py keep working unchanged —
# but as live wrapper functions (not a one-time `= _BACKEND.xxx` binding)
# so each call re-checks calendar_source and can route to `_BACKEND`, a
# fresh backend swapped in by a test, or a local no-op depending on the
# CURRENT setting rather than whatever it was at import time.

def get_todays_meetings() -> List[dict]:
    if not _local_calendar_allowed():
        return []
    return _BACKEND.get_todays_meetings()


def get_meetings_for_date(target_date: datetime.date) -> List[dict]:
    if not _local_calendar_allowed():
        return []
    return _BACKEND.get_meetings_for_date(target_date)


def get_upcoming_meetings(hours_ahead: int = 168) -> List[dict]:
    if not _local_calendar_allowed():
        return []
    return _BACKEND.get_upcoming_meetings(hours_ahead)


def get_meeting_detail(subject: str, start_iso: str) -> dict:
    if not _local_calendar_allowed():
        return dict(_EMPTY_DETAIL)
    return _BACKEND.get_meeting_detail(subject, start_iso)


def is_outlook_available() -> bool:
    """Whether the local calendar backend (Outlook COM / EventKit) is
    reachable. Gated like every other function here: "extension"/"off"
    never call into `_BACKEND`, so this never launches/attaches to
    Outlook when the user has opted out. `/calendar/available` layers
    its own extension-aware answer on top for "extension" mode — see
    server.py."""
    if not _local_calendar_allowed():
        return False
    return _BACKEND.is_outlook_available()


def invalidate_calendar_cache() -> None:
    # Safe regardless of calendar_source — this only clears an
    # in-memory dict inside the backend module and never touches
    # Outlook COM / EventKit itself.
    return _BACKEND.invalidate_calendar_cache()


make_session_name = _BACKEND.make_session_name  # pure string formatting, no calendar I/O
