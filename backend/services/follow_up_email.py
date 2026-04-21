"""
Auto-drafted follow-up emails.

For a processed session, generate a per-attendee follow-up email in
Outlook Drafts. The draft includes their personal action items, key
decisions from the meeting, and a short friendly thanks — so the user
only has to review + hit Send.

Design choices:
- Creates DRAFTS, never sends. The SA reviews each one.
- Uses Claude Haiku to draft per-attendee body (personalised wording).
- Uses Outlook COM (win32com) to create the draft in the user's default
  Drafts folder. The app already requires Outlook for calendar access,
  so this has no new dependencies.
- Silently skips attendees we can't resolve to an email address — we do
  a best-effort GAL (Global Address List) resolve for names from the
  transcript, and fall back to using their display name only if the
  resolve fails (user can still edit the To: field).
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


# Match lines like:  - [ ] **John Smith**: Send the SOW draft by Friday
_ACTION_ITEM_RE = re.compile(
    r"^\s*-\s*\[\s*[xX ]?\s*\]\s*\*\*\[?([^\]*]+?)\]?\*\*\s*:\s*(.+)$"
)


def _parse_action_items_by_owner(action_items_md: str) -> Dict[str, List[str]]:
    """
    Group checkbox-style action items by owner. Returns a dict keyed by owner
    display name (as it appears in the markdown) → list of action descriptions.
    """
    out: Dict[str, List[str]] = {}
    if not action_items_md:
        return out
    for line in action_items_md.splitlines():
        m = _ACTION_ITEM_RE.match(line)
        if not m:
            continue
        owner = m.group(1).strip()
        desc = m.group(2).strip()
        if not owner or not desc:
            continue
        # Skip generic "Team" / "All" / "Everyone" — we can't resolve a single email
        if owner.lower() in {"team", "all", "everyone", "group", "tbd", "unknown"}:
            continue
        out.setdefault(owner, []).append(desc)
    return out


def _resolve_email(outlook_ns, name: str) -> Optional[str]:
    """Best-effort GAL lookup for `name` → SMTP address, or None."""
    if not name:
        return None
    try:
        recipient = outlook_ns.CreateRecipient(name)
        if not recipient.Resolve():
            return None
        addr = recipient.AddressEntry
        # Exchange user → pull SMTP from the GAL entry
        try:
            exchange_user = addr.GetExchangeUser()
            if exchange_user and exchange_user.PrimarySmtpAddress:
                return exchange_user.PrimarySmtpAddress
        except Exception:
            pass
        # Fallback — Address may be the SMTP already (external contact)
        raw = getattr(addr, "Address", "") or ""
        if "@" in raw:
            return raw
    except Exception as e:
        logger.debug(f"GAL resolve failed for {name!r}: {e}")
    return None


async def _compose_body(
    summarizer, meeting_title: str, owner: str, tasks: List[str],
    decisions_md: str, summary_md: str, tone: str,
) -> Tuple[str, str]:
    """Ask Claude for (subject, html_body) tailored to this attendee."""
    bullets = "\n".join(f"- {t}" for t in tasks)
    prompt = (
        f"Write a {tone} follow-up email from me to {owner} after a meeting "
        f"titled '{meeting_title}'. Output EXACTLY this structure, no extras:\n\n"
        f"SUBJECT: <subject line, short, actionable>\n"
        f"BODY:\n"
        f"<email body in plain text, 4-8 sentences, mentioning their specific "
        f"action items as a short bulleted list, and thanking them. Do not "
        f"sign off with a name — the sender's signature is auto-appended.>\n\n"
        f"Their action items:\n{bullets}\n\n"
    )
    if decisions_md and decisions_md.strip().lower() != \
            "no decisions made in this meeting.":
        prompt += f"Decisions made (for context):\n{decisions_md[:1500]}\n\n"
    if summary_md:
        prompt += f"Meeting summary (for context):\n{summary_md[:1500]}\n"

    msg = await asyncio.wait_for(
        summarizer._client.messages.create(
            model=summarizer._model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        ),
        timeout=45.0,
    )
    text = msg.content[0].text.strip()

    subject = ""
    body = text
    m = re.search(r"^SUBJECT:\s*(.+?)\s*$", text, re.MULTILINE)
    if m:
        subject = m.group(1).strip()
    m = re.search(r"^BODY:\s*\n(.+)$", text, re.DOTALL | re.MULTILINE)
    if m:
        body = m.group(1).strip()
    if not subject:
        subject = f"Follow-up — {meeting_title}"
    return subject, body


def _body_to_html(body: str) -> str:
    """Very small text→html conversion so Outlook renders paragraphs + bullets."""
    lines = body.splitlines()
    html: list[str] = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{stripped[2:].strip()}</li>")
        else:
            if in_list:
                html.append("</ul>")
                in_list = False
            if stripped:
                html.append(f"<p>{stripped}</p>")
            else:
                html.append("<br>")
    if in_list:
        html.append("</ul>")
    return "\n".join(html)


def draft_follow_up_emails(svc, session_id: str,
                            tone: str = "friendly-professional") -> int:
    """
    Create an Outlook draft for each attendee with assigned action items.
    Returns the number of drafts actually created.

    Runs synchronously — callers should invoke via asyncio.to_thread.
    """
    import win32com.client  # Windows-only; imported lazily

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

    # Attach COM to the running Outlook instance (same pattern calendar_service uses)
    try:
        outlook = win32com.client.GetActiveObject("Outlook.Application")
    except Exception:
        outlook = win32com.client.Dispatch("Outlook.Application")
    ns = outlook.GetNamespace("MAPI")

    meeting_title = session_data.get("display_name") or f"Session {session_id}"
    decisions_md = session_data.get("decisions") or ""
    summary_md = session_data.get("summary") or ""

    # Run Claude drafting calls concurrently (bounded by asyncio.gather)
    async def _gather():
        results = await asyncio.gather(*[
            _compose_body(
                svc.summarizer, meeting_title, owner, tasks,
                decisions_md, summary_md, tone,
            )
            for owner, tasks in owners.items()
        ], return_exceptions=True)
        return list(zip(owners.keys(), owners.values(), results))

    drafted_bodies: list[tuple[str, list[str], Tuple[str, str]]] = []
    try:
        loop = asyncio.new_event_loop()
        try:
            raw = loop.run_until_complete(_gather())
        finally:
            loop.close()
    except Exception as e:
        logger.exception(f"Follow-up drafts: Claude batch failed ({e})")
        raise

    for owner, tasks, res in raw:
        if isinstance(res, BaseException):
            logger.warning(f"Follow-up drafts: Claude failed for {owner}: {res}")
            continue
        drafted_bodies.append((owner, tasks, res))

    created = 0
    for owner, tasks, (subject, body) in drafted_bodies:
        try:
            email_addr = _resolve_email(ns, owner)
            mail = outlook.CreateItem(0)  # 0 = olMailItem
            if email_addr:
                mail.To = email_addr
            else:
                # Couldn't resolve — put the name in the To field so SA sees who
                mail.To = owner
            mail.Subject = subject
            mail.HTMLBody = _body_to_html(body)
            # Save to Drafts (not Send)
            mail.Save()
            created += 1
            logger.info(
                f"Follow-up draft created for {owner} "
                f"({email_addr or 'unresolved'})"
            )
        except Exception as e:
            logger.warning(f"Could not create draft for {owner}: {e}")

    logger.info(
        f"Follow-up drafts: created {created} of {len(owners)} drafts "
        f"for session {session_id}"
    )
    return created
