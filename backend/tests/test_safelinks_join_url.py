"""
Teams meetings imported with no join link on Microsoft 365 tenants that
use Defender Safe Links (field report 2026-10-01: 44 meetings, links only
on the Zoom ones).

Safe Links rewrites every invite link to
safelinks.protection.outlook.com with the destination percent-encoded
in ``url=``. The host-matching extractor never recognised it; the two
substring extractors (Outlook desktop, macOS EventKit) accepted it by
accident and handed back the wrapper. All three now unwrap first, and
only a destination that is a real provider join URL is ever accepted.

The wrapper follows Microsoft's documented Safe Links shape; the
destination and every token in it are synthetic.
"""

from __future__ import annotations

from urllib.parse import quote

from services.extension_calendar_service import (
    find_join_url_in_text, join_provider_for_url, unwrap_safelink)

TEAMS = ("https://teams.microsoft.com/l/meetup-join/19%3ameeting_EXAMPLEID"
         "%40thread.v2/0")
NEW_TEAMS = "https://teams.microsoft.com/meet/0000000000000?p=EXAMPLEPASS"


def safe(dest):
    return ("https://nam12.safelinks.protection.outlook.com/?url="
            + quote(dest, safe="")
            + "&data=05%7C02%7Cuser%40example.com%7CEXAMPLE%7C0"
            + "&sdata=EXAMPLESIG%3D&reserved=0")


def test_unwrap_returns_the_destination():
    assert unwrap_safelink(safe(TEAMS)) == TEAMS
    assert unwrap_safelink(safe(safe(NEW_TEAMS))) == NEW_TEAMS


def test_unwrap_leaves_other_urls_alone():
    assert unwrap_safelink(TEAMS) == TEAMS
    assert unwrap_safelink("") == ""


def test_a_wrapped_teams_link_is_recognised():
    assert join_provider_for_url(safe(TEAMS)) == "teams"
    assert join_provider_for_url(safe(NEW_TEAMS)) == "teams"


def test_a_wrapped_non_meeting_link_is_still_rejected():
    assert join_provider_for_url(safe("https://example.com/doc")) == ""
    assert join_provider_for_url(safe("https://zoom.us/pricing")) == ""


def test_the_link_is_found_in_a_rewritten_invite_body():
    html = ('<a href="' + safe(TEAMS).replace("&", "&amp;")
            + '">Join the meeting now</a> Meeting ID: 000 000 000')
    assert find_join_url_in_text(html) == TEAMS


def test_plain_links_still_work():
    assert find_join_url_in_text(
        "Join: https://zoom.us/j/0000000000?pwd=X") == \
        "https://zoom.us/j/0000000000?pwd=X"


def test_desktop_and_mac_extractors_return_the_destination(monkeypatch):
    import sys
    import types
    # The Outlook module imports win32com at load; it is Windows-only
    # and the extractor under test never touches it.
    win32com = types.ModuleType("win32com")
    win32com.client = types.ModuleType("win32com.client")
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", win32com.client)
    from services import _calendar_outlook, _calendar_eventkit
    body = "Join the meeting now <" + safe(TEAMS) + ">"
    assert _calendar_outlook._extract_join_url(body) == TEAMS
    assert _calendar_eventkit._extract_join_url(body) == TEAMS
