"""
Follow-up email drafting — platform router.

Picks the right backend based on the OS:

    Windows → _follow_up_email_outlook.py  (Outlook COM via pywin32)
    macOS   → _follow_up_email_macos.py    (Mail.app + Outlook for Mac
                                             via AppleScript, EML fallback)
    other   → no-op stub

The two backends present the same `draft_follow_up_emails(svc, session_id,
tone)` entry point so server.py never has to branch on platform.
"""

from __future__ import annotations

import sys

from services.follow_up_owners import DraftResult
from utils.logger import get_logger

logger = get_logger(__name__)

UNSUPPORTED_PLATFORM = "unsupported_platform"


def draft_follow_up_emails(svc, session_id: str,
                           tone: str = "friendly-professional") -> DraftResult:
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
