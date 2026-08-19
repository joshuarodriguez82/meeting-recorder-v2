"""
Follow-up emails as Outlook Web compose links — the no-mail-client path.

WHY THIS MODULE EXISTS
----------------------

`calendar_source = "extension"` is documented in the UI as:

    "Never contacts Outlook — the Record tab shows only what the Chrome
    extension has scraped from Outlook Web. Use this if Outlook keeps
    asking you to sign in."

Field report 2026-08-19: a user set it for exactly that reason — they
live in the Outlook PWA and cannot use classic desktop Outlook at all —
then clicked **Draft follow-up emails**. That path called
`win32com.client.Dispatch("Outlook.Application")`, which *launches*
classic desktop Outlook and files the drafts into whichever profile COM
happens to attach to. Ten drafts, in a client they do not use, in a
mailbox they never look at. The setting made a promise the drafting path
broke, one process launch after the calendar path had been taught not
to.

macOS had the same defect in a different dialect: `_follow_up_email_macos`
drives AppleScript at Mail.app, then at "Microsoft Outlook" — the second
of those is literally contacting Outlook — and falls back to writing
`.eml` files into ~/Downloads. Same shape: a user who set "extension"
because no desktop mail client works for them gets one driven anyway.

So the gate lives in `follow_up_email.py`'s router, ABOVE the platform
branch, and both platforms land here.

WHAT THIS PATH PRODUCES — AND WHAT IT DOES NOT
----------------------------------------------

An `https://outlook.office.com/mail/deeplink/compose?...` URL per
recipient. Opening one drops a prefilled compose window into the browser
session the user is already signed into: no COM, no AppleScript, no
auth, no profile guessing, and it is by construction the mailbox they
are looking at.

**A compose link is not a draft.** Nothing is written to any mailbox
here — not a Drafts folder, not a file on disk. Close the tab without
saving and the message is gone. Everything user-facing in this module
says that out loud (`compose_message`, and `DraftResult.artifact =
ARTIFACT_COMPOSE_LINK`), because "we produced something" rendered as
"your drafts are saved" is the exact class of overclaim
`follow_up_recipients.py` already exists to stop.

Deliberately NOT done here:

  * **We do not open the links.** Ten compose tabs firing at once is
    worse than the bug being fixed. The set goes back to the UI and the
    user opens them one at a time.
  * **No `.eml` fallback.** The macOS backend writes `.eml` files when
    no mail client answers, and that is right *there* — it is the last
    resort before producing nothing. Here it would be a third artifact
    kind with a third honesty burden (a file in ~/Downloads is not a
    draft either) offered to a user whose whole situation is "I have a
    working browser session and no working mail client". The browser
    session is the better artifact, so it is the only one.

URL LENGTH
----------
See `URL_BUDGET_CHARS`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Tuple
from urllib.parse import quote

# Prompt + owner attribution are shared, exactly as the macOS backend
# shares them: only DELIVERY differs between the three paths. Importing
# the Outlook backend does NOT touch COM — `import win32com.client` is
# inside its `_create_drafts` closure, never at module scope — and
# `test_follow_up_web_compose.py` pins that with a win32com stub that
# raises if anything reaches for it.
from services._follow_up_email_outlook import _compose_body
from services.follow_up_owners import (
    ARTIFACT_COMPOSE_LINK, DraftResult, build_draft_plan,
)
from services.follow_up_recipients import resolve_recipient
from utils.logger import get_logger

logger = get_logger(__name__)


# Outlook on the web's compose deeplink. Works for both the commercial
# (outlook.office.com) and consumer tenants; the commercial host is the
# right default for the tenant-locked users this path exists for, and it
# redirects rather than 404s for the other.
COMPOSE_ENDPOINT = "https://outlook.office.com/mail/deeplink/compose"


# ── The URL budget ───────────────────────────────────────────────────
#
# 2000 characters for the WHOLE URL, and everything about that number is
# a floor rather than a guess:
#
#   * Internet Explorer / legacy Edge capped a URL at 2083 characters,
#     and that limit is still enforced by a long tail of corporate
#     forward proxies, SSO gateways and link-rewriting mail filters —
#     which is precisely the population this path serves, since they are
#     here because their tenant is locked down.
#   * The deeplink does not land on outlook.office.com directly on a
#     cold session: it goes through a login/redirect chain, and each hop
#     carries the whole query string forward.
#   * A truncated URL does not fail loudly. It silently drops the tail
#     of the body, which is the failure mode this repo keeps paying for.
#
# So: 2000 for the URL, leaving ~80 characters of headroom under 2083.
# The `to` and `subject` parameters are never truncated — a message with
# a mangled subject or a mangled recipient is worse than a shortened
# one — so the body gets whatever is left, and if that is nothing the
# body simply does not go in the URL.
URL_BUDGET_CHARS = 2000

# Appended to the body that goes IN the URL when it had to be shortened.
# Visible in the compose window on purpose: the user must be able to see
# that what is in front of them is not the whole message. Silently
# sending three quarters of a follow-up is not an option.
TRUNCATION_NOTICE = (
    "\n\n[Shortened to fit this compose link — the full message is in "
    "Meeting Recorder; copy it from there before you send.]"
)

# How far back from the cut point we will look for a whitespace boundary
# rather than slicing a word in half. Purely cosmetic; if there is no
# boundary within this many characters we take the hard cut.
_WORD_BOUNDARY_LOOKBACK = 120


def _q(value: str) -> str:
    """Percent-encode one query-parameter value.

    `safe=""` is the whole point: with the default safe set, `&`, `#`,
    `+`, `/` and `?` survive into the query string and a subject like
    "Pricing & scope — next steps" quietly becomes two parameters, while
    a `+` in a body is read back as a space. Everything that is not an
    unreserved character gets encoded, newlines included (`%0A`), and
    non-ASCII goes out as UTF-8 percent-octets.

    `quote` (not `quote_plus`): space becomes `%20`, so a literal `+`
    can be `%2B` without the reader having to guess which convention
    produced it.
    """
    return quote(value or "", safe="", encoding="utf-8", errors="replace")


def _fit_encoded(text: str, budget_chars: int) -> str:
    """Longest prefix of `text` whose ENCODED length fits `budget_chars`.

    Measured on the encoded form, because that is what the budget is
    about: a newline costs 3 characters, an accented letter 6, an emoji
    up to 12. Counting source characters would blow the budget on
    exactly the bodies most likely to be near it.
    """
    if budget_chars <= 0:
        return ""
    used = 0
    cut = 0
    for i, ch in enumerate(text):
        # Percent-encoding is per-byte and context-free, so a character's
        # cost is the same in isolation as it is in the whole string.
        cost = len(_q(ch))
        if used + cost > budget_chars:
            break
        used += cost
        cut = i + 1
    if cut >= len(text):
        return text
    head = text[:cut]
    boundary = max(head.rfind(" "), head.rfind("\n"))
    if boundary >= len(head) - _WORD_BOUNDARY_LOOKBACK and boundary > 0:
        return head[:boundary]
    return head


def build_compose_url(
    to: str, subject: str, body: str,
    budget_chars: int = URL_BUDGET_CHARS,
) -> Tuple[str, bool]:
    """(url, truncated). `truncated` is True when the URL carries less
    than the whole body — in which case the URL's body ends with
    `TRUNCATION_NOTICE` so the user can see it in the compose window.

    Never raises and never returns an empty URL: an unaddressed
    recipient just gets an empty `to`, and a body that cannot fit at all
    gets left out entirely rather than mangled.
    """
    to = (to or "").strip()
    subject = subject or ""
    body = body or ""

    def _assemble(body_text: str) -> str:
        return (f"{COMPOSE_ENDPOINT}"
                f"?to={_q(to)}"
                f"&subject={_q(subject)}"
                f"&body={_q(body_text)}")

    full = _assemble(body)
    if len(full) <= budget_chars:
        return full, False

    # Room left for the body once the endpoint, `to` and `subject` have
    # taken their share. Those three are never trimmed.
    available = budget_chars - len(_assemble(""))
    notice_cost = len(_q(TRUNCATION_NOTICE))
    if available <= notice_cost:
        # Pathological: a subject or address long enough to eat the whole
        # budget. Ship the link with no body rather than no link — the
        # full body is in the UI either way, and the compose window still
        # opens against the right mailbox.
        logger.warning(
            "Follow-up compose link: subject/recipient alone fill the "
            "%d-character URL budget; sending the link with an empty body",
            budget_chars)
        return _assemble(""), True

    shortened = _fit_encoded(body, available - notice_cost).rstrip()
    return _assemble(shortened + TRUNCATION_NOTICE), True


@dataclass
class ComposeLink:
    """One recipient's prefilled compose window, not yet opened.

    `body` is always the FULL drafted text regardless of what fitted in
    `url` — it is what the UI's copy button hands over, and it is the
    reason truncation here is never a loss of the message.
    """

    owner: str
    display_name: str = ""
    address: str = ""
    subject: str = ""
    body: str = ""
    url: str = ""
    truncated: bool = False

    @property
    def addressed(self) -> bool:
        return bool(self.address)

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "display_name": self.display_name,
            "address": self.address,
            "subject": self.subject,
            "body": self.body,
            "url": self.url,
            "truncated": self.truncated,
            "addressed": self.addressed,
        }


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def compose_message(links: List[ComposeLink]) -> str:
    """The sentence the user reads. Its whole job is to not be mistaken
    for the saved-draft message in `follow_up_recipients.delivery_message`.

    Three facts, in this order, because that is their order of
    consequence: (1) nothing was saved anywhere, (2) how many still need
    an address, (3) how many open shortened.
    """
    n = len(links)
    if not n:
        return (
            "No compose links could be prepared — nothing was written to "
            "any mailbox and no draft exists."
        )

    parts = [
        f"{n} Outlook Web compose {_plural(n, 'link', 'links')} ready — "
        f"nothing has been saved to any mailbox and no {_plural(n, 'draft', 'drafts')} "
        f"{_plural(n, 'exists', 'exist')} yet."
    ]
    parts.append(
        f"Open {_plural(n, 'it', 'them one at a time')} to review in your "
        f"browser; {_plural(n, 'it', 'each')} becomes a draft only once you "
        f"save it in Outlook Web."
    )
    unaddressed = sum(1 for link in links if not link.addressed)
    if unaddressed:
        parts.append(
            f"{unaddressed} {_plural(unaddressed, 'opens', 'open')} with an "
            f"empty To: field — add the address before sending."
        )
    truncated = sum(1 for link in links if link.truncated)
    if truncated:
        parts.append(
            f"{truncated} {_plural(truncated, 'message is', 'messages are')} "
            f"too long for a link and {_plural(truncated, 'opens', 'open')} "
            f"shortened — copy the full text from here first."
        )
    return " ".join(parts)


def draft_follow_up_emails(svc, session_id: str,
                           tone: str = "friendly-professional") -> DraftResult:
    """Build one Outlook Web compose link per owner with outstanding work.

    Same inputs and same drafting as the two mail-client backends —
    `build_draft_plan` for owner attribution, `_compose_body` for the
    wording (including the `no_invented_precision()` block),
    `resolve_recipient` for the To: field. Only delivery differs, and
    delivery here writes nothing anywhere.

    Runs synchronously; callers invoke via `asyncio.to_thread`. There is
    no COM worker and no subprocess: this path touches no mail client on
    any platform, which is the entire reason it exists.
    """
    session_data = svc.session_svc.load(session_id)
    if not session_data:
        raise FileNotFoundError(f"Session not found: {session_id}")

    plan = build_draft_plan(svc, session_id, session_data)
    if not plan.ok:
        result = DraftResult.from_plan(plan)
        result.artifact = ARTIFACT_COMPOSE_LINK
        return result
    owners = plan.owners

    meeting_title = session_data.get("display_name") or f"Session {session_id}"
    decisions_md = session_data.get("decisions") or ""
    summary_md = session_data.get("summary") or ""
    attendees = list(session_data.get("attendees") or [])

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
        logger.exception(f"Follow-up compose links: Claude batch failed ({e})")
        raise

    links: List[ComposeLink] = []
    failed = 0
    for owner, _tasks, res in raw:
        if isinstance(res, BaseException):
            failed += 1
            logger.warning(
                f"Follow-up compose links: Claude failed for one owner: {res}")
            continue
        subject, body = res
        # No directory to look a name up in — there is no GAL over HTTP
        # and reaching for one would mean contacting the very thing this
        # path exists to avoid. `resolver=None` still picks the richest
        # known form of the name for the To: field (Outlook Web
        # autocompletes a two-token name where it can do nothing with a
        # bare first name) and reports itself honestly as unaddressed.
        recipient = resolve_recipient(
            owner, resolver=None,
            alias_index=plan.alias_index, attendees=attendees,
        )
        # An owner label that IS an address (rare, but it happens when
        # the action item was written that way) is the only way this
        # path can be addressed at all.
        address = recipient.address or (
            recipient.to_field if "@" in recipient.to_field else "")
        url, truncated = build_compose_url(address, subject, body)
        links.append(ComposeLink(
            owner=owner,
            display_name=recipient.display_name or owner,
            address=address,
            subject=subject,
            body=body,
            url=url,
            truncated=truncated,
        ))

    addressed = sum(1 for link in links if link.addressed)
    truncated_count = sum(1 for link in links if link.truncated)
    logger.info(
        f"Follow-up compose links: prepared {len(links)} of {len(owners)} "
        f"for session {session_id} (source={plan.source}, "
        f"addressed={addressed}, unaddressed={len(links) - addressed}, "
        f"truncated={truncated_count}, drafting_failures={failed}) — "
        f"no mail client was contacted and nothing was saved"
    )

    result = DraftResult.from_plan(plan, created=len(links))
    result.addressed = addressed
    result.unaddressed = len(links) - addressed
    # Nothing was written, so there is no folder and no account to name.
    # Left empty on purpose — see the class docstring in
    # follow_up_owners.DraftResult.
    result.location = ""
    result.account = ""
    result.artifact = ARTIFACT_COMPOSE_LINK
    result.compose_links = [link.to_dict() for link in links]
    result.message = compose_message(links)
    return result
