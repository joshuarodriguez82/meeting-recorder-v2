"""
Follow-up email drafting — delivery router.

Two questions, in this order:

  1. **Is a desktop mail client allowed at all?** `calendar_source ==
     "extension"` says no — see below. Everything goes to
     `_follow_up_email_web.py`, on every platform.
  2. Otherwise, which OS is this?

         Windows → _follow_up_email_outlook.py  (Outlook COM via pywin32)
         macOS   → _follow_up_email_macos.py    (Mail.app + Outlook for
                                                  Mac via AppleScript,
                                                  EML fallback)
         other   → no-op stub

All three backends present the same `draft_follow_up_emails(svc,
session_id, tone)` entry point so server.py never has to branch on
platform or on the setting.

WHY THE SETTING GATE IS HERE AND NOT IN A BACKEND
-------------------------------------------------

`calendar_source = "extension"` is presented to the user as **"Never
contacts Outlook"**, and the calendar side has been taught to honour
that (services/calendar_service.py is the single choke point; a stray
`Dispatch("Outlook.Application")` is enough to re-trigger a locked-down
tenant's sign-in prompt, which is the whole reason the setting exists).

The drafting side had not. Field report 2026-08-19: a user who lives in
the Outlook PWA and cannot use classic desktop Outlook at all had set
"extension" for exactly that reason, then clicked *Draft follow-up
emails* — which called `Dispatch("Outlook.Application")`, launched
classic Outlook and filed ten drafts into a profile they never open.
macOS was the same defect in AppleScript: `_follow_up_email_macos`
drives Mail.app, then *Microsoft Outlook*, then writes `.eml` files.

So the gate sits ABOVE the platform branch, in the one function every
caller already goes through, rather than being re-implemented as an
early return inside each backend. Two implementations of "is a mail
client allowed?" is how the display path and the trigger path came to
disagree about the calendar; there is no reason to build that again
here.

Note this also gives the previously-unsupported platforms a working
path under "extension": the artifact is a URL, so nothing about it is
Windows- or macOS-specific.

READ-ONLY ON THE CALENDAR SETTING
---------------------------------
This module only ever *reads* `calendar_source`. It does not change what
any value means, what it defaults to, or how any calendar consumer
interprets it — `calendar_feed.merged_upcoming` / `merged_today`,
`auto_record_service` and the Record/Today views are untouched by this
file's existence.
"""

from __future__ import annotations

import sys

from services.follow_up_owners import DraftResult
from utils.logger import get_logger

logger = get_logger(__name__)

UNSUPPORTED_PLATFORM = "unsupported_platform"

# The one `calendar_source` value that forbids touching a mail client.
# "off" deliberately does NOT: it means "no calendar data", which says
# nothing about whether the user's Outlook works, and silently changing
# where their drafts go would be its own surprise.
NO_MAIL_CLIENT_SOURCE = "extension"


def _calendar_source() -> str:
    """Live-read `calendar_source`, same contract as
    `calendar_service._current_calendar_source`.

    Read fresh from disk on every call rather than off the service
    container: Settings.from_env's docstring explains why (Tauri spawn
    contexts can deliver a partial/stale process environment), and a
    setting whose entire job is "never contact Outlook" must not be able
    to answer from a cached value that predates the user changing it.

    A settings read that fails falls back to "auto" — the pre-existing
    behaviour — rather than crashing the drafting run.
    """
    try:
        from config.settings import Settings
        return (Settings.from_env().calendar_source or "auto").strip().lower()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not read calendar_source for drafting: {e}")
        return "auto"


def draft_follow_up_emails(svc, session_id: str,
                           tone: str = "friendly-professional") -> DraftResult:
    source = _calendar_source()
    if source == NO_MAIL_CLIENT_SOURCE:
        logger.info(
            "Follow-up drafts: calendar_source is 'extension' — no mail "
            "client will be contacted on any platform; producing Outlook "
            "Web compose links instead.")
        from services._follow_up_email_web import (
            draft_follow_up_emails as _impl,
        )
        return _impl(svc, session_id, tone=tone)
    if sys.platform.startswith("win"):
        from services._follow_up_email_outlook import (
            draft_follow_up_emails as _impl,
        )
        return _impl(svc, session_id, tone=tone)
    if sys.platform == "darwin":
        from services._follow_up_email_macos import (
            draft_follow_up_emails as _impl,
        )
        return _impl(svc, session_id, tone=tone)
    logger.warning(
        f"Follow-up email drafting is not supported on platform "
        f"{sys.platform!r}. Returning 0 drafts."
    )
    return DraftResult(
        created=0,
        state=UNSUPPORTED_PLATFORM,
        message=(
            f"Follow-up email drafting is not supported on "
            f"{sys.platform} — it needs Outlook on Windows or "
            f"Mail.app / Outlook on macOS."
        ),
    )
