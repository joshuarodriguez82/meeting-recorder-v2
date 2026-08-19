"""
Auto-drafted follow-up emails — Windows / Outlook COM backend.

Imported lazily by follow_up_email.py when running on Windows. Do NOT
import directly — use follow_up_email.draft_follow_up_emails() which
routes to this module or _follow_up_email_macos.py based on the OS.

Design choices:
- Creates DRAFTS, never sends. The SA reviews each one.
- Uses Claude Haiku to draft per-attendee body (personalised wording).
- Uses Outlook COM (win32com) to create the draft in the user's default
  Drafts folder.
- Silently skips attendees we can't resolve to an email address — we do
  a best-effort GAL (Global Address List) resolve for names from the
  transcript, and fall back to using their display name only if the
  resolve fails (user can still edit the To: field).
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional, Tuple

from core._precision import no_invented_precision
from services.follow_up_owners import DraftResult, build_draft_plan
from utils.com_worker import run_com
from utils.logger import get_logger

logger = get_logger(__name__)


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
        f"Their action items:\n{bullets}\n"
        # Full block. This is the ONLY artifact in the app that leaves
        # the user's machine addressed to someone else — a fabricated
        # deadline in a follow-up email is a commitment the recipient
        # now believes they made. The "4-8 sentences ... and thanking
        # them" instruction is itself pressure to pad, and padding an
        # email is done with specifics: a date, a count, a system name.
        + no_invented_precision()
        + "\n"
    )
    if decisions_md and decisions_md.strip().lower() != \
            "no decisions made in this meeting.":
        prompt += f"Decisions made (for context):\n{decisions_md[:1500]}\n\n"
    if summary_md:
        prompt += f"Meeting summary (for context):\n{summary_md[:1500]}\n"

    # Routed through the provider-agnostic `_chat` helper rather than a
    # raw client call. This used to be
    # `summarizer._client.messages.create(...)`, but `Summarizer` has no
    # `_client` — only `_anthropic_client` and `_openai_client` — so
    # drafting a follow-up raised AttributeError on every platform and
    # every provider. `_chat` also means this now works for
    # OpenAI-compatible providers, which the raw Anthropic call never
    # could. `_follow_up_email_macos.py` imports this same function, so
    # the fix covers both.
    text = (await summarizer._chat(
        prompt, max_tokens=600, timeout=45.0)).strip()

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
                            tone: str = "friendly-professional") -> DraftResult:
    """
    Create an Outlook draft for each attendee with assigned action items.
    Returns a DraftResult — the draft count plus WHY the count is what it
    is, so the caller can tell "no action items" from "action items we
    couldn't read" from "nobody individual owns anything".

    Owner attribution comes from services/follow_up_owners.py: the
    session's commitments sidecar when it has open, individually-owned
    commitments (a real `owner` field, alias-resolved), otherwise a
    tolerant re-parse of the `action_items` markdown.

    Runs synchronously — callers should invoke via asyncio.to_thread.

    Only the actual Outlook COM work (creating + saving the draft items)
    runs on the process's single COM worker thread (utils/com_worker.py)
    — the Claude drafting calls above it are plain async network I/O and
    have no business serializing behind Outlook. Nothing returned from
    the run_com() closure is a COM object; `created` is a plain int.
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

    def _create_drafts() -> int:
        import win32com.client  # Windows-only; imported lazily

        # Attach COM to the running Outlook instance (same pattern
        # calendar_service uses).
        try:
            outlook = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")

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
        return created

    created = run_com(_create_drafts, timeout=90.0)

    logger.info(
        f"Follow-up drafts: created {created} of {len(owners)} drafts "
        f"for session {session_id} (source={plan.source})"
    )
    return DraftResult.from_plan(plan, created=created)
