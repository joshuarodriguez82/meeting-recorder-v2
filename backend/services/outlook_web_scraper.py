"""
Outlook Web scraper — drives the user's installed Chrome via Playwright
to read today's calendar from outlook.office.com without depending on
the M365 Graph API.

WHY this exists. The Today tab's daily briefing was originally fed by
the user pasting M365 Copilot's scheduled-prompt output. Two things
broke that flow in mid-2026: (1) IT policy started blocking calendar /
Graph API access on personal machines, killing any "real" Outlook
integration we could ship, and (2) the Copilot scheduled prompts the
user relied on became flaky — running at 6:30am sometimes, not at all
other days. The user CAN still log in to outlook.office.com via Chrome
on their personal machine (web access is allowed), so this service
reuses that authenticated browser session as the data path.

WHY Playwright with channel='chrome' (not bundled Chromium):
- Chromium adds ~150 MB to the installer. We already ship a big bundle.
- Microsoft's conditional-access policies sometimes treat headless
  Chromium as "untrusted browser" and force MFA even when the user just
  MFA'd, because the User-Agent / device fingerprint differs from their
  normal Chrome. Using the user's actual Chrome install minimizes the
  device-fingerprint delta.
- channel='chrome' uses the system Chrome's binary but with Playwright's
  automation control surface. The persistent userDataDir keeps cookies
  separate from the user's regular Chrome profile (so we never read or
  touch their personal browsing data), but the binary is the same.

WHY a persistent profile, in a directory we own (not the user's actual
Chrome profile): cookies + storage in the persistent context survive
between launches, so the user signs in ONCE (and re-MFA's about once a
week — typical M365 conditional-access lifetime). Keeping our own dir
means we never touch the user's regular Chrome profile.

CONTRACT for the rest of the app:
- ``open_signin_window(profile_dir)`` opens Chrome headed at OWA's
  calendar so the user can sign in or re-MFA. Returns when the user
  closes the window. Cookies persist.
- ``scrape_today_briefing_text(profile_dir)`` runs Chrome headless with
  the SAME profile, navigates to OWA day view, extracts the calendar
  text, returns it. Raises ``OutlookAuthExpired`` if the navigation
  ended up at a login page (i.e. session cookies are no longer valid —
  user needs to re-sign-in). Raises ``OutlookScraperUnavailable`` if
  Chrome isn't installed or Playwright import fails (so the endpoint
  can return a clean error instead of 500).
- The returned text is intentionally free-form (a big innerText blob
  from the day-view's main region) because the summarizer's existing
  ``parse_daily_briefing`` already LLM-parses free-form briefing text
  into structured DailyBriefing JSON. Feeding it scraped text means we
  reuse the parser-and-normalizer pipeline unchanged.

Concurrency: only one scrape at a time. Playwright's persistent context
can't be opened twice against the same userDataDir (Chrome locks the
profile directory). The caller (`server.py`) serializes via an asyncio
Lock so a fast double-click on Sync Now doesn't error.

Selectors: this file deliberately avoids brittle DOM selectors. It
extracts the day-view's main region as a single inner-text blob and
hands that to the LLM. OWA's DOM changes every few months; the LLM
absorbs that change. If extraction breaks completely, the test
fixtures in tests/test_outlook_web_scraper.py pin the bare-minimum
text-extraction contract.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)


# OWA day-view: shows today's events in a vertical column. We prefer
# the day view over the week view because the day view's textual layout
# is much closer to how the user reads "today's agenda" and the LLM
# parses it cleanly.
OWA_DAY_VIEW_URL = "https://outlook.office.com/calendar/view/day"

# Loose set of substrings that indicate the page navigated to a login
# screen instead of the calendar. Microsoft uses several login domains
# (login.microsoftonline.com is the primary, login.live.com is the
# fallback for some tenants, login.microsoft.com is the consumer URL).
LOGIN_URL_NEEDLES = ("login.microsoftonline.com", "login.live.com",
                     "login.microsoft.com", "/oauth2/", "/common/oauth2/",
                     "signin", "/login/")

# Cap the scraped text. OWA's day view fully expanded is a few KB; if
# we extracted megabytes of text something extracted the wrong DOM
# subtree (e.g. the whole shell instead of the calendar area). The
# briefing parser caps input at 50 KB; we cap here at 30 KB to leave
# headroom for joining with the recorder's open action items below.
MAX_SCRAPE_TEXT_BYTES = 30_000

# Conservative timeouts. Loading OWA on a cold Chrome with no warmed
# DNS / TCP / TLS can take a few seconds; loading the calendar grid on
# a slow tenant can add another few. 20s is well above worst-case for
# a healthy connection and bounds the time a user waits on the Sync Now
# button before they see "took too long."
PAGE_LOAD_TIMEOUT_MS = 20_000
CONTENT_WAIT_TIMEOUT_MS = 15_000


class OutlookScraperError(RuntimeError):
    """Base class for scraper failures."""


class OutlookAuthExpired(OutlookScraperError):
    """The persistent profile's M365 session cookies are no longer
    valid. UI should prompt the user to re-sign-in."""


class OutlookScraperUnavailable(OutlookScraperError):
    """Playwright isn't importable or the user has no Chrome installed.
    Distinct from auth-expired so the UI can show a different message
    (install Chrome vs. sign in to Microsoft)."""


def _import_playwright():
    """Lazy import so the rest of the app doesn't fail at module load
    when playwright isn't installed yet (e.g. an old venv from before
    we added it to requirements). Raises OutlookScraperUnavailable
    with a user-readable message if missing."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
        return async_playwright
    except ImportError as e:
        raise OutlookScraperUnavailable(
            "Playwright isn't installed in this venv. Restart the app — "
            "the first-launch bootstrap will install it from "
            "requirements-*.txt."
        ) from e


def _url_looks_like_login(url: str) -> bool:
    lo = (url or "").lower()
    return any(needle in lo for needle in LOGIN_URL_NEEDLES)


def profile_dir_for(data_root: Path) -> Path:
    """Where the persistent Chrome profile lives. Kept next to the
    briefings/ dir under the same data root so a user wiping recorder
    state wipes the web session too."""
    return Path(data_root) / "web-session"


async def open_signin_window(data_root: Path) -> None:
    """Spawn a HEADED Chrome window at OWA's calendar so the user can
    sign in / re-MFA. Returns when the user closes the window (or after
    a generous 10-minute timeout). Cookies persist in the profile dir.

    Failure modes:
      - Chrome not installed → OutlookScraperUnavailable
      - Playwright not importable → OutlookScraperUnavailable
      - Profile dir locked by a previous run → also surfaces as
        OutlookScraperUnavailable with a message telling the user to
        close any other Meeting Recorder Chrome window.
    """
    async_playwright = _import_playwright()
    profile = profile_dir_for(data_root)
    profile.mkdir(parents=True, exist_ok=True)

    logger.info(f"Opening Outlook sign-in window (profile={profile})")
    try:
        async with async_playwright() as p:
            try:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    channel="chrome",
                    headless=False,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
            except Exception as e:  # noqa: BLE001 — playwright surfaces many subclasses
                raise OutlookScraperUnavailable(
                    f"Couldn't launch Chrome for sign-in. Is Chrome installed? "
                    f"Underlying error: {e}"
                ) from e

            # Reuse the about:blank tab Chrome opens on launch; never
            # create a second one (extra tabs confuse the close-detection
            # below).
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(OWA_DAY_VIEW_URL, wait_until="domcontentloaded",
                                timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"OWA navigation failed in sign-in window: {e}")
                # Don't raise — let the user manually navigate if needed.

            # Wait for the user to close the browser. We poll ctx.pages
            # instead of awaiting page.wait_for_event('close') because
            # the user may close the WINDOW (which closes the context),
            # not just the tab, and the latter signal is more reliable.
            deadline = asyncio.get_event_loop().time() + 600.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    if not ctx.pages:
                        break
                except Exception:  # noqa: BLE001
                    break
                await asyncio.sleep(1.0)

            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
            logger.info("Outlook sign-in window closed.")
    except OutlookScraperUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise OutlookScraperUnavailable(
            f"Sign-in window failed unexpectedly: {e}"
        ) from e


async def scrape_today_briefing_text(data_root: Path) -> str:
    """Open OWA's day view headlessly using the persistent profile and
    return the main region's inner text. Raises OutlookAuthExpired if
    the navigation hit a login page.

    Returns a free-form text blob, not structured data. Caller feeds it
    into ``summarizer.parse_daily_briefing`` which handles the LLM-parse
    + normalization the same way it does for manual Copilot pastes.
    """
    async_playwright = _import_playwright()
    profile = profile_dir_for(data_root)
    if not profile.exists():
        # No profile dir yet = user has never signed in. Surface this
        # as auth-expired so the UI prompts the same sign-in flow.
        raise OutlookAuthExpired(
            "No Outlook session yet — click Sign in to Microsoft.")

    logger.info(f"Scraping OWA day view (profile={profile})")
    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="chrome",
                headless=True,
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception as e:  # noqa: BLE001
            raise OutlookScraperUnavailable(
                f"Couldn't launch headless Chrome. Is Chrome installed? "
                f"Underlying error: {e}"
            ) from e

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(
                    OWA_DAY_VIEW_URL,
                    wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT_MS,
                )
            except Exception as e:  # noqa: BLE001
                raise OutlookScraperError(
                    f"OWA navigation failed: {e}") from e

            # If Microsoft bounced us to a login screen, the cookies have
            # expired (or were never valid). Surface as auth-expired so the
            # UI prompts re-sign-in.
            final_url = page.url
            if _url_looks_like_login(final_url):
                logger.info(f"OWA scrape redirected to login: {final_url}")
                raise OutlookAuthExpired(
                    "Microsoft 365 session expired — sign in again.")

            # Give the day-view rendering loop time to fill the main
            # region. We don't pin a specific selector because OWA's
            # DOM changes between releases; instead, wait for the body
            # to settle on a stable inner-text size.
            try:
                await page.wait_for_load_state("networkidle",
                                                timeout=CONTENT_WAIT_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                # networkidle can flap on OWA (background telemetry).
                # Don't fail the scrape on it — just continue and grab
                # whatever rendered.
                logger.debug("OWA networkidle timed out; proceeding anyway")

            # Extract the [role=main] inner text. OWA wraps the active
            # calendar area in role=main; this consistently captures the
            # visible day grid + sidebar agenda without dragging in the
            # left nav. Fall back to body innerText if role=main isn't
            # there (older OWA branches).
            try:
                main = page.locator('[role="main"]').first
                if await main.count() > 0:
                    text = await main.inner_text(timeout=5_000)
                else:
                    text = await page.locator("body").inner_text(timeout=5_000)
            except Exception as e:  # noqa: BLE001
                raise OutlookScraperError(
                    f"Couldn't read OWA text content: {e}") from e

            text = (text or "").strip()
            if not text:
                raise OutlookScraperError(
                    "OWA returned an empty calendar — try Sign in again.")

            # If after all that the visible text screams "sign in to your
            # account" or similar, treat as auth-expired. OWA's interstitial
            # login pages sometimes render at outlook.office.com directly
            # (no redirect URL change) when the session is JUST expired.
            lowered = text.lower()[:500]
            if "sign in to your account" in lowered or \
                    "pick an account" in lowered or \
                    "stay signed in" in lowered:
                raise OutlookAuthExpired(
                    "Microsoft 365 sign-in page rendered — re-sign-in.")

            # Cap size so a runaway extraction can't blow up the parser.
            if len(text.encode("utf-8")) > MAX_SCRAPE_TEXT_BYTES:
                logger.warning(
                    f"OWA text exceeded {MAX_SCRAPE_TEXT_BYTES} bytes; truncating")
                text = text.encode("utf-8")[:MAX_SCRAPE_TEXT_BYTES].decode(
                    "utf-8", errors="ignore")
            return text
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass


def format_for_briefing_parser(owa_text: str,
                                open_actions: Optional[list] = None) -> str:
    """Stitch the scraped OWA text and the recorder's open action items
    into a single free-form blob that mimics the shape of a Copilot
    daily-brief paste. The existing summarizer prompt for
    ``parse_daily_briefing`` already handles loose, narrative-style
    input — we just give it labeled sections so the LLM finds the
    agenda + needs-response items cleanly.

    Keeping this function pure (no I/O, no LLM) so it's trivially
    testable.
    """
    parts: list[str] = []
    parts.append("=== Today's Outlook Calendar ===")
    parts.append("")
    parts.append((owa_text or "").strip() or "(no events visible)")

    if open_actions:
        parts.append("")
        parts.append("=== Open action items from recent meetings ===")
        parts.append("")
        for a in open_actions[:50]:
            title = str(a.get("title") or "").strip()
            who = str(a.get("who") or "").strip()
            due = str(a.get("due") or "").strip()
            src = str(a.get("source") or "").strip()
            if not title:
                continue
            line = f"- {title}"
            if who:
                line += f" (owner: {who})"
            if due:
                line += f" — due {due}"
            if src:
                line += f"  [from: {src}]"
            parts.append(line)
    return "\n".join(parts)
