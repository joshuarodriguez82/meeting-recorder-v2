"""
Auto-drafted follow-up emails — macOS backend.

Drives Mail.app (preferred) or Microsoft Outlook for Mac via AppleScript.
We pick whichever is the user's default mail client; if neither is
installed/responsive we fall back to writing .eml files into the user's
Downloads folder so the user can drag them into their email app.

The Claude drafting logic is imported from _follow_up_email_outlook.py
so we don't duplicate prompt + parser code — only the OS-side draft
creation differs between platforms.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from utils.logger import get_logger

# Reuse the prompt + parsing helpers from the Outlook backend — they are
# pure-Python and OS-agnostic.
from services._follow_up_email_outlook import (
    _ACTION_ITEM_RE,                    # noqa: F401  re-used for tests
    _parse_action_items_by_owner,
    _compose_body,
    _body_to_html,
)

logger = get_logger(__name__)


def _applescript_quote(s: str) -> str:
    """Escape a Python string for safe interpolation into an AppleScript
    string literal. AppleScript only needs `\\` and `"` escaped."""
    return s.replace("\\", "\\\\").replace("\"", "\\\"")


def _osascript(script: str, timeout_s: float = 15.0) -> Optional[str]:
    """Run an AppleScript snippet via /usr/bin/osascript. Returns stdout
    on success or None on any failure (timeout, app not running, etc.)."""
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout_s,
        )
        if proc.returncode != 0:
            logger.debug(f"osascript rc={proc.returncode} stderr={proc.stderr.strip()}")
            return None
        return proc.stdout.strip()
    except Exception as e:
        logger.debug(f"osascript raised: {e}")
        return None


def _have_app(bundle_or_name: str) -> bool:
    """Best-effort check that the named app exists. Uses AppleScript's
    `application id` lookup or a Spotlight bundle search."""
    out = _osascript(
        f'try\n  tell application "Finder" to get application file id "{bundle_or_name}"\n  return "yes"\non error\n  return "no"\nend try'
    )
    if out and out.strip().lower() == "yes":
        return True
    # Fallback: name-based check
    out = _osascript(
        f'try\n  tell application "System Events" to get bundle identifier of process "{bundle_or_name}"\n  return "yes"\non error\n  return "no"\nend try'
    )
    return bool(out and out.strip().lower() == "yes")


def _create_mail_draft(to_addr: str, subject: str, html_body: str) -> bool:
    """Create a draft in Apple Mail. Returns True on success."""
    # Mail.app's AppleScript supports `make new outgoing message` with
    # `content` as plain text. To preserve our HTML body we set the
    # message's content to a stripped plain-text version, then set
    # `html content` via the property — but the latter isn't part of the
    # standard Mail terminology pre-Sonoma. Safer approach: convert HTML
    # to a Markdown-ish plain text since Mail will reflow paragraphs.
    plain = re.sub(r"<[^>]+>", "", html_body)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    script = f'''
        tell application "Mail"
            set newMsg to make new outgoing message with properties {{subject:"{_applescript_quote(subject)}", content:"{_applescript_quote(plain)}", visible:false}}
            tell newMsg
                make new to recipient at end of to recipients with properties {{address:"{_applescript_quote(to_addr)}"}}
                save
            end tell
            return id of newMsg as string
        end tell
    '''
    out = _osascript(script, timeout_s=20)
    return out is not None and len(out) > 0


def _create_outlook_mac_draft(to_addr: str, subject: str, html_body: str) -> bool:
    """Create a draft in Microsoft Outlook for Mac. Returns True on success."""
    script = f'''
        tell application "Microsoft Outlook"
            set newMsg to make new outgoing message with properties {{subject:"{_applescript_quote(subject)}", content:"{_applescript_quote(html_body)}"}}
            tell newMsg
                make new recipient at end of to recipients with properties {{email address:{{address:"{_applescript_quote(to_addr)}"}}}}
                save
            end tell
            return id of newMsg as string
        end tell
    '''
    out = _osascript(script, timeout_s=20)
    return out is not None and len(out) > 0


def _write_eml_fallback(to_addr: str, subject: str, html_body: str,
                       owner: str) -> Optional[str]:
    """Write an .eml file to ~/Downloads when no mail client is reachable.
    Returns the path written, or None on failure. The user can double-click
    the .eml to open it in their default mail app and finish sending."""
    try:
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        safe_owner = "".join(c if c.isalnum() else "_" for c in owner)[:32]
        path = downloads / f"follow-up-{safe_owner}.eml"
        eml = (
            f"To: {to_addr}\n"
            f"Subject: {subject}\n"
            f"MIME-Version: 1.0\n"
            f"Content-Type: text/html; charset=UTF-8\n"
            f"\n"
            f"{html_body}"
        )
        path.write_text(eml, encoding="utf-8")
        return str(path)
    except Exception as e:
        logger.warning(f"EML fallback write failed: {e}")
        return None


def draft_follow_up_emails(svc, session_id: str,
                           tone: str = "friendly-professional") -> int:
    """
    Create a draft for each attendee with assigned action items. Mac path:
    routes to Mail.app, then Outlook for Mac, then .eml fallback in
    ~/Downloads.

    Returns the number of drafts actually created (across whichever
    backends ended up being used).
    """
    session_data = svc.session_svc.load(session_id)
    if not session_data:
        raise FileNotFoundError(f"Session not found: {session_id}")

    action_items_md = session_data.get("action_items") or ""
    if not action_items_md:
        logger.info(f"Follow-up drafts: session {session_id} has no action_items, nothing to draft")
        return 0

    owners = _parse_action_items_by_owner(action_items_md)
    if not owners:
        logger.info(f"Follow-up drafts: no owner-attributed action items in session {session_id}")
        return 0

    meeting_title = session_data.get("display_name") or f"Session {session_id}"
    decisions_md = session_data.get("decisions") or ""
    summary_md = session_data.get("summary") or ""

    # Pick a draft creator. Prefer Mail.app (always present on macOS);
    # use Outlook for Mac only if Mail.app is missing or scripting is
    # blocked. We try Mail's AppleScript dictionary on the first call —
    # if that returns false from a smoke probe, switch to Outlook.
    probe = _osascript('tell application "Mail" to get version', timeout_s=5)
    if probe:
        creator = _create_mail_draft
        creator_name = "Mail.app"
    else:
        probe = _osascript('tell application "Microsoft Outlook" to get version', timeout_s=5)
        if probe:
            creator = _create_outlook_mac_draft
            creator_name = "Outlook for Mac"
        else:
            creator = None
            creator_name = "EML fallback (~/Downloads)"
    logger.info(f"Follow-up drafts: using {creator_name}")

    # Run Claude drafting calls concurrently
    async def _gather():
        results = await asyncio.gather(*[
            _compose_body(
                svc.summarizer, meeting_title, owner, tasks,
                decisions_md, summary_md, tone,
            )
            for owner, tasks in owners.items()
        ], return_exceptions=True)
        return list(zip(owners.keys(), owners.values(), results))

    try:
        loop = asyncio.new_event_loop()
        try:
            raw = loop.run_until_complete(_gather())
        finally:
            loop.close()
    except Exception as e:
        logger.exception(f"Follow-up drafts: Claude batch failed ({e})")
        raise

    drafted_bodies: list[tuple[str, list[str], Tuple[str, str]]] = []
    for owner, tasks, res in raw:
        if isinstance(res, BaseException):
            logger.warning(f"Follow-up drafts: Claude failed for {owner}: {res}")
            continue
        drafted_bodies.append((owner, tasks, res))

    created = 0
    for owner, tasks, (subject, body) in drafted_bodies:
        # We don't have a GAL on Mac. Use the owner's display name as the
        # To: header — the user can edit it before sending. Mail.app shows
        # an autocomplete from Contacts as soon as they tab out.
        to_addr = owner
        html = _body_to_html(body)
        ok = False
        if creator is not None:
            try:
                ok = creator(to_addr, subject, html)
            except Exception as e:
                logger.warning(f"Could not create draft for {owner} via {creator_name}: {e}")
        if not ok:
            path = _write_eml_fallback(to_addr, subject, html, owner)
            if path:
                logger.info(f"Wrote .eml fallback for {owner}: {path}")
                ok = True
        if ok:
            created += 1
            logger.info(f"Follow-up draft created for {owner}")

    logger.info(
        f"Follow-up drafts: created {created} of {len(owners)} drafts "
        f"for session {session_id}"
    )
    return created
