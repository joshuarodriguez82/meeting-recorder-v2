"""The calendar invite as the candidate set for speaker identification.

Speaker ID used to read the transcript and nothing else, so a meeting
where everyone says "Jane" and nobody says "Doe" produced the speaker
label "Jane" — and a first name is exactly what
services/follow_up_recipients.py cannot turn into an address. The
invite already carries the fuller form.

Two artifacts are under test here:

  * `core/speaker_roster.py` — the pure functions. What an address
    yields, and just as importantly what it deliberately does NOT
    yield: a form that cannot be split without guessing contributes
    nothing rather than a mangled name.

  * THE PROMPT. No model is ever called; `_chat` is monkeypatched to
    capture the prompt string, which is the thing that actually ships.
    Same pattern as test_no_invented_precision.py and
    test_prep_brief_documents.py.

The load-bearing assertion is the last one: an empty roster must give
back the byte-for-byte prompt that existed before this feature, so
sessions never started from a calendar entry cannot regress. Same
check `test_prep_brief_documents.py::test_omitting_document_notes_
entirely_matches_passing_empty` makes for the documents kwarg.

Every name and address below is fictional and every domain is a
reserved `.example` — see AGENTS.md.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# summarizer.py imports the anthropic SDK at module load and
# config.settings imports python-dotenv; neither is in the lightweight
# test env. Same stub pattern as test_no_invented_precision.py.
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core.speaker_roster import (  # noqa: E402
    display_name_from_email,
    roster_block,
    roster_names,
)
from core.summarizer import Summarizer  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parents[1]

TRANSCRIPT = (
    "[00:00 → 00:09] SPEAKER_00: Alright, let's get going. Jane, can "
    "you walk us through the Globex routing map?\n"
    "[00:09 → 00:24] SPEAKER_01: Sure. We landed on six intents and "
    "the overflow queue still needs an owner.\n"
    "[00:24 → 00:31] SPEAKER_00: Thanks, Jane. I'll take the overflow "
    "queue.\n"
)


def _run(coro):
    return asyncio.run(coro)


def _capturing(reply: str = '{"SPEAKER_01": "Jane Doe"}'):
    s = Summarizer(api_key="x", model="claude-haiku-4-5",
                   provider="anthropic")
    prompts: list[str] = []

    async def _fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return reply

    s._chat = _fake_chat  # type: ignore[assignment]
    return s, prompts


def _prompt_for(**kwargs) -> str:
    s, prompts = _capturing()
    _run(s.identify_speakers(TRANSCRIPT, **kwargs))
    return prompts[0]


# ── The point of the feature ────────────────────────────────────────


def test_a_first_name_in_the_transcript_gets_the_rosters_fuller_form():
    """The whole reason the roster exists. The transcript says "Jane"
    twice and never says a surname; the invite says jane.doe@. The
    prompt has to carry the derived full name AND the instruction that
    a single matching roster entry is what a bare first name resolves
    to — otherwise the model has the data and no licence to use it."""
    prompt = _prompt_for(attendees=["jane.doe@acme.example"])
    assert "=== INVITE ROSTER" in prompt
    assert "\n- Jane Doe\n" in prompt
    assert "PREFER THE ROSTER'S FULLER FORM." in prompt
    assert ("EXACTLY ONE roster entry contains that name, report that "
            "entry's full name") in prompt
    # And the roster reaches the prompt as a CANDIDATE SET, not as a
    # replacement for the two original signals.
    assert "1. SELF-INTRODUCTION" in prompt
    assert "2. DIRECT ADDRESS" in prompt


def test_display_name_attendees_reach_the_roster_verbatim():
    """The Chrome-extension calendar source resolves names only, no
    addresses — including Outlook's surname-first form. Those are not
    re-ordered in Python (a comma is not reliably a swap: "Roe, Pat
    Jr." is one person's whole name, which is the regression
    owner_service.py's no-comma-split rule exists to prevent). The
    prompt tells the model to read the form instead."""
    prompt = _prompt_for(attendees=["Doe, Jane [EMEA]", "Pat Roe"])
    assert "\n- Doe, Jane [EMEA]\n" in prompt
    assert "\n- Pat Roe\n" in prompt
    assert "'Doe, Jane [EMEA]'" in prompt
    assert "Report the person in natural order" in prompt


# ── Address → display name ──────────────────────────────────────────


@pytest.mark.parametrize("address,expected", [
    # first.last@ — the dominant corporate form.
    ("jane.doe@acme.example", "Jane Doe"),
    # first_last@ — the same thing with the other separator.
    ("jane_doe@globex.example", "Jane Doe"),
    # first.m.last@ — the middle initial is dropped, not rendered. A
    # bare letter reads as a name of its own and matches a heard
    # "Jane" no better than "Jane Doe" does.
    ("jane.q.doe@initech.example", "Jane Doe"),
    # Mixed separators, and a three-part name that is all real parts.
    ("mary-jane.roe@northwind.example", "Mary-Jane Roe"),
    ("ana.van.noh@globex.example", "Ana Van Noh"),
    # Casing in the local part is not information — it is re-cased.
    ("JANE.DOE@acme.example", "Jane Doe"),
    ("Jane.Doe@acme.example", "Jane Doe"),
    # A trailing disambiguator digit means "the second Jane Doe", not
    # a person called Doe2.
    ("jane.doe2@acme.example", "Jane Doe"),
    # +tag sub-addressing is stripped before the split.
    ("jane.doe+meetings@acme.example", "Jane Doe"),
    # Apostrophes and diacritics survive intact.
    ("pat.o-roe@initech.example", "Pat O-Roe"),
    ("zoe.døe@northwind.example", "Zoe Døe"),
    # Surrounding angle brackets / whitespace from a calendar field.
    ("  <jane.doe@acme.example>  ", "Jane Doe"),
])
def test_supported_email_forms_derive_a_display_name(address, expected):
    assert display_name_from_email(address) == expected


@pytest.mark.parametrize("address", [
    # flast@ — the form this module deliberately refuses. "jdoe" is
    # almost certainly J-something Doe, but the ONLY thing that makes
    # it different from a short given name is a name dictionary we do
    # not have: "jane@" below has exactly the same shape. Splitting
    # gives "J. Doe" for one and "J. Ane" for the other, and being
    # wrong invents a person who was never in the meeting. A bare
    # surname is no better — it adds nothing the transcript did not
    # already have and reads downstream as a full name.
    "jdoe@acme.example",
    "jroe@globex.example",
    # A bare given name. Same shape, same refusal.
    "jane@acme.example",
    # The dotted spelling of the same problem: one initial, one name.
    "j.doe@acme.example",
    "j.q.doe@acme.example",
    # Not people at all — room, distribution and system mailboxes,
    # including the ones that split into two tidy tokens and would
    # otherwise become a plausible-looking attendee.
    "noreply@acme.example",
    "no-reply@acme.example",
    "conference.room.a@acme.example",
    "meeting.room@globex.example",
    "support.team@initech.example",
    # No alphabetic name part survives.
    "2fa.99@acme.example",
    "x.y@acme.example",
    # Not an address, or not a usable one.
    "",
    "   ",
    "not-an-address",
    "jane.doe@",
    "@acme.example",
    None,
])
def test_addresses_with_no_usable_name_contribute_nothing(address):
    assert display_name_from_email(address) == ""


def test_an_unusable_address_is_absent_from_the_roster_entirely():
    """Not rendered as a blank bullet, not rendered as the raw address:
    absent. A roster the model cannot trust is worse than no roster."""
    names = roster_names([
        "jane.doe@acme.example",
        "jdoe@globex.example",
        "noreply@initech.example",
        "",
        "   ",
    ])
    assert names == ["Jane Doe"]
    block = roster_block(names)
    assert "jdoe" not in block
    assert "noreply" not in block
    assert "@" not in block.split("These people were")[0]


# ── Ambiguity, and the roster's limits ──────────────────────────────


def test_two_roster_entries_sharing_a_first_name_are_both_kept_and_refused():
    """Disambiguation is NOT done in Python by picking one — both
    entries stay on the roster and the prompt instructs a refusal.
    Speaker names flow into commitments, owner grouping and follow-up
    recipients, so a name pinned to the wrong person does not stay a
    wrong label; it becomes a wrong To: field. An unnamed speaker
    costs the user one rename."""
    prompt = _prompt_for(attendees=["jane.doe@acme.example",
                                    "jane.roe@globex.example"])
    assert "\n- Jane Doe\n" in prompt
    assert "\n- Jane Roe\n" in prompt
    assert "REFUSE AMBIGUITY." in prompt
    assert ("leave that speaker out of the result entirely") in prompt
    assert ("An unnamed speaker is always better than speech "
            "attributed to the wrong person.") in prompt


def test_a_speaker_who_is_not_on_the_roster_can_still_be_named():
    """The roster is a strong prior, not a cage. Someone who joins an
    invite they were never on still exists, and the transcript can
    still name them by the two original signals."""
    prompt = _prompt_for(attendees=["jane.doe@acme.example"])
    assert "THE ROSTER IS NOT THE ONLY WAY IN." in prompt
    assert ("Someone who is not on it may still be in the meeting."
            in prompt)
    assert ("name them from the transcript exactly as it gives the "
            "name.") in prompt


def test_being_invited_is_not_evidence_of_having_spoken():
    """The failure the roster itself introduces: handing every
    diarized label a roster name because the names were right there.
    That is the IDENTIFIERS clause of no-invented-precision applied to
    the new input, so it is stated against the new input too."""
    prompt = _prompt_for(attendees=["jane.doe@acme.example",
                                    "pat.roe@globex.example",
                                    "sam.noh@initech.example"])
    assert "BEING INVITED IS NOT EVIDENCE OF SPEAKING." in prompt
    assert ("Never hand a roster name to a speaker the transcript "
            "does not identify") in prompt
    assert "never map two different speaker IDs to the same person" in prompt
    # The rule the roster is meant to make ENFORCEABLE is still there.
    assert "=== NO INVENTED PRECISION" in prompt


# ── Roster assembly ─────────────────────────────────────────────────


def test_the_organiser_leads_the_roster_and_dedupes_against_attendees():
    """The organiser is the likeliest person to be both present and
    speaking, so they go first. Outlook usually repeats them in the
    attendee list; the case-insensitive de-dupe collapses that rather
    than listing one person twice and inviting a split attribution."""
    names = roster_names(
        attendees=["pat.roe@globex.example", "JANE.DOE@acme.example"],
        organizer="jane.doe@acme.example",
    )
    assert names == ["Jane Doe", "Pat Roe"]


def test_roster_handles_the_name_plus_angle_address_form():
    """Some calendar sources hand back `Name <addr>`. The display half
    wins when it carries a name; otherwise the address is derived."""
    names = roster_names([
        "Jane Doe <jane.doe@acme.example>",
        "<pat.roe@globex.example>",
        '"Noh, Sam [APAC]" <s.noh@initech.example>',
    ])
    assert names == ["Jane Doe", "Pat Roe", "Noh, Sam [APAC]"]


def test_non_string_and_placeholder_entries_are_dropped():
    """Attendee lists come off JSON that predates any schema. A
    non-string or a punctuation placeholder must not become a bullet."""
    assert roster_names(["-", "(", "  ", None, 42, "Jane Doe"]) == ["Jane Doe"]


def test_roster_block_is_empty_for_an_empty_roster():
    assert roster_block([]) == ""
    assert roster_block(roster_names([])) == ""
    assert roster_block(roster_names(["jdoe@acme.example"])) == ""


# ── The regression guard ────────────────────────────────────────────


def test_empty_roster_is_byte_identical_to_the_pre_roster_prompt():
    """THE load-bearing assertion. Most recordings are not started
    from a calendar entry, so most sessions have no attendees — and
    for those the prompt must be the one that shipped before this
    feature, byte for byte. Every way of saying "no roster" has to
    reach the same string: the kwargs omitted entirely (the old call
    signature), passed empty, passed None, and passed a list whose
    entries all yield nothing."""
    baseline = _prompt_for()
    assert "INVITE ROSTER" not in baseline
    assert "roster" not in baseline.lower()

    assert _prompt_for(attendees=[]) == baseline
    assert _prompt_for(attendees=None) == baseline
    assert _prompt_for(attendees=[], organizer="") == baseline
    assert _prompt_for(attendees=["", "   "]) == baseline
    # An invite that carried only unsplittable addresses is the same
    # case as no invite at all — no half-roster leaks in.
    assert _prompt_for(attendees=["jdoe@acme.example",
                                  "noreply@globex.example"]) == baseline


def test_the_roster_block_is_additive_and_leaves_the_rest_untouched():
    """The roster is inserted, not woven in: removing the block from a
    roster prompt yields the no-roster prompt exactly. Nothing else in
    the builder is conditional on attendees."""
    baseline = _prompt_for()
    withroster = _prompt_for(attendees=["jane.doe@acme.example"])
    block = roster_block(["Jane Doe"])
    assert block in withroster
    assert withroster.replace(block, "") == baseline
