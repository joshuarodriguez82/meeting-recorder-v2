"""The calendar invite as ground truth for WHO WAS IN THE ROOM.

Speaker identification used to work from the transcript alone: the
model read "Thanks, Jane" and had to decide, with nothing else to go
on, whether the next turn belonged to a Jane and what her surname
might be. It could only ever produce what the transcript literally
carried — a bare first name — or invent the rest.

The invite already knows. `Session.attendees` is populated from the
calendar entry the recording was started from, and it carries either
display names (the Chrome-extension calendar source resolves names
only) or addresses (the local Outlook / EventKit paths can carry
both). Handing that list to the prompt as a CANDIDATE SET turns the
existing IDENTIFIERS clause of the no-invented-precision rule from a
general prohibition into something enforceable against real data:
there is now a specific, closed list of fuller forms a heard first
name is allowed to resolve to.

Three properties this module exists to hold:

  * **A prior, not a cage.** People join meetings they were never
    invited to. The roster raises confidence for the names on it; it
    never licenses dropping a speaker the transcript genuinely names.
    That rule lives in the prompt text below, not in code.

  * **Ambiguity is refused.** Two roster entries sharing a first name
    means the speaker stays unnamed. Speaker names flow into
    commitments, owner grouping and follow-up email recipients, so a
    name pinned to the wrong person does not stay a wrong label — it
    becomes a wrong To: field.

  * **No mangled guesses.** Deriving "Jane Doe" from
    `jane.doe@acme.example` is a split, not a guess. Deriving anything
    at all from `jdoe@acme.example` IS a guess: nothing distinguishes
    initial-plus-surname from a short given name (`jane@acme.example`
    has exactly the same shape), and getting it wrong produces a
    person who does not exist. Those addresses contribute nothing —
    see `display_name_from_email`.

Kept dependency-free on purpose, like core/_precision.py: this is pure
text, and importing it must not drag in the anthropic SDK or the
services layer.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

# ── Address parsing ─────────────────────────────────────────────────

# "Jane Doe <jane.doe@acme.example>" — the RFC-ish form some calendar
# sources hand back. The display half wins when it is non-empty.
_ANGLE_ADDR_RE = re.compile(r"^(?P<display>[^<>]*)<(?P<addr>[^<>]+)>$")

# Local parts split on dot and underscore only. A hyphen is left alone:
# it is far more often INSIDE one name part ("mary-jane", "doe-roe")
# than between two.
_LOCAL_SPLIT_RE = re.compile(r"[._]+")

# Trailing disambiguator digits on an otherwise fine token —
# "jane.doe2@" is a second Jane Doe, not a person called "Doe2".
_TRAILING_DIGITS_RE = re.compile(r"\d+$")

# A name part, once the trailing digits are gone: letters, with
# hyphens and apostrophes allowed inside. \w minus digits and
# underscore keeps non-ASCII names (diacritics) working.
_NAME_PART_RE = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*$", re.UNICODE)

# Tokens that mean the mailbox is a THING, not a person. A room or
# distribution mailbox that happens to split into two tidy tokens
# ("conference.room.a@") would otherwise become a plausible-looking
# attendee named Conference Room, and the whole point of the roster is
# that its entries are trustworthy.
_NON_PERSON_TOKENS = frozenset({
    "admin", "alerts", "all", "billing", "boardroom", "calendar",
    "conf", "conference", "contact", "desk", "distribution",
    "do-not-reply", "donotreply", "events", "finance", "group",
    "help", "helpdesk", "hr", "huddle", "info", "inbox", "legal",
    "mailbox", "mailer-daemon", "marketing", "meeting", "meetings",
    "no-reply", "noreply", "notification", "notifications", "office",
    "postmaster", "reception", "resource", "resources", "room",
    "rooms", "sales", "security", "service", "support", "team",
    "teams", "webmaster",
})


def _titlecase(token: str) -> str:
    """"mary-jane" -> "Mary-Jane", "o'roe" -> "O'Roe".

    Each alphabetic run is capitalised independently so the hyphen and
    apostrophe forms survive. A token that already carries capitals is
    re-cased the same way — local parts are not reliable about case
    ("Jane.DOE@"), so there is nothing worth preserving.
    """
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), token)


def display_name_from_email(address: str) -> str:
    """Derive a person's display name from an address, or "".

    Handled, with the reasoning for each:

      ``jane.doe@acme.example``        -> "Jane Doe"
      ``jane_doe@globex.example``      -> "Jane Doe"
      ``jane.q.doe@initech.example``   -> "Jane Doe"  (initial dropped)
      ``mary-jane.roe@globex.example`` -> "Mary-Jane Roe"
      ``jane.doe2@acme.example``       -> "Jane Doe"  (disambiguator)

    Deliberately NOT handled — these return "":

      ``jdoe@acme.example``   A single-token local part cannot be
      ``jane@acme.example``   split. "jdoe" is probably initial-plus-
                              surname and "jane" is probably a given
                              name, but they are the same shape, and no
                              rule tells them apart without a name
                              dictionary. Guessing yields "J. Doe" for
                              one and "J. Ane" for the other; being
                              wrong invents a person. A bare surname
                              ("Doe") is no better — it adds nothing
                              the transcript did not already have and
                              reads as a full name downstream.
      ``noreply@acme.example``      Not a person.
      ``conference.room.a@…``       Not a person, splits tidily.
      ``2fa.99@acme.example``       No alphabetic name part survives.

    An address that yields nothing contributes nothing to the roster —
    it does not appear in the prompt at all.
    """
    addr = (address or "").strip().strip("<>").strip()
    if "@" not in addr:
        return ""
    local, _, domain = addr.partition("@")
    if not domain.strip():
        return ""
    # Strip a "+tag" sub-address before splitting.
    local = local.split("+", 1)[0].strip().strip("\"'")
    if not local:
        return ""

    raw_tokens = [t for t in _LOCAL_SPLIT_RE.split(local) if t]
    if len(raw_tokens) < 2:
        # Single token: see the docstring. Refused, not guessed.
        return ""

    parts: List[str] = []
    for token in raw_tokens:
        token = _TRAILING_DIGITS_RE.sub("", token)
        if not token or not _NAME_PART_RE.match(token):
            return ""
        if token.lower() in _NON_PERSON_TOKENS:
            return ""
        if len(token) == 1:
            # A middle initial. Dropped rather than rendered: "Jane Q
            # Doe" matches a heard "Jane" no better than "Jane Doe"
            # does, and the bare letter reads as a name of its own.
            continue
        parts.append(_titlecase(token))

    if len(parts) < 2:
        # Everything but one part was an initial — "j.doe@" lands here,
        # and it is the single-token case wearing a dot.
        return ""
    return " ".join(parts)


def _name_from_entry(entry: str) -> str:
    """One attendee entry -> a display name, or "".

    Entries arrive in three shapes depending on the calendar source: a
    plain display name, a bare address, or ``Name <addr>``. A display
    name is passed through as written — including the surname-first
    ``Doe, Jane [EMEA]`` form Outlook produces, which the prompt tells
    the model to read and report in natural order rather than this
    module trying to re-order (a comma is not reliably a swap: "Roe,
    Pat Jr." is one person's whole name, and owner_service.py exists
    partly because splitting on that comma shreds it into two people).
    """
    raw = (entry or "").strip().strip("\"'").strip()
    if not raw:
        return ""

    m = _ANGLE_ADDR_RE.match(raw)
    if m:
        display = m.group("display").strip().strip("\"'").strip()
        if display and "@" not in display:
            return display
        return display_name_from_email(m.group("addr"))

    if "@" in raw:
        return display_name_from_email(raw)

    # A display name. Must carry at least one letter — a stray "-" or
    # a "(none)" style placeholder is not a person.
    if not re.search(r"[^\W\d_]", raw):
        return ""
    return raw


def roster_names(
    attendees: Optional[Sequence[str]] = None,
    organizer: str = "",
) -> List[str]:
    """The invite's people, as display names, de-duplicated.

    The organiser goes first when one is supplied: they are the person
    most likely to be both present and speaking. They are usually also
    in `attendees`, which the case-insensitive de-dupe collapses.

    Entries that yield no usable name are dropped silently — an empty
    result is the "no roster" case and must stay empty so the prompt
    degrades exactly to its pre-roster form.
    """
    out: List[str] = []
    seen = set()
    for entry in [organizer or "", *(attendees or [])]:
        name = _name_from_entry(entry if isinstance(entry, str) else "")
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


# ── The prompt fragment ─────────────────────────────────────────────

ROSTER_HEADER = (
    "\n\n=== INVITE ROSTER — who the calendar says was invited ===\n"
)

# Stated once, here, for the same reason the no-invented-precision rule
# is stated once in core/_precision.py: a paraphrase in a second
# builder is how the weaker wording ends up being the one that ships.
ROSTER_RULES = (
    "\nThese people were on the meeting invite, so they are the "
    "likeliest speakers. Use the roster as a strong prior, not a "
    "closed set:\n"
    "- PREFER THE ROSTER'S FULLER FORM. When the transcript names a "
    "speaker by first name only ('Thanks, Jane') and EXACTLY ONE "
    "roster entry contains that name, report that entry's full name "
    "for the speaker.\n"
    "- REFUSE AMBIGUITY. When two or more roster entries could be the "
    "name you heard, leave that speaker out of the result entirely. An "
    "unnamed speaker is always better than speech attributed to the "
    "wrong person.\n"
    "- THE ROSTER IS NOT THE ONLY WAY IN. Someone who is not on it may "
    "still be in the meeting. If the transcript identifies them by "
    "self-introduction or direct address, name them from the "
    "transcript exactly as it gives the name.\n"
    "- BEING INVITED IS NOT EVIDENCE OF SPEAKING. Never hand a roster "
    "name to a speaker the transcript does not identify, and never map "
    "two different speaker IDs to the same person.\n"
    "- A roster entry may be written surname-first with a region or "
    "org tag, e.g. 'Doe, Jane [EMEA]' or 'Roe, Pat (Globex)'. Report "
    "the person in natural order — 'Jane Doe' — and drop the tag.\n"
)


def roster_block(names: Sequence[str]) -> str:
    """The roster section of the speaker-identification prompt.

    Returns "" for an empty roster, and that emptiness is load-bearing:
    a session with no attendees must produce a BYTE-IDENTICAL prompt to
    the one this feature replaced, so nothing regresses for the
    sessions that were never started from a calendar entry. Pinned by
    test_speaker_roster.py.
    """
    if not names:
        return ""
    listed = "\n".join(f"- {n}" for n in names)
    return f"{ROSTER_HEADER}{listed}\n{ROSTER_RULES}"
