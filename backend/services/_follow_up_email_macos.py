"""
Auto-drafted follow-up emails — macOS backend.

Drives Mail.app (preferred) or Microsoft Outlook for Mac via AppleScript.
We pick whichever is the user's default mail client; if neither is
installed/responsive we fall back to writing .eml files into the user's
Downloads folder so the user can drag them into their email app.

The Claude drafting logic is imported from _follow_up_email_outlook.py
and the owner attribution from services/follow_up_owners.py, so we don't
duplicate prompt or parser code — only the OS-side draft creation
differs between platforms.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from utils.logger import get_logger

# Reuse the prompt + rendering helpers from the Outlook backend — they
# are pure-Python and OS-agnostic. Owner attribution is shared one level
# further out, in follow_up_owners.py, so neither platform carries its
# own copy of the parser.
from services._follow_up_email_outlook import (
    _compose_body,
    _body_to_html,
)
from services.follow_up_owners import DraftResult, build_draft_plan
from services.follow_up_recipients import (
    DraftDelivery, draft_result, resolve_recipient,
)

logger = get_logger(__name__)

# Used when the per-backend location below can't be established.
FALLBACK_LOCATION = "your mail app's Drafts folder"


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


def _create_mail_draft(to_addr: str, subject: str,
                       html_body: str) -> Optional[str]:
    """Create a draft in Apple Mail. Returns the new message's id on
    success, None on failure.

    The id IS the verification: AppleScript returning an id means Mail
    filed the message, which is the macOS equivalent of the `EntryID`
    check the Outlook backend does after `Save()`. A truthy return from
    `osascript` that carried no id used to count as a created draft."""
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
    return out.strip() if out and out.strip() else None


def _create_outlook_mac_draft(to_addr: str, subject: str,
                              html_body: str) -> Optional[str]:
    """Create a draft in Microsoft Outlook for Mac. Returns the new
    message's id on success, None on failure — same verification
    contract as `_create_mail_draft`."""
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
    return out.strip() if out and out.strip() else None


def _default_account_address(app: str) -> str:
    """Best-effort "which account did this land in?" for the AppleScript
    backends.

    The macOS side had the same gap the Outlook side did: we told the
    user drafts existed without knowing which account's Drafts mailbox
    they went to. Neither app exposes the account of a freshly-made
    outgoing message, so we read the default/first enabled account —
    which is where an unsent message is filed. Errors and permission
    denials return "", and the message then says the folder could not be
    confirmed rather than naming one we did not check.
    """
    if app == "Mail":
        script = (
            'tell application "Mail"\n'
            '  try\n'
            '    set a to first account whose enabled is true\n'
            '    return item 1 of (get email addresses of a)\n'
            '  on error\n'
            '    return ""\n'
            '  end try\n'
            'end tell'
        )
    else:
        script = (
            'tell application "Microsoft Outlook"\n'
            '  try\n'
            '    return email address of default account\n'
            '  on error\n'
            '    return ""\n'
            '  end try\n'
            'end tell'
        )
    return (_osascript(script, timeout_s=8) or "").strip()


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
        # Verify it is on disk before we claim it — the .eml path's
        # equivalent of Outlook's post-Save() EntryID check.
        if not path.exists():
            logger.warning(f"EML fallback for {owner} is not on disk after write")
            return None
        return str(path)
    except Exception as e:
        logger.warning(f"EML fallback write failed: {e}")
        return None


def draft_follow_up_emails(svc, session_id: str,
                           tone: str = "friendly-professional") -> DraftResult:
    """
    Create a draft for each attendee with assigned action items. Mac path:
    routes to Mail.app, then Outlook for Mac, then .eml fallback in
    ~/Downloads.

    Returns a DraftResult: the number of drafts actually created (across
    whichever backends ended up being used) plus the state explaining a
    zero, how many of them carry a real address, and which app/folder
    they went to. Owner attribution is the shared commitments-first /
    markdown-fallback plan from services/follow_up_owners.py; recipient
    resolution and the delivery tally are the shared
    services/follow_up_recipients.py.
    """
    session_data = svc.session_svc.load(session_id)
    if not session_data:
        raise FileNotFoundError(f"Session not found: {session_id}")

    plan = build_draft_plan(svc, session_id, session_data)
    if not plan.ok:
        return DraftResult.from_plan(plan)
    owners = plan.owners

    meeting_title = session_data.get("display_name") or f"Session {session_id}"
    decisions_md = session_data.get("decisions") or ""
    summary_md = session_data.get("summary") or ""
    attendees = list(session_data.get("attendees") or [])

    # Pick a draft creator. Prefer Mail.app (always present on macOS);
    # use Outlook for Mac only if Mail.app is missing or scripting is
    # blocked. We try Mail's AppleScript dictionary on the first call —
    # if that returns false from a smoke probe, switch to Outlook.
    probe = _osascript('tell application "Mail" to get version', timeout_s=5)
    if probe:
        creator = _create_mail_draft
        creator_name = "Mail.app"
        # Where the drafts will be, said in terms the user can act on.
        creator_location = "the Drafts mailbox in Mail"
        creator_account = _default_account_address("Mail")
    else:
        probe = _osascript('tell application "Microsoft Outlook" to get version', timeout_s=5)
        if probe:
            creator = _create_outlook_mac_draft
            creator_name = "Outlook for Mac"
            creator_location = "the Drafts folder in Outlook for Mac"
            creator_account = _default_account_address("Outlook")
        else:
            creator = None
            creator_name = "EML fallback (~/Downloads)"
            creator_location = ""
            creator_account = ""
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

    delivery = DraftDelivery()
    for owner, tasks, (subject, body) in drafted_bodies:
        # There is no GAL on Mac, so no `resolver` — but the richest
        # known form of the name still matters: it is what goes in the
        # To: header, and Mail/Outlook autocomplete a two-token name from
        # Contacts where they can do nothing with a bare first name. The
        # resolution reports itself unaddressed, which is the truth: we
        # never established an address.
        recipient = resolve_recipient(
            owner, resolver=None,
            alias_index=plan.alias_index, attendees=attendees,
        )
        to_addr = recipient.to_field
        html = _body_to_html(body)
        landed_in = ""
        account = ""
        created_id: Optional[str] = None
        if creator is not None:
            try:
                created_id = creator(to_addr, subject, html)
            except Exception as e:
                logger.warning(f"Could not create draft for {owner} via {creator_name}: {e}")
            if created_id:
                landed_in, account = creator_location, creator_account
        if not created_id:
            path = _write_eml_fallback(to_addr, subject, html, owner)
            if path:
                logger.info(f"Wrote .eml fallback for {owner}: {path}")
                created_id = path
                landed_in = str(Path(path).parent)
                account = ""
        if not created_id:
            # Nothing confirmed it persisted — not counted as created.
            delivery.note_unverified()
            logger.warning(
                f"Follow-up draft for {owner} was not confirmed saved by "
                f"{creator_name} or the .eml fallback; not counting it")
            continue
        # `to_addr` is a name unless the owner label was literally an
        # address, so this is almost always False on macOS — and saying
        # so is the point.
        delivery.note_created(
            addressed="@" in to_addr,
            location=landed_in,
            account=account,
        )
        logger.info(f"Follow-up draft created for {owner} in "
                    f"{landed_in or 'an unconfirmed location'}")

    logger.info(
        f"Follow-up drafts: created {delivery.created} of {len(owners)} "
        f"drafts for session {session_id} (source={plan.source}, "
        f"addressed={delivery.addressed}, "
        f"unaddressed={delivery.unaddressed}, "
        f"unverified={delivery.unverified}, "
        f"location={delivery.location or 'unconfirmed'})"
    )
    return draft_result(plan, delivery, FALLBACK_LOCATION)
