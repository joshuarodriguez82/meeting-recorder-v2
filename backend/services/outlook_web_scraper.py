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

# Teams Web Activity feed: shows @mentions, replies, reactions, missed
# calls, and meeting reminders — exactly the "what needs my attention
# today" surface the briefing should reflect. The Chat tab is the next
# obvious extension but it's much noisier (every active 1:1 thread)
# and harder to scope to "today" without DOM-specific filtering.
TEAMS_ACTIVITY_URL = "https://teams.microsoft.com/v2/?clientType=desktop#/activity"

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
# How long we'll wait for the React SPA to mount its actual content
# (calendar grid / activity feed) into the page after initial load.
# v2.15.1's networkidle approach returned in ~500ms because OWA's
# initial bundle loaded fast and telemetry hadn't started yet — we
# extracted from a barely-rendered shell (392 chars in the field
# repro) instead of an actual calendar. v2.15.2 polls for inner-text
# size to settle, which is the right signal for "is the SPA done
# mounting." 30 seconds is enough for cold-cache OWA on a slow
# tenant; Teams Web is heavier and gets a separate 35s cap.
CONTENT_SETTLE_MAX_WAIT_SEC = 30.0
TEAMS_CONTENT_SETTLE_MAX_WAIT_SEC = 35.0

# Inner-text size thresholds. OWA's day view with NO events still
# renders the time-of-day axis ("8 AM", "9 AM", ...) which is roughly
# 600-1000 chars. The chrome alone (left nav + top bar + date strip,
# no calendar grid) is ~250-450 chars. Crossing MIN_USEFUL_CHARS
# means we have at least the grid axis; crossing TARGET_CHARS means
# we likely have meeting tiles too. Below MIN_SCRAPE_FLOOR is a hard
# failure — that's the v2.15.1 bug pattern (calendar didn't render).
OWA_TARGET_CHARS = 1500     # full day view with some events
OWA_MIN_USEFUL_CHARS = 700  # day view axis rendered, possibly no events
TEAMS_TARGET_CHARS = 1500
TEAMS_MIN_USEFUL_CHARS = 500
MIN_SCRAPE_FLOOR = 500      # below this, treat as "didn't render"

# How many consecutive equal-size polls before we declare the text
# "stable" and stop waiting. Each poll is ~1s apart.
STABILITY_POLLS = 3


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


async def _wait_for_text_to_settle(
    locator,
    *,
    target_chars: int,
    min_useful_chars: int,
    max_wait_sec: float,
    label: str,
) -> str:
    """Poll the locator's inner-text every second until ONE of:
      1. it reaches ``target_chars`` (we have what we want, return early),
      2. it stops growing for ``STABILITY_POLLS`` polls in a row (the
         page has settled; whatever's there is what we get), or
      3. ``max_wait_sec`` elapses (give up and return whatever we have).

    Returns the final text. Logs what happened so future repros are
    easy to diagnose from backend.log alone.

    Why this replaces ``wait_for_load_state("networkidle")``: in v2.15.1
    OWA's React tree paints the menu shell first (~400 chars), then
    networkidle fires when telemetry is briefly quiet (before the
    calendar grid mounts), then we extracted from the shell-only DOM
    and got the empty-brief bug. Polling actual inner-text size is
    the only reliable signal that "the SPA is done mounting."
    """
    start = asyncio.get_event_loop().time()
    deadline = start + max_wait_sec
    last_len = -1
    stable_count = 0
    last_text = ""

    while asyncio.get_event_loop().time() < deadline:
        try:
            text = await locator.inner_text(timeout=3000)
            text = (text or "").strip()
            last_text = text
            now_len = len(text)
            elapsed = asyncio.get_event_loop().time() - start

            if now_len >= target_chars:
                logger.info(
                    f"{label}: content reached target ({now_len} chars) "
                    f"in {elapsed:.1f}s")
                return text

            if now_len == last_len and now_len > 0:
                stable_count += 1
                if stable_count >= STABILITY_POLLS:
                    # Text has been the same for N polls in a row —
                    # the page is done loading whatever it's going to
                    # load. If it's above the "useful" floor we
                    # accept it (e.g. empty calendar day legitimately
                    # has fewer chars). Below the floor is the bug
                    # case — log distinctly and return anyway; the
                    # caller decides whether to raise.
                    if now_len >= min_useful_chars:
                        logger.info(
                            f"{label}: content stable at {now_len} chars "
                            f"(below target {target_chars} but above "
                            f"useful floor {min_useful_chars}) "
                            f"after {elapsed:.1f}s")
                    else:
                        logger.warning(
                            f"{label}: content stable at {now_len} chars "
                            f"after {elapsed:.1f}s — below useful floor "
                            f"({min_useful_chars}). SPA may not have "
                            f"finished mounting; brief may be incomplete.")
                    return text
            else:
                stable_count = 0
                last_len = now_len
        except Exception as e:  # noqa: BLE001
            # inner_text occasionally throws on Playwright transient
            # render-tree changes. Don't bail — just skip this poll.
            logger.debug(f"{label}: inner_text poll failed: {e}")

        await asyncio.sleep(1.0)

    logger.warning(
        f"{label}: content never settled within {max_wait_sec}s; "
        f"using last={len(last_text)} chars.")
    return last_text


def _url_looks_like_login(url: str) -> bool:
    lo = (url or "").lower()
    return any(needle in lo for needle in LOGIN_URL_NEEDLES)


def profile_dir_for(user_data_dir: Path) -> Path:
    """Where the persistent Chrome profile lives. Must be a LOCAL-only
    path — passing the user's ``recordings_dir`` here (v2.14.0
    behavior) was a bug because that directory is frequently a
    cloud-synced folder (Google Drive Stream, OneDrive, iCloud). Chrome
    stores cookies, IndexedDB, and a SingletonLock file in the profile;
    putting any of those on a cloud-sync filter driver causes the same
    contention class the v2.12.0 audio-read fix solved — the headed
    sign-in window writes cookies that the headless scrape can't read
    because they haven't synced down yet, and the sync conflict can
    leave the profile partially-locked. Symptom: empty calendar
    extraction even right after a successful sign-in.

    v2.15.0+ pins this to ``USER_DATA_DIR`` (Windows: %LOCALAPPDATA%,
    macOS: ~/Library/Application Support, Linux: ~/.config) — same
    LOCAL-only rationale as speaker profiles and the auto-record
    blocklist, which also must never roam between machines."""
    return Path(user_data_dir) / "web-session"


async def open_signin_window(user_data_dir: Path) -> None:
    """Spawn a HEADED Chrome window with TWO tabs — OWA's calendar
    and Teams Activity — so the user can sign in (or re-MFA) to both
    M365 surfaces in a single session. Returns when the user closes
    the window (or after a generous 10-minute timeout). Cookies
    persist in the profile dir.

    ``user_data_dir`` must be a LOCAL-only path — see ``profile_dir_for``
    for the rationale (v2.14.0 wired this to the user's recordings_dir
    which can be on Google Drive Stream / OneDrive, breaking the
    headed-then-headless cookie handoff).

    v2.15.1+: opens a SECOND tab at Teams Activity. Background:
    Teams Web has its own OAuth dance on top of M365 SSO. Even when
    OWA has minted a valid cookie set, the first visit to
    teams.microsoft.com bounces through login.microsoftonline.com to
    issue a Teams-specific access token, and may hit "Stay signed in?"
    or organization-consent interstitials that the headless scrape
    can't navigate. The fix is to surface that dance during the
    interactive sign-in flow: user signs in to OWA in tab 1 (their
    normal flow), notices Teams loading in tab 2, completes any
    additional prompts there, closes the window. After that one-time
    dance, the headless Teams scrape works because the token is
    cached in the persistent profile.

    Failure modes:
      - Chrome not installed → OutlookScraperUnavailable
      - Playwright not importable → OutlookScraperUnavailable
      - Profile dir locked by a previous run → also surfaces as
        OutlookScraperUnavailable with a message telling the user to
        close any other Meeting Recorder Chrome window.
    """
    async_playwright = _import_playwright()
    profile = profile_dir_for(user_data_dir)
    profile.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"Opening sign-in window (OWA + Teams; profile={profile})")
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

            # Tab 1: OWA day view (primary auth target). Reuses the
            # about:blank tab Chrome opens on launch.
            owa_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await owa_page.goto(
                    OWA_DAY_VIEW_URL, wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"OWA navigation failed in sign-in window: {e}")
                # Don't raise — let the user manually navigate if needed.

            # Tab 2: Teams Activity (secondary auth target). Opening
            # a fresh page object instructs Chrome to make a new tab
            # in the same window. The persistent context shares
            # cookies across tabs, so any token Teams mints lands in
            # the same profile the headless scrape will read later.
            try:
                teams_page = await ctx.new_page()
                await teams_page.goto(
                    TEAMS_ACTIVITY_URL, wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001
                # Teams tab failing here isn't fatal for sign-in —
                # the user can still authenticate to OWA in tab 1
                # and Sync now will publish an OWA-only brief. We
                # log so future diagnosis sees what happened.
                logger.warning(
                    f"Teams tab failed to open in sign-in window "
                    f"(OWA tab still works): {e}")

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
            logger.info("Sign-in window closed.")
    except OutlookScraperUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise OutlookScraperUnavailable(
            f"Sign-in window failed unexpectedly: {e}"
        ) from e


async def scrape_today_briefing_text(user_data_dir: Path) -> str:
    """Open OWA's day view headlessly using the persistent profile and
    return the main region's inner text. Raises OutlookAuthExpired if
    the navigation hit a login page.

    Returns a free-form text blob, not structured data. Caller feeds it
    into ``summarizer.parse_daily_briefing`` which handles the LLM-parse
    + normalization the same way it does for manual Copilot pastes.

    ``user_data_dir`` must be a LOCAL-only path. See ``profile_dir_for``
    for why — v2.14.0 used the user's recordings_dir which sync-locked
    Chrome's cookie store on cloud volumes.
    """
    async_playwright = _import_playwright()
    profile = profile_dir_for(user_data_dir)
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

            # Wait for the day-view's React tree to actually mount its
            # content. v2.15.1 used wait_for_load_state("networkidle")
            # which returned in ~500ms before the calendar grid
            # rendered (resulting in 392-char extraction of just the
            # menu chrome). v2.15.2 polls inner-text size: returns
            # early when content is rich, falls back to stability
            # detection when the day is genuinely empty, hard caps at
            # 30s so the user's wait is bounded.
            try:
                main = page.locator('[role="main"]').first
                main_present = await main.count() > 0
                locator = main if main_present else page.locator("body")
                text = await _wait_for_text_to_settle(
                    locator,
                    target_chars=OWA_TARGET_CHARS,
                    min_useful_chars=OWA_MIN_USEFUL_CHARS,
                    max_wait_sec=CONTENT_SETTLE_MAX_WAIT_SEC,
                    label="OWA",
                )
            except Exception as e:  # noqa: BLE001
                raise OutlookScraperError(
                    f"Couldn't read OWA text content: {e}") from e

            text = (text or "").strip()
            if not text:
                raise OutlookScraperError(
                    "OWA returned an empty calendar — try Sign in again.")

            # Hard floor — below this, the calendar grid never
            # rendered. Raising here lets the UI show a distinct
            # "content didn't load" error instead of silently
            # publishing an empty brief like v2.15.1 did.
            if len(text) < MIN_SCRAPE_FLOOR:
                raise OutlookScraperError(
                    f"OWA's calendar grid didn't render in time "
                    f"(only {len(text)} chars extracted; expected "
                    f"≥{MIN_SCRAPE_FLOOR}). Try Sync again, or click "
                    f"Sign in to Microsoft and confirm your calendar "
                    f"loads in the OWA tab before closing.")

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


async def scrape_today_teams_text(user_data_dir: Path) -> str:
    """Open Teams Web's Activity feed headlessly using the persistent
    profile and return the main region's inner text. Activity is the
    surface that shows @mentions, replies, missed calls, meeting
    reminders — i.e. the "needs my attention today" view, which is the
    right shape for the daily briefing parser. The Chat tab is noisier
    and harder to scope to "today" without filtering at the DOM level;
    Activity is what to ship first.

    Returns "" (empty string, not an error) if Teams fails — the
    briefing should still succeed with just OWA. Specifically:
      - Teams Web is heavier than OWA and more frequently breaks in
        headless Chromium; treating Teams failures as fatal would
        regress what OWA-only already does.
      - Auth-expired flows are surfaced the SAME way as OWA via
        ``OutlookAuthExpired``, since both surfaces share the M365
        session cookies. If we hit login here we want the UI's
        existing banner to fire.

    ``user_data_dir`` is the same LOCAL-only path OWA uses. The same
    persistent profile (cookies, MFA state) authenticates both.
    """
    async_playwright = _import_playwright()
    profile = profile_dir_for(user_data_dir)
    if not profile.exists():
        raise OutlookAuthExpired(
            "No Microsoft session yet — click Sign in to Microsoft.")

    logger.info(f"Scraping Teams activity (profile={profile})")
    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="chrome",
                headless=True,
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception as e:  # noqa: BLE001
            # Same Chrome-missing surface as OWA — distinct from
            # "Teams returned nothing" so the UI knows it's still a
            # setup problem, not a per-surface flake.
            raise OutlookScraperUnavailable(
                f"Couldn't launch headless Chrome for Teams. "
                f"Underlying error: {e}"
            ) from e

        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                # Teams Web is a heavier SPA than OWA — give it a bit
                # more headroom on the initial navigation. The route's
                # hash fragment (#/activity) doesn't survive
                # domcontentloaded reliably; we let the redirect dance
                # settle then check the URL.
                await page.goto(
                    TEAMS_ACTIVITY_URL,
                    wait_until="domcontentloaded",
                    timeout=PAGE_LOAD_TIMEOUT_MS,
                )
            except Exception as e:  # noqa: BLE001
                # Don't fail the whole briefing if Teams navigation
                # times out — log and return empty so OWA-only path
                # still publishes a useful brief.
                logger.warning(
                    f"Teams navigation failed; skipping Teams section: {e}")
                return ""

            final_url = page.url
            if _url_looks_like_login(final_url):
                logger.info(
                    f"Teams scrape redirected to login: {final_url}")
                raise OutlookAuthExpired(
                    "Microsoft 365 session expired — sign in again.")

            # Same content-settle wait as OWA but with a longer
            # ceiling. Teams Web's React tree takes 5-15s to fully
            # mount the activity feed even on warm cache; networkidle
            # never settles because of continuous telemetry. v2.15.1
            # had the same shell-only extraction problem here.
            try:
                main = page.locator('[role="main"]').first
                main_present = await main.count() > 0
                locator = main if main_present else page.locator("body")
                text = await _wait_for_text_to_settle(
                    locator,
                    target_chars=TEAMS_TARGET_CHARS,
                    min_useful_chars=TEAMS_MIN_USEFUL_CHARS,
                    max_wait_sec=TEAMS_CONTENT_SETTLE_MAX_WAIT_SEC,
                    label="Teams",
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Couldn't read Teams text content; skipping: {e}")
                return ""

            text = (text or "").strip()
            if not text:
                # Empty result here is the cloud-sync-profile bug class
                # that bit OWA in v2.14.0 — but with the profile moved
                # to USER_DATA_DIR in v2.15.0 it shouldn't repro. Log
                # and return empty rather than failing the whole
                # briefing.
                logger.info(
                    "Teams returned no activity text; section omitted.")
                return ""

            # Defensive auth check by content — Teams' login interstitial
            # sometimes serves at teams.microsoft.com directly with a
            # "Sign in" UI. Surface as auth-expired so the UI's banner
            # fires; the OWA-side handler does the same.
            lowered = text.lower()[:500]
            if "sign in to your account" in lowered or \
                    "pick an account" in lowered or \
                    "stay signed in" in lowered:
                raise OutlookAuthExpired(
                    "Microsoft 365 sign-in page rendered for Teams.")

            if len(text.encode("utf-8")) > MAX_SCRAPE_TEXT_BYTES:
                logger.warning(
                    f"Teams text exceeded {MAX_SCRAPE_TEXT_BYTES} bytes; truncating")
                text = text.encode("utf-8")[:MAX_SCRAPE_TEXT_BYTES].decode(
                    "utf-8", errors="ignore")
            return text
        finally:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass


def format_for_briefing_parser(owa_text: str,
                                open_actions: Optional[list] = None,
                                teams_text: Optional[str] = None) -> str:
    """Stitch the scraped OWA + Teams text and the recorder's open
    action items into a single free-form blob that mimics the shape of
    a Copilot daily-brief paste. The existing summarizer prompt for
    ``parse_daily_briefing`` already handles loose, narrative-style
    input — we just give it labeled sections so the LLM finds the
    agenda + needs-response items cleanly.

    ``teams_text`` is optional; when present (v2.15.0+) it gets its own
    labeled section so the LLM can lift @mentions and missed-call
    notifications into the needs_response category. Empty/None Teams
    text is silently omitted (Teams scrape failures don't drop the
    whole brief — OWA-only output is still useful).

    Keeping this function pure (no I/O, no LLM) so it's trivially
    testable.
    """
    parts: list[str] = []
    parts.append("=== Today's Outlook Calendar ===")
    parts.append("")
    parts.append((owa_text or "").strip() or "(no events visible)")

    teams_clean = (teams_text or "").strip()
    if teams_clean:
        parts.append("")
        parts.append("=== Today's Teams Activity (mentions, replies, missed calls) ===")
        parts.append("")
        parts.append(teams_clean)

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
