"""
Tests for the Outlook Web scraper service.

We can't actually launch Chrome in CI — Playwright + the user's system
Chrome aren't reachable from the test runner — so these tests cover the
pure-function parts of the module and the contract around its error
classes. The real "does it work against my tenant" check happens on
the user's machine when they install the build.

What's pinned here:
  - Auth-expired detection keys (login-URL substrings).
  - The free-form text formatter that stitches scraped OWA text with
    open action items, since that blob is what the LLM parser eats —
    if it changes shape, the parser will produce a different briefing.
  - The lazy-import contract: importing the module without Playwright
    installed must succeed; only calling the scraper functions raises
    OutlookScraperUnavailable.
  - The profile directory placement (next to briefings/) so a user
    wiping recorder state wipes the web session too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# The scraper module is self-contained (no recording-service deps),
# so we can import it directly without the AST-extraction dance the
# provider-model-fetch tests use.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import outlook_web_scraper as ows  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# _url_looks_like_login
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://login.microsoftonline.com/common/oauth2/authorize?...",
    "https://login.live.com/login.srf?wa=wsignin1.0",
    "https://login.microsoft.com/common/oauth2/v2.0/authorize?...",
    "https://outlook.office.com/owa/auth/signin.aspx",
    "https://outlook.office.com/Common/oauth2/authorize",
    "https://login.microsoftonline.com/COMMON/OAuth2/Authorize",  # case
])
def test_url_looks_like_login_true_for_known_signin_domains(url: str):
    assert ows._url_looks_like_login(url) is True


@pytest.mark.parametrize("url", [
    "https://outlook.office.com/calendar/view/day",
    "https://outlook.office.com/mail/",
    "https://outlook.office.com/calendar/0/view/day",
    "",
    "about:blank",
])
def test_url_looks_like_login_false_for_calendar_pages(url: str):
    assert ows._url_looks_like_login(url) is False


# ──────────────────────────────────────────────────────────────────────
# format_for_briefing_parser
# ──────────────────────────────────────────────────────────────────────

def test_format_blob_just_owa_text():
    """OWA-only blob — no action items — should still produce a
    section header so the LLM parser knows where the calendar is."""
    out = ows.format_for_briefing_parser("9:00 AM Sync with Acme")
    assert "Today's Outlook Calendar" in out
    assert "9:00 AM Sync with Acme" in out
    # No action-items section when none provided.
    assert "Open action items" not in out


def test_format_blob_joins_action_items():
    """When the recorder has open action items, they should appear as
    a labeled section after the calendar so the LLM picks them up as
    needs-response items rather than calendar events."""
    out = ows.format_for_briefing_parser(
        owa_text="10:30 AM Status with Globex",
        open_actions=[
            {"title": "Send updated SOW", "who": "Joshua", "due": "today",
             "source": "Acme discovery 6/26"},
            {"title": "Review architecture diagram", "who": "Sarah"},
        ],
    )
    assert "Today's Outlook Calendar" in out
    assert "10:30 AM Status with Globex" in out
    assert "Open action items from recent meetings" in out
    assert "Send updated SOW" in out
    assert "(owner: Joshua)" in out
    assert "due today" in out
    assert "[from: Acme discovery 6/26]" in out
    # Action with only a title (no who/due/source) shouldn't sprout
    # empty parens or stray "due — " text.
    assert "Review architecture diagram" in out
    assert "Review architecture diagram (owner: Sarah)" in out


def test_format_blob_caps_action_items_at_50():
    """A runaway action-item list (50+) should be capped so the blob
    doesn't blow past the briefing parser's 50KB cap on the
    action-items side alone."""
    actions = [{"title": f"Item {i}"} for i in range(200)]
    out = ows.format_for_briefing_parser("", actions)
    # Each line is "- Item N"; count them.
    item_lines = [l for l in out.splitlines() if l.startswith("- Item ")]
    assert len(item_lines) == 50


def test_format_blob_skips_actions_without_title():
    """A malformed action item (no title) shouldn't render as a
    dangling dash with nothing after it."""
    out = ows.format_for_briefing_parser(
        "1 PM thing",
        [{"who": "ghost"}, {"title": "real one"}],
    )
    lines = out.splitlines()
    # Only the real one shows up, not the title-less ghost.
    item_lines = [l for l in lines if l.startswith("-")]
    assert len(item_lines) == 1
    assert "real one" in item_lines[0]


def test_format_blob_handles_empty_owa_text():
    """Empty OWA text (no calendar events today) should render a
    placeholder so the LLM doesn't think the scraper silently broke."""
    out = ows.format_for_briefing_parser("")
    assert "(no events visible)" in out


# ──────────────────────────────────────────────────────────────────────
# Teams formatter integration (v2.15.0+)
# ──────────────────────────────────────────────────────────────────────

def test_format_blob_joins_teams_text_with_owa():
    """When teams_text is provided, it gets its own labeled section
    after the calendar so the LLM lifts @mentions / replies into the
    needs_response category — not the agenda one."""
    out = ows.format_for_briefing_parser(
        owa_text="9:00 AM Sync with Acme",
        teams_text=(
            "Sarah @Joshua mentioned you 8:42 AM: please review the SOW\n"
            "Missed call from Bob, 8:55 AM"
        ),
    )
    # OWA section still present and labeled.
    assert "Today's Outlook Calendar" in out
    assert "9:00 AM Sync with Acme" in out
    # Teams section is present, labeled, and contains the raw text.
    assert "Teams Activity" in out
    assert "Sarah @Joshua mentioned you" in out
    assert "Missed call from Bob" in out
    # Section order matters for the LLM prompt — Teams sits between
    # calendar and action items so the agenda + needs_response
    # extraction stays grouped logically.
    owa_idx = out.find("Today's Outlook Calendar")
    teams_idx = out.find("Teams Activity")
    assert owa_idx < teams_idx, "Teams section should follow OWA"


def test_format_blob_omits_teams_section_when_empty():
    """Empty or None teams_text shouldn't render an empty Teams
    section header — Teams failures are explicitly silent so the
    user's brief still looks correct without it."""
    out_none = ows.format_for_briefing_parser("event", teams_text=None)
    assert "Teams Activity" not in out_none
    out_empty = ows.format_for_briefing_parser("event", teams_text="")
    assert "Teams Activity" not in out_empty
    out_whitespace = ows.format_for_briefing_parser(
        "event", teams_text="   \n  \n")
    assert "Teams Activity" not in out_whitespace


def test_format_blob_full_combination():
    """All three sections (OWA + Teams + open actions) coexist in the
    expected order so the LLM's parse_daily_briefing prompt receives a
    consistent layout."""
    out = ows.format_for_briefing_parser(
        owa_text="10am standup",
        teams_text="@Joshua please review",
        open_actions=[{"title": "Send proposal", "who": "Joshua"}],
    )
    owa_idx = out.find("Today's Outlook Calendar")
    teams_idx = out.find("Teams Activity")
    actions_idx = out.find("Open action items")
    assert 0 <= owa_idx < teams_idx < actions_idx


# ──────────────────────────────────────────────────────────────────────
# profile_dir_for — v2.15.0+ contract
# ──────────────────────────────────────────────────────────────────────

def test_profile_dir_placed_under_user_data_dir(tmp_path: Path):
    """The persistent profile lives under USER_DATA_DIR (LOCAL-only).
    v2.14.0 put it under recordings_dir which often lives on Google
    Drive Stream / OneDrive; cloud-sync filter drivers corrupt
    Chrome's cookie store and SingletonLock writes, so the headed
    sign-in's cookies never become available to the subsequent
    headless scrape. Symptom: empty calendar even right after
    sign-in. The directory NAME stays "web-session" so an upgraded
    user just needs to re-sign-in once (no migration required)."""
    got = ows.profile_dir_for(tmp_path)
    assert got == tmp_path / "web-session"


# ──────────────────────────────────────────────────────────────────────
# Error class contract
# ──────────────────────────────────────────────────────────────────────

def test_error_subclasses_share_base():
    """Callers (server.py endpoints) catch each subclass separately to
    map to distinct HTTP status codes. The base class lets a generic
    handler still catch all scraper failures."""
    assert issubclass(ows.OutlookAuthExpired, ows.OutlookScraperError)
    assert issubclass(ows.OutlookScraperUnavailable, ows.OutlookScraperError)
    assert issubclass(ows.OutlookScraperError, RuntimeError)


# ──────────────────────────────────────────────────────────────────────
# Lazy-import contract
# ──────────────────────────────────────────────────────────────────────

def test_module_importable_even_without_playwright(monkeypatch):
    """We import playwright lazily so the rest of the backend keeps
    working when the venv hasn't been re-bootstrapped after we added
    the dep. Pretend playwright isn't there and confirm we still get a
    clean module + a clean error class on first invocation."""
    # Hide playwright from sys.modules and the import system
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    with pytest.raises(ows.OutlookScraperUnavailable) as exc:
        ows._import_playwright()
    msg = str(exc.value)
    assert "Playwright" in msg or "playwright" in msg
    # Should suggest restart so the bootstrap re-installs from
    # requirements-*.txt; don't strand the user without guidance.
    assert "Restart" in msg or "restart" in msg
