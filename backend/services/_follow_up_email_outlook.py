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
- Never skips an attendee we can't resolve to an email address: the body
  is useful either way and the user can fill in the To: field. What we
  do NOT do any more is report such a draft as finished — see
  `services/follow_up_recipients.py` for the resolution ladder and for
  the delivery read-back that tells the user which folder and which
  account the drafts actually landed in.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from core._precision import no_invented_precision
from services.follow_up_owners import DraftResult, build_draft_plan
from services.follow_up_recipients import (
    DraftDelivery, draft_result, resolve_recipient,
)
from utils.com_worker import run_com
from utils.logger import get_logger

logger = get_logger(__name__)

# Used in the user-facing message when the folder read-back below comes
# back empty. Deliberately vague — naming a folder we did not verify is
# the mistake this whole module is being corrected for.
FALLBACK_LOCATION = "your Outlook Drafts folder"


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


# ── Where did the draft actually land? ───────────────────────────────
#
# `mail.Save()` takes no folder argument: the item goes to the default
# Drafts folder of whichever profile/store COM attached to. On a machine
# with more than one account — or where the user lives in the Outlook
# PWA and COM is driving classic desktop Outlook against a different
# profile — that is not the Drafts folder they are looking at, and the
# app used to report "created 10 of 10" with no idea where "created"
# meant. Every getter below is individually guarded: a missing read-back
# degrades to "we could not confirm the folder", never to an exception
# and never to a guess.


def _safe_str(obj, attr: str) -> str:
    try:
        return str(getattr(obj, attr, "") or "")
    except Exception:
        return ""


def _safe_attr(obj, attr: str):
    """`getattr` that survives a COM property raising, not just missing."""
    try:
        return getattr(obj, attr, None)
    except Exception:
        return None


def _store_account(outlook_ns, store) -> str:
    """SMTP address of the account that owns `store`.

    Outlook exposes no "which account is this folder in" property, so we
    match the folder's Store against each Account's DeliveryStore by
    StoreID — the one identifier that is stable across both. Falls back
    to the store's display name (often the mailbox address anyway) and
    finally to "", which the message reports honestly.
    """
    if store is None:
        return ""
    store_id = _safe_str(store, "StoreID")
    if store_id:
        try:
            for account in outlook_ns.Accounts:
                delivery = _safe_attr(account, "DeliveryStore")
                if delivery is not None and \
                        _safe_str(delivery, "StoreID") == store_id:
                    smtp = _safe_str(account, "SmtpAddress")
                    if smtp:
                        return smtp
        except Exception as e:
            logger.debug(f"Could not enumerate Outlook accounts: {e}")
    return _safe_str(store, "DisplayName")


def _draft_location(outlook_ns, mail) -> Tuple[str, str, str]:
    """(folder name, full folder path, owning account) for a saved item.

    `mail.Parent` is the MAPIFolder the item was filed into — read AFTER
    Save(), so it reflects where Outlook actually put it rather than
    where we assumed it would go.
    """
    try:
        folder = mail.Parent
    except Exception as e:
        logger.debug(f"Could not read the saved draft's parent folder: {e}")
        return "", "", ""
    if folder is None:
        return "", "", ""
    name = _safe_str(folder, "Name")
    path = _safe_str(folder, "FolderPath")
    try:
        store = folder.Store
    except Exception:
        store = None
    return name, path, _store_account(outlook_ns, store)


def _account_count(outlook_ns) -> int:
    try:
        return int(outlook_ns.Accounts.Count)
    except Exception:
        return 0


@dataclass
class SavedDraft:
    """A draft we have *verified* is in a folder."""

    entry_id: str
    folder_name: str = ""
    folder_path: str = ""
    account: str = ""

    @property
    def location(self) -> str:
        return self.folder_name or self.folder_path


def save_draft(outlook, outlook_ns, to_field: str, subject: str,
               html_body: str) -> Optional[SavedDraft]:
    """Create one draft and confirm it persisted. None if it did not.

    `Save()` on a MailItem is not a promise. An item that actually
    landed in a folder has an `EntryID`; one that did not, does not. The
    caller treats None as "not created" rather than counting it, because
    the alternative is what produced a "created 10 of 10" for drafts the
    user could not find.
    """
    mail = outlook.CreateItem(0)  # 0 = olMailItem
    mail.To = to_field
    mail.Subject = subject
    mail.HTMLBody = html_body
    # Save to Drafts, never Send — and never Display(), which would open
    # one window per owner.
    mail.Save()
    entry_id = _safe_str(mail, "EntryID")
    if not entry_id:
        return None
    folder_name, folder_path, account = _draft_location(outlook_ns, mail)
    return SavedDraft(entry_id=entry_id, folder_name=folder_name,
                      folder_path=folder_path, account=account)


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
    couldn't read" from "nobody individual owns anything" — and, for a
    run that did produce drafts, how many carry a real address and which
    folder/account they went to.

    Owner attribution comes from services/follow_up_owners.py: the
    session's commitments sidecar when it has open, individually-owned
    commitments (a real `owner` field, alias-resolved), otherwise a
    tolerant re-parse of the `action_items` markdown.

    Runs synchronously — callers should invoke via asyncio.to_thread.

    Only the actual Outlook COM work (creating + saving the draft items)
    runs on the process's single COM worker thread (utils/com_worker.py)
    — the Claude drafting calls above it are plain async network I/O and
    have no business serializing behind Outlook. Nothing returned from
    the run_com() closure is a COM object: `DraftDelivery` holds ints
    and strings that were already copied out of COM inside the closure.
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
    # Names only — `session.attendees` is a List[str] and there are no
    # email addresses anywhere in the app. Used purely to widen a bare
    # first name before it goes to the GAL.
    attendees = list(session_data.get("attendees") or [])

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

    def _create_drafts() -> DraftDelivery:
        import win32com.client  # Windows-only; imported lazily

        # Attach COM to the running Outlook instance (same pattern
        # calendar_service uses).
        try:
            outlook = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")

        # We deliberately do NOT pick a store to save into. Outlook gives
        # us no reliable way to know which of several accounts a meeting's
        # follow-ups belong in — the owners are names, not mailboxes, and
        # the one address that resolved in the field report was on a
        # different domain from the user's own mailbox, so "the account
        # whose domain matches" would have picked wrong. Guessing a store
        # would move drafts somewhere the user is even less likely to look.
        # So: keep the default profile's Drafts folder, and report exactly
        # where that turned out to be (below) instead of assuming.
        accounts = _account_count(ns)
        if accounts > 1:
            logger.info(
                f"Follow-up drafts: Outlook COM reports {accounts} accounts. "
                f"Drafts go to the default profile's Drafts folder; the "
                f"folder and account actually used are read back per item "
                f"and reported to the user.")

        delivery = DraftDelivery()
        for owner, tasks, (subject, body) in drafted_bodies:
            try:
                # Richest known form of this person's name first — a bare
                # first name is what a GAL is worst at. See
                # services/follow_up_recipients.py.
                recipient = resolve_recipient(
                    owner,
                    resolver=lambda name: _resolve_email(ns, name),
                    alias_index=plan.alias_index,
                    attendees=attendees,
                )
                # Address when we have one; otherwise the fullest name we
                # know, so the user has something to autocomplete from.
                saved = save_draft(outlook, ns, recipient.to_field,
                                   subject, _body_to_html(body))
                if saved is None:
                    delivery.note_unverified()
                    logger.warning(
                        f"Follow-up draft for {owner} has no EntryID after "
                        f"Save() — it did not persist; not counting it")
                    continue

                delivery.note_created(
                    addressed=recipient.addressed,
                    location=saved.location,
                    account=saved.account,
                )
                logger.info(
                    f"Follow-up draft created for {owner} "
                    f"({recipient.address or 'unresolved — name only'}) "
                    f"in {saved.folder_path or saved.folder_name or 'unknown folder'}"
                    + (f" [{saved.account}]" if saved.account else "")
                )
            except Exception as e:
                logger.warning(f"Could not create draft for {owner}: {e}")
        return delivery

    delivery = run_com(_create_drafts, timeout=90.0)

    logger.info(
        f"Follow-up drafts: created {delivery.created} of {len(owners)} "
        f"drafts for session {session_id} (source={plan.source}, "
        f"addressed={delivery.addressed}, "
        f"unaddressed={delivery.unaddressed}, "
        f"unverified={delivery.unverified}, "
        f"location={delivery.location or 'unconfirmed'})"
    )
    return draft_result(plan, delivery, FALLBACK_LOCATION)
