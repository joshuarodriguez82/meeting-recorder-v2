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

# Re-export the same names the old single-file module exposed so server.py
# and calendar_monitor.py keep working unchanged.
get_todays_meetings = _BACKEND.get_todays_meetings
get_meetings_for_date = _BACKEND.get_meetings_for_date
get_upcoming_meetings = _BACKEND.get_upcoming_meetings
is_outlook_available = _BACKEND.is_outlook_available
invalidate_calendar_cache = _BACKEND.invalidate_calendar_cache
make_session_name = _BACKEND.make_session_name
