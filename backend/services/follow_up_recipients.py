"""
Who a follow-up draft is actually addressed to — and where it landed.

`services/follow_up_owners.py` answers *which people* have outstanding
work. This module answers the two questions that come after it, and it
lives here for the same reason the parser does: both backends
(`_follow_up_email_outlook.py`, `_follow_up_email_macos.py`) need it and
neither should carry its own copy.

    1. Given an owner label like "Alex", what is the richest form of
       that person's name we can prove we know, so a directory lookup
       has something to match on?
    2. Of the drafts we saved, how many are actually addressed, how many
       are not, and *where did they go*?

Why (1) exists
--------------

Field repro 2026-08-19: a session produced ten Outlook drafts and
reported "created 10 of 10". Nine of them had no email address. The
Windows backend resolves a recipient by handing the owner label to
Outlook's `CreateRecipient(name).Resolve()` — a Global Address List
lookup — and the owner labels coming out of action items are usually
*bare first names*, which a corporate GAL almost never resolves. The one
that did resolve had an unusually distinctive first name.

The app frequently knows a fuller form of the same person than the
action item happened to use: `session.attendees` carries names from the
calendar invite, and `owner_service.AliasIndex` carries variants the
user has explicitly confirmed are one identity. A two-token name
resolves against a GAL far more often than one token, so we try the
richest known form first and fall back through shorter ones, ending at
the label we started with.

The mis-resolution guard
------------------------

Addressing an email to the wrong person is worse than leaving the To:
field for the user to fill in, so a fuller form is only ever used when
we can *prove* it belongs to the same person:

* **Alias groups** are user-confirmed. `AliasIndex.by_member` only
  groups keys the user merged by hand (`suggest_groups` is advisory and
  never auto-applied — see `owner_service`'s module docstring). If the
  owner's normalised key is in a group, every other name in that group
  is the same human by construction, so all of them are fair candidates.

* **Attendees** are not confirmed by anybody, so the test is structural
  and strict: the owner's significant tokens must be a proper *subset*
  of the attendee's tokens. "Alex" ⊂ "Alex Doe" qualifies; "Alex" and
  "Alexandra Roe" do not, because token subset is not substring match.

* **Ambiguity is fatal, not a tie-break.** If two attendees both extend
  the owner label — "Alex Doe" and "Alex Roe" for an action item that
  said "Alex" — we use *neither*. Picking the alphabetically-first one
  would silently address somebody's commitments to their colleague.
  Falling back to the bare label just means the user fills in the To:
  field, which the result now tells them to do.

Why (2) exists
--------------

Same field report, second half: the drafts were not in the Drafts folder
the user was looking at. `mail.Save()` takes no folder argument, so an
item lands in the default Drafts folder of whichever Outlook profile COM
attached to — which is not necessarily the mailbox in front of the user
(theirs was the Outlook PWA; COM drives classic desktop Outlook). The
app reported "created 10 of 10" while knowing neither the folder nor the
account. Claiming success without establishing where the artifact went
is the same defect as rendering an unaddressed draft as a finished one,
so `DraftDelivery` carries the read-back and `delivery_message()` puts
it in front of the user.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional

from services.follow_up_owners import DraftPlan, DraftResult
from services.owner_service import AliasIndex, normalize_owner
from utils.logger import get_logger

logger = get_logger(__name__)


# Tokens that carry no identifying weight on their own. A person whose
# owner label is literally "Jr." must not match every attendee with a
# suffix, so these are dropped before the subset test. Kept local rather
# than imported from follow_up_owners so this module stays a leaf of
# that one (it imports DraftPlan/DraftResult from there) and the two
# never form a cycle.
_WEAK_TOKENS = {
    "van", "von", "de", "der", "den", "del", "della", "di", "da", "du",
    "la", "le", "bin", "ibn", "al", "mc", "mac", "ter", "ten",
    "jr", "sr", "ii", "iii", "iv", "phd", "md",
}

_TOKEN_SPLIT_RE = re.compile(r"[\s,]+")
_TOKEN_TRIM = ".,;:()[]'\""


def _name_tokens(name: str) -> FrozenSet[str]:
    """Lowercase token set for `name`, punctuation trimmed.

    Goes through `normalize_owner` first so a trailing "(Acme)" org
    suffix and stray punctuation are gone before we split — the same
    normalisation the owner filter and the alias store use, so the keys
    line up.
    """
    key, _display = normalize_owner(name or "")
    toks = {
        t.strip(_TOKEN_TRIM) for t in _TOKEN_SPLIT_RE.split(key)
    }
    return frozenset(t for t in toks if t)


def _significant_tokens(name: str) -> FrozenSet[str]:
    return frozenset(_name_tokens(name) - _WEAK_TOKENS)


def _prettify(display: str) -> str:
    """Title-case a name ONLY when it carries no capitals at all.

    Alias-group members are stored as normalised (lowercased) keys, so
    the richest form of a name can arrive as "samantha noh" and would
    otherwise go straight into a To: field like that. A name with any
    existing capital is left exactly as written — "Ana van der Noh" and
    "McRoe" must survive untouched, and directory lookups are
    case-insensitive either way, so this is presentation only.
    """
    if not display or any(ch.isupper() for ch in display):
        return display
    return " ".join(w[:1].upper() + w[1:] for w in display.split(" "))


def _richness(name: str) -> tuple:
    """Sort key for "how much has a directory got to match on?".

    Token count first (two-token names resolve where one-token names do
    not), then raw length as the tie-break.
    """
    toks = _name_tokens(name)
    key, _display = normalize_owner(name or "")
    return (len(toks), len(key))


# ── Candidate forms of one person's name ─────────────────────────────


def alias_group_names(
    owner: str, alias_index: Optional[AliasIndex] = None,
) -> List[str]:
    """Every name the user has confirmed is the same identity as `owner`.

    Empty when `owner` is not in any confirmed group — which is the
    common case, and the point: an unconfirmed lookalike contributes
    nothing here.
    """
    if not alias_index:
        return []
    key, _display = normalize_owner(owner or "")
    if not key:
        return []
    alias = alias_index.by_member.get(key)
    if alias is None:
        # `build_draft_plan` runs owners through `resolve_owners()`, so
        # the label reaching us is usually a group's *canonical* display
        # name — which is not itself required to be one of the member
        # keys. Match on it too, otherwise the richest form is invisible
        # for exactly the sessions that have aliases configured.
        for candidate in alias_index.by_member.values():
            if normalize_owner(candidate.canonical)[0] == key:
                alias = candidate
                break
    if alias is None:
        return []
    return [alias.canonical, *alias.members]


def attendee_match(
    owner: str, attendees: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """The one attendee entry that is an unambiguously fuller form of
    `owner`, or None.

    None when nothing matches, when the only matches are no fuller than
    the label we already have, and — deliberately — when more than one
    attendee matches. See "The mis-resolution guard" above: two people
    called Alex means we address neither of them by guess.
    """
    owner_toks = _significant_tokens(owner)
    if not owner_toks:
        return None
    hits: Dict[str, str] = {}
    for entry in attendees or []:
        entry_toks = _name_tokens(entry)
        if not entry_toks or not owner_toks <= entry_toks:
            continue
        if len(entry_toks) <= len(_name_tokens(owner)):
            continue  # same shape as what we already have; nothing gained
        key, display = normalize_owner(entry)
        if key:
            hits.setdefault(key, display)
    if len(hits) == 1:
        return next(iter(hits.values()))
    if len(hits) > 1:
        # Count only — attendee names are meeting content and do not go
        # to the log. The count is what makes the ambiguity diagnosable.
        logger.info(
            f"Follow-up drafts: {len(hits)} attendees are fuller forms of "
            f"one owner label; too ambiguous to pick, using the label as "
            f"written")
    return None


def candidate_names(
    owner: str,
    alias_index: Optional[AliasIndex] = None,
    attendees: Optional[Iterable[str]] = None,
) -> List[str]:
    """Forms of `owner`'s name to try against a directory, richest first.

    Always contains `owner` itself, so the last resort is the label we
    were given rather than nothing.
    """
    owner = (owner or "").strip()
    if not owner:
        return []
    forms: Dict[str, str] = {}

    def _add(name: str) -> None:
        key, display = normalize_owner(name or "")
        if key and key not in forms:
            forms[key] = _prettify(display)

    _add(owner)
    for name in alias_group_names(owner, alias_index):
        _add(name)
    fuller = attendee_match(owner, attendees)
    if fuller:
        _add(fuller)

    return sorted(
        forms.values(),
        key=lambda n: (-_richness(n)[0], -_richness(n)[1], n.lower()),
    )


@dataclass
class RecipientResolution:
    """Outcome of trying to turn an owner label into a mailbox."""

    owner: str
    display_name: str = ""
    address: Optional[str] = None
    # Which candidate form produced the address. Empty when unresolved.
    resolved_from: str = ""
    forms_tried: int = 0

    @property
    def addressed(self) -> bool:
        return bool(self.address)

    @property
    def to_field(self) -> str:
        """What to put in To:. The address when we have one, otherwise
        the richest known form of the name — an unaddressed draft is
        still useful, and a full name gives the user's mail client an
        autocomplete to work with where a bare first name does not."""
        return self.address or self.display_name or self.owner


def resolve_recipient(
    owner: str,
    resolver: Optional[Callable[[str], Optional[str]]] = None,
    alias_index: Optional[AliasIndex] = None,
    attendees: Optional[Iterable[str]] = None,
) -> RecipientResolution:
    """Resolve `owner` to an email address, richest name form first.

    `resolver` is the platform's directory lookup — the Outlook GAL on
    Windows. Pass None where there is no directory (macOS): the richest
    form is still chosen for the To: field, and the result honestly
    reports itself as unaddressed rather than implying a lookup happened.
    """
    forms = candidate_names(owner, alias_index, attendees)
    resolution = RecipientResolution(
        owner=owner, display_name=forms[0] if forms else (owner or "").strip())
    if resolver is None:
        return resolution
    for form in forms:
        resolution.forms_tried += 1
        try:
            address = resolver(form)
        except Exception as e:
            logger.debug(f"Recipient resolve raised on a candidate form: {e}")
            address = None
        if address:
            resolution.address = address
            resolution.resolved_from = form
            break
    return resolution


# ── Where the drafts went ────────────────────────────────────────────


@dataclass
class DraftDelivery:
    """Running tally of what a drafting run actually produced.

    `created` counts only drafts we *verified* persisted. Anything that
    failed verification lands in `unverified` and is deliberately not
    counted as created — "we asked the mail client to save it" is not
    the same claim as "it is in your Drafts folder", and conflating the
    two is how ten drafts came to be reported that the user could not
    find.
    """

    created: int = 0
    addressed: int = 0
    unaddressed: int = 0
    unverified: int = 0
    _locations: Counter = field(default_factory=Counter)
    _accounts: Counter = field(default_factory=Counter)

    def note_created(self, addressed: bool, location: str = "",
                     account: str = "") -> None:
        self.created += 1
        if addressed:
            self.addressed += 1
        else:
            self.unaddressed += 1
        if location:
            self._locations[location] += 1
        if account:
            self._accounts[account] += 1

    def note_unverified(self) -> None:
        self.unverified += 1

    @property
    def location(self) -> str:
        """The folder the drafts landed in. Empty when we could not read
        it back — which the message then says out loud instead of
        inventing a plausible-sounding folder name."""
        return self._locations.most_common(1)[0][0] if self._locations else ""

    @property
    def account(self) -> str:
        return self._accounts.most_common(1)[0][0] if self._accounts else ""


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def delivery_message(delivery: DraftDelivery, fallback_location: str) -> str:
    """One sentence telling the user where the drafts are, how many are
    ready, and how many are not.

    Both facts are load-bearing. People expect a compose window to open
    (the code calls `.Save()`, never `.Display()`, on purpose — ten
    windows is worse), so nothing on screen tells them where to look;
    and a draft with a name where an address should be looks finished
    until they hit Send.
    """
    where = delivery.location or fallback_location
    if delivery.account:
        where = f"{where} ({delivery.account})"
    if not delivery.location:
        where = f"{where} — exact folder could not be confirmed"

    created = delivery.created
    parts = [
        f"{created} {_plural(created, 'draft', 'drafts')} saved to {where}"
    ]
    if delivery.unaddressed:
        n = delivery.unaddressed
        parts.append(
            f"{n} {_plural(n, 'needs', 'need')} an email address before you "
            f"can send {_plural(n, 'it', 'them')}"
        )
    elif created:
        parts.append("all of them addressed and ready to review")
    message = " — ".join(parts) + "."
    if delivery.unverified:
        n = delivery.unverified
        message += (
            f" {n} more {_plural(n, 'draft', 'drafts')} could not be "
            f"confirmed as saved and {_plural(n, 'is', 'are')} not counted "
            f"above."
        )
    return message


def draft_result(plan: DraftPlan, delivery: DraftDelivery,
                 fallback_location: str) -> DraftResult:
    """Fold a `DraftDelivery` into the `DraftResult` the API returns."""
    result = DraftResult.from_plan(plan, created=delivery.created)
    result.addressed = delivery.addressed
    result.unaddressed = delivery.unaddressed
    result.unverified = delivery.unverified
    result.location = delivery.location
    result.account = delivery.account
    result.message = delivery_message(delivery, fallback_location)
    return result
