"""
"Calendar: Not connected" on a machine that has a working calendar.

THE FIELD BUG
-------------
Reported three times, always the same shape: calendar works on macOS,
doesn't on Windows, same app version, same account. Each previous look
went hunting in the Chrome extension's detail-pass budget. The actual
asymmetry is one line of backend logic.

In `auto` mode — the default — `/calendar/upcoming` merges TWO sources:
the local calendar (Outlook COM on Windows, EventKit on macOS) and
whatever the Chrome extension scraped from Outlook Web. But
`/calendar/available`, which drives the Record tab's readiness chip,
probed the LOCAL source only. So:

    macOS   EventKit answers            -> "Connected"
    Windows Outlook COM unreachable     -> "Not connected"

on the same account, with the extension feeding events perfectly well on
both. And Windows is where COM is reachable least often now: the "new"
Outlook has no COM automation surface at all, so every user Microsoft
migrates loses it silently. The backend already knew — `_get_outlook`
logs "If using New Outlook, switch to Classic or meetings won't load" —
but it told the log, not the user, and the chip's hint said to go
"connect a calendar in Settings" when one was already connected.

This is the house defect again, in the place it does the most damage:
**a result you couldn't read must never render as a result that isn't
there.**

WHY THE LOGIC IS A PURE FUNCTION
--------------------------------
`server.py` needs the full dependency set to import, so nothing cheap
can assert on a route. That is exactly how /health told everyone it was
version 2.0.0 for 70 releases. The decision lives in calendar_feed and
the route calls it.
"""

from __future__ import annotations

import pytest

from services import calendar_feed as cf


def _ext(count: int = 0, **kw):
    """An ExtensionCalendarService.capture_status-shaped dict."""
    status = {
        "updated_at": None, "event_count": count, "future_event_count": count,
        "last_import_path": None, "last_import_raw": None,
        "last_import_kept": None, "last_import_dropped": None,
        "last_import_fallback_reason": None, "last_import_at": None,
    }
    status.update(kw)
    return status


class TestAutoMode:
    """`auto` merges both sources, so it must report on both."""

    def test_extension_alone_is_a_connected_calendar(self):
        """THE BUG. Windows, new Outlook, extension syncing fine: the
        panel shows meetings and the chip said "Not connected"."""
        r = cf.calendar_availability("auto", local_available=False,
                                     extension=_ext(54), platform="win32")
        assert r["available"] is True
        assert r["local_available"] is False
        assert r["event_count"] == 54

    def test_local_alone_is_a_connected_calendar(self):
        r = cf.calendar_availability("auto", local_available=True,
                                     extension=_ext(0), platform="darwin")
        assert r["available"] is True
        assert r["local_available"] is True

    def test_neither_source_is_unavailable_with_a_reason(self):
        r = cf.calendar_availability("auto", local_available=False,
                                     extension=_ext(0), platform="win32")
        assert r["available"] is False
        assert r["reason"], "an unavailable calendar must say why"

    def test_the_windows_reason_names_new_outlook(self):
        """The single most common cause, and the one the user cannot
        guess: new Outlook exposes no COM automation at all."""
        r = cf.calendar_availability("auto", local_available=False,
                                     extension=_ext(0), platform="win32")
        assert "outlook" in r["reason"].lower()
        assert "classic" in r["reason"].lower()

    def test_the_macos_reason_does_not_talk_about_outlook_versions(self):
        """macOS reads EventKit and the usual cause is the privacy
        permission, so the Windows advice would be actively misleading."""
        r = cf.calendar_availability("auto", local_available=False,
                                     extension=_ext(0), platform="darwin")
        assert "classic" not in r["reason"].lower()
        assert "permission" in r["reason"].lower()

    def test_which_source_answered_is_reported(self):
        """The chip says "Connected (Chrome extension)" rather than a
        bare "Connected" — otherwise a user whose Outlook silently died
        has no way to notice the app is running on the fallback."""
        r = cf.calendar_availability("auto", local_available=False,
                                     extension=_ext(3), platform="win32")
        assert r["sources_answering"] == ["extension"]
        r = cf.calendar_availability("auto", local_available=True,
                                     extension=_ext(3), platform="win32")
        assert r["sources_answering"] == ["local", "extension"]


class TestOutlookOnlyMode:
    """`outlook` means the user deliberately excluded the extension —
    its events must not make the calendar look connected."""

    def test_extension_events_do_not_count(self):
        r = cf.calendar_availability("outlook", local_available=False,
                                     extension=_ext(54), platform="win32")
        assert r["available"] is False
        assert r["sources_answering"] == []

    def test_local_counts(self):
        r = cf.calendar_availability("outlook", local_available=True,
                                     extension=_ext(0), platform="win32")
        assert r["available"] is True


class TestExtensionOnlyMode:
    """Pre-existing behaviour, pinned so this change can't alter it:
    availability follows the extension and Outlook is never consulted."""

    def test_events_mean_available(self):
        r = cf.calendar_availability("extension", local_available=False,
                                     extension=_ext(2), platform="win32")
        assert r["available"] is True

    def test_no_events_is_unavailable_with_a_reason(self):
        r = cf.calendar_availability("extension", local_available=False,
                                     extension=_ext(0), platform="win32")
        assert r["available"] is False
        assert "extension" in r["reason"].lower()

    def test_a_reachable_outlook_is_ignored(self):
        """The whole point of the mode is never touching Outlook; a
        stray True must not leak in and make it look connected."""
        r = cf.calendar_availability("extension", local_available=True,
                                     extension=_ext(0), platform="win32")
        assert r["available"] is False


class TestOff:
    def test_off_is_never_available_and_says_so_plainly(self):
        r = cf.calendar_availability("off", local_available=True,
                                     extension=_ext(9), platform="darwin")
        assert r["available"] is False
        assert r["sources_answering"] == []
        assert "off" in r["reason"].lower() or "turned off" in r["reason"].lower()


class TestShape:
    @pytest.mark.parametrize("source", ["auto", "outlook", "extension", "off"])
    def test_every_mode_returns_the_same_keys(self, source: str):
        """The frontend reads one shape regardless of mode; a key that
        appears in only some modes is how the old response left `auto`
        with no counts to render."""
        r = cf.calendar_availability(source, local_available=False,
                                     extension=_ext(0), platform="win32")
        for key in ("available", "source", "local_available",
                    "sources_answering", "reason", "event_count"):
            assert key in r, f"{source} response is missing {key}"
        assert r["source"] == source

    def test_an_available_calendar_has_no_reason(self):
        """`reason` is why it ISN'T working. Present-but-empty when it
        is, so the UI can render it unconditionally."""
        r = cf.calendar_availability("auto", local_available=True,
                                     extension=_ext(1), platform="darwin")
        assert r["reason"] == ""
