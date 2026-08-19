"""
Owner attribution for follow-up email drafts.

Field repro 2026-08-19: a 43-minute meeting that plainly assigned work to
several named people produced "No owner-attributed action items to draft
from — Claude didn't attribute any items to a specific person". The
session HAD action items; the drafter's single regex only accepted
`- [ ] **[Owner]**: task` and could not read the shape the model actually
emitted. Something unparseable rendered as something absent.

These tests pin down the fix on three axes:

  1. Source preference — commitments (structured `owner`, alias-resolved)
     beat re-parsing generated markdown, and the markdown is still there
     as a fallback for the sessions that have no commitments.
  2. Tolerance — the format variants models actually produce all parse,
     and a description containing a colon still does NOT become an owner.
  3. The three empty states stay distinct, because conflating the middle
     one into the third IS the bug.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.commitments_service import Commitment, CommitmentsService
from services.follow_up_owners import (
    GENERIC_OWNERS_ONLY,
    NO_ACTION_ITEMS,
    READY,
    UNREADABLE_FORMAT,
    build_draft_plan,
    is_generic_owner,
    looks_like_person,
    owners_from_commitments,
    parse_action_items_by_owner,
    _shape,
)
from services.owner_service import OwnerAliasStore
from services.session_service import SessionService


# ── Helpers ──────────────────────────────────────────────────────────


def _owners(md: str) -> dict:
    return parse_action_items_by_owner(md).by_owner


def _commitment(session_id: str, owner: str, description: str,
                status: str = "awaiting", due: str = "") -> Commitment:
    return Commitment(
        commitment_id=f"c-{owner}-{description}"[:60],
        session_id=session_id,
        owner=owner,
        side="unknown",
        description=description,
        quote="",
        timestamp_seconds=0.0,
        due_date_iso=due,
        created_at="2026-08-01T00:00:00",
        status=status,
    )


def _make_svc(tmp_path: Path, session_id: str, session_data: dict,
              commitments=None, alias_store=None):
    """Minimal stand-in for server.py's service container."""
    session_svc = SessionService(str(tmp_path), index_enabled=False)
    (tmp_path / f"session_{session_id}.json").write_text(
        json.dumps({"session_id": session_id, **session_data}),
        encoding="utf-8")
    commitments_svc = CommitmentsService(session_svc)
    if commitments:
        commitments_svc.replace_session_commitments(session_id, commitments)
    return SimpleNamespace(
        session_svc=session_svc,
        commitments_svc=commitments_svc,
        owner_alias_store=alias_store,
    )


# ── 1. Every format variant parses to the right owner ────────────────


CANONICAL = "- [ ] **[Jane Doe]**: Send the routing map"

VARIANTS = {
    "canonical bold+bracket+colon": CANONICAL,
    "bold, no brackets": "- [ ] **Jane Doe**: Send the routing map",
    "bold, em dash": "- [ ] **Jane Doe** — Send the routing map",
    "bold, en dash": "- [ ] **Jane Doe** – Send the routing map",
    "bold, ascii hyphen": "- [ ] **Jane Doe** - Send the routing map",
    "bold, no separator": "- [ ] **Jane Doe** Send the routing map",
    "brackets only": "- [ ] [Jane Doe]: Send the routing map",
    "brackets, hyphen": "- [ ] [Jane Doe] - Send the routing map",
    "no bold at all": "- [ ] Jane Doe: Send the routing map",
    "no bold, em dash": "- [ ] Jane Doe — Send the routing map",
    "checked box": "- [x] **Jane Doe**: Send the routing map",
    "empty box": "- [] **Jane Doe**: Send the routing map",
    "nested bullet": "    - [ ] **Jane Doe**: Send the routing map",
    "star bullet": "* [ ] **Jane Doe**: Send the routing map",
    "numbered": "1. [ ] **Jane Doe**: Send the routing map",
    "no checkbox": "- **Jane Doe**: Send the routing map",
    "underscore bold": "- [ ] __Jane Doe__: Send the routing map",
}


@pytest.mark.parametrize("label,line", sorted(VARIANTS.items()))
def test_every_variant_attributes_to_jane_doe(label, line):
    """One formatting deviation used to zero the whole feature."""
    md = f"## Action Items\n{line}\n"
    assert _owners(md) == {"Jane Doe": ["Send the routing map"]}, label


def test_markdown_table_with_header():
    md = (
        "## Action Items\n"
        "| Owner | Action | Due |\n"
        "| --- | --- | --- |\n"
        "| Jane Doe | Send the routing map | Fri |\n"
        "| Pat Roe | Book the Globex workshop | |\n"
    )
    assert _owners(md) == {
        "Jane Doe": ["Send the routing map"],
        "Pat Roe": ["Book the Globex workshop"],
    }


def test_markdown_table_with_owner_column_second():
    """Header mapping, not blind column 0 — a table can lead with the task."""
    md = (
        "## Action Items\n"
        "| Action Item | Assignee |\n"
        "|---|---|\n"
        "| Send the routing map | Jane Doe |\n"
    )
    assert _owners(md) == {"Jane Doe": ["Send the routing map"]}


def test_markdown_table_without_header_uses_name_shape():
    """No header row: column 0 is assumed to be the owner only when it
    reads as a name, so a headerless task/owner table is refused rather
    than inventing an owner called 'Send the routing map'."""
    ok = (
        "## Action Items\n"
        "| Jane Doe | Send the routing map |\n"
    )
    assert _owners(ok) == {"Jane Doe": ["Send the routing map"]}

    ambiguous = (
        "## Action Items\n"
        "| Send the routing map | Jane Doe |\n"
    )
    parse = parse_action_items_by_owner(ambiguous)
    assert parse.by_owner == {}
    assert parse.candidate_lines == 1
    assert parse.parsed_items == 0


def test_mixed_shapes_in_one_document():
    md = (
        "## Action Items\n"
        "- [ ] **[Jane Doe]**: Send the routing map\n"
        "  - [ ] Pat Roe — Draft the Initech migration plan\n"
        "- [x] **Sam Poe** Confirm the Umbrella licence count\n"
        "\n"
        "## Decisions Made\n"
        "- **Chosen**: we go with the phased rollout\n"
        "\n"
        "## Open Questions\n"
        "- [ ] **Nobody**: who owns the Zorg handover?\n"
    )
    parsed = _owners(md)
    assert parsed == {
        "Jane Doe": ["Send the routing map"],
        "Pat Roe": ["Draft the Initech migration plan"],
        "Sam Poe": ["Confirm the Umbrella licence count"],
    }
    # Section scoping: neither the Decisions bullet nor the Open Questions
    # one leaked in as somebody's task.
    assert "Chosen" not in parsed


def test_owner_with_org_suffix_and_calendar_form():
    """The awkward real-capture shapes, anonymised: an org suffix and the
    `Last, First Suffix [REGION]` organiser form."""
    md = (
        "## Action Items\n"
        "- [ ] Sam (Acme): Share the Hooli sizing model\n"
        "- [ ] Roe, Pat Jr. [US-EMEA]: Circulate the Northwind notes\n"
        "- [ ] Ana van der Noh: Review the Globex contract\n"
    )
    assert _owners(md) == {
        "Sam (Acme)": ["Share the Hooli sizing model"],
        "Roe, Pat Jr. [US-EMEA]": ["Circulate the Northwind notes"],
        "Ana van der Noh": ["Review the Globex contract"],
    }


# ── 2. A description containing a colon is not an owner ──────────────


NOT_OWNERS = [
    # Sentence-shaped: lowercase tokens and/or more than four words.
    "- [ ] Send the report: include the Q3 numbers",
    "- [ ] Decide on the pricing model: option A or B",
    "- [ ] Next steps: circulate the deck",
    "- [ ] Follow up with the vendor: confirm the SLA",
    "- [ ] Review the architecture doc: focus on failover",
    # Title-cased but verb-initial.
    "- [ ] Follow Up: confirm the SLA",
    "- [ ] Send Deck: to the Acme stakeholders",
    # Single token carrying topic-label morphology, not a name.
    "- [ ] Pricing: confirm with finance",
    "- [ ] Onboarding: schedule the kickoff",
    "- [ ] Migration: agree the cutover window",
    "- [ ] Notes: recap of the Globex thread",
]


@pytest.mark.parametrize("line", NOT_OWNERS)
def test_description_with_a_colon_is_not_read_as_an_owner(line):
    md = f"## Action Items\n{line}\n"
    parse = parse_action_items_by_owner(md)
    assert parse.by_owner == {}, f"{line!r} produced {parse.by_owner}"
    # It still counts as a line we saw and could not attribute, which is
    # what drives the UNREADABLE_FORMAT state rather than a silent zero.
    assert parse.candidate_lines == 1


def test_ascii_hyphen_needs_a_marked_owner():
    """" - " appears inside descriptions constantly, so an unmarked
    hyphen split is refused; em dash and en dash are accepted."""
    assert _owners("## Action Items\n- [ ] Jane Doe - Send the map\n") == {}
    assert _owners("## Action Items\n- [ ] Jane Doe — Send the map\n") == {
        "Jane Doe": ["Send the map"]}
    assert _owners("## Action Items\n- [ ] **Jane Doe** - Send the map\n") == {
        "Jane Doe": ["Send the map"]}


def test_marked_owner_is_trusted_even_when_it_reads_like_a_sentence():
    """The name-shape test only guards UNMARKED candidates — when the
    model says `**X**` it has told us which span is the owner."""
    md = "## Action Items\n- [ ] **Sam Doe and Pat Roe**: Split the write-up\n"
    assert _owners(md) == {"Sam Doe and Pat Roe": ["Split the write-up"]}


@pytest.mark.parametrize("candidate,expected", [
    ("Sam", True),
    ("Jane Doe", True),
    ("Roe, Pat Jr.", True),
    ("Sam (Acme)", True),
    ("Ana van der Noh", True),
    ("Send the report", False),
    ("Next steps", False),
    ("Follow Up", False),
    ("Pricing", False),
    ("", False),
    ("A" * 41, False),
    ("One Two Three Four Five", False),
])
def test_looks_like_person(candidate, expected):
    assert looks_like_person(candidate) is expected


# ── 3. Generic owners are still skipped ──────────────────────────────


@pytest.mark.parametrize("owner", [
    "Team", "team", "All", "Everyone", "everybody", "The Team",
    "Group", "TBD", "Unassigned", "N/A", "attendees",
])
def test_generic_owners_are_generic(owner):
    assert is_generic_owner(owner) is True


@pytest.mark.parametrize("owner", ["Jane Doe", "Sam", "Pat Roe", "Ana Noh"])
def test_real_names_are_not_generic(owner):
    assert is_generic_owner(owner) is False


def test_generic_owners_never_get_an_email():
    md = (
        "## Action Items\n"
        "- [ ] **Team**: Keep the Acme board updated\n"
        "- [ ] **Everyone**: Read the Globex brief\n"
        "- [ ] **Jane Doe**: Send the routing map\n"
    )
    parse = parse_action_items_by_owner(md)
    assert parse.by_owner == {"Jane Doe": ["Send the routing map"]}
    assert parse.parsed_items == 3
    assert parse.generic_items == 2


# ── 4. Commitments are preferred; markdown is the fallback ───────────


def test_commitments_are_preferred_over_markdown(tmp_path):
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": "## Action Items\n- [ ] **Sam Poe**: From markdown\n"},
        commitments=[_commitment("s1", "Jane Doe", "Send the routing map")],
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == READY
    assert plan.source == "commitments"
    assert plan.owners == {"Jane Doe": ["Send the routing map"]}


def test_commitment_due_date_rides_along(tmp_path):
    """A due date the extractor actually resolved is real data, so the
    drafting prompt gets it — this is not invented precision."""
    svc = _make_svc(
        tmp_path, "s1", {},
        commitments=[_commitment("s1", "Jane Doe", "Send the map",
                                 due="2026-08-28")],
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.owners == {"Jane Doe": ["Send the map (Due: 2026-08-28)"]}


def test_commitment_owners_are_split_and_alias_resolved(tmp_path):
    store = OwnerAliasStore(tmp_path)
    store.create("Sam", ["sam", "samantha"])
    svc = _make_svc(
        tmp_path, "s1", {},
        commitments=[
            _commitment("s1", "Pat Roe/Samantha", "Draft the Initech plan"),
        ],
        alias_store=store,
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.source == "commitments"
    assert set(plan.owners) == {"Pat Roe", "Sam"}


def test_delivered_commitments_do_not_earn_a_chaser(tmp_path):
    """Resolved commitments fall through to the markdown fallback rather
    than emailing somebody about something they already sent."""
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": "## Action Items\n- [ ] **Sam Poe**: From markdown\n"},
        commitments=[_commitment("s1", "Jane Doe", "Sent already",
                                 status="delivered")],
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == READY
    assert plan.source == "action_items"
    assert plan.owners == {"Sam Poe": ["From markdown"]}


def test_markdown_fallback_when_the_session_has_no_commitments(tmp_path):
    """Commitments are a separate, optional extraction pass — sessions
    that predate it, or where it never ran, have an empty sidecar."""
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": "## Action Items\n- [ ] Jane Doe: Send the map\n"},
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == READY
    assert plan.source == "action_items"
    assert plan.owners == {"Jane Doe": ["Send the map"]}


def test_missing_commitments_service_still_drafts(tmp_path):
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": "## Action Items\n- [ ] Jane Doe: Send the map\n"},
    )
    svc.commitments_svc = None
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == READY
    assert plan.source == "action_items"


def test_commitments_with_only_generic_owners_fall_through(tmp_path):
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": "## Action Items\n- [ ] Jane Doe: Send the map\n"},
        commitments=[_commitment("s1", "the team", "Keep Acme updated")],
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.source == "action_items"
    assert plan.owners == {"Jane Doe": ["Send the map"]}


def test_owners_from_commitments_counts_what_it_considered():
    by_owner, considered = owners_from_commitments([
        _commitment("s1", "Jane Doe", "a"),
        _commitment("s1", "team", "b"),
        _commitment("s1", "Pat Roe", "c", status="dismissed"),
    ])
    assert considered == 2          # the dismissed one is not considered
    assert by_owner == {"Jane Doe": ["a"]}


# ── 5. The three empty states are three different messages ───────────


def test_state_no_action_items(tmp_path):
    svc = _make_svc(tmp_path, "s1", {})
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == NO_ACTION_ITEMS
    assert plan.owners == {}
    assert "extract action items" in plan.message.lower()


def test_state_no_action_items_for_none_identified(tmp_path):
    """'None identified.' is the prompt's own empty marker — no
    item-shaped lines at all, so this is absence, not a format problem."""
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": "## Action Items\nNone identified.\n"},
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == NO_ACTION_ITEMS


def test_state_unreadable_format(tmp_path):
    """THE regression. Action items exist, every line is item-shaped, and
    not one of them yields an owner. The message must say the format
    could not be read — never that nobody was attributed."""
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": (
            "## Action Items\n"
            "- [ ] Send the sizing model to the Acme architects\n"
            "- [ ] Confirm the cutover window with the Globex team\n"
            "- [ ] Rework the Initech runbook before the pilot\n"
        )},
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == UNREADABLE_FORMAT
    assert plan.owners == {}
    low = plan.message.lower()
    assert "format" in low
    # It must NOT blame the model for failing to attribute owners.
    assert "didn't attribute" not in low
    assert "did not attribute" not in low


def test_state_generic_owners_only(tmp_path):
    svc = _make_svc(
        tmp_path, "s1",
        {"action_items": (
            "## Action Items\n"
            "- [ ] **Team**: Keep the Acme board updated\n"
            "- [ ] **Everyone**: Read the Globex brief\n"
        )},
    )
    plan = build_draft_plan(svc, "s1", svc.session_svc.load("s1"))
    assert plan.state == GENERIC_OWNERS_ONLY
    assert plan.owners == {}
    assert "team" in plan.message.lower()


def test_the_three_empty_states_have_three_distinct_messages(tmp_path):
    messages = set()
    cases = {
        "s_none": {},
        "s_unreadable": {"action_items":
                         "## Action Items\n- [ ] Send the sizing model\n"},
        "s_generic": {"action_items":
                      "## Action Items\n- [ ] **Team**: Keep Acme updated\n"},
    }
    states = set()
    for sid, data in cases.items():
        svc = _make_svc(tmp_path / sid, sid, data)
        plan = build_draft_plan(svc, sid, svc.session_svc.load(sid))
        messages.add(plan.message)
        states.add(plan.state)
    assert len(messages) == 3
    assert states == {NO_ACTION_ITEMS, UNREADABLE_FORMAT, GENERIC_OWNERS_ONLY}


# ── 6. Debug logging leaks shapes, never meeting content ─────────────


def test_unparsed_shape_carries_no_meeting_content():
    line = "- [ ] Jane Doe — Send the Q3 Globex deck"
    sketch = _shape(line)
    assert sketch == "- [ ] Aa Aa — Aa a A9 Aa a"
    for word in ("Jane", "Doe", "Globex", "deck", "Send"):
        assert word not in sketch
    # The thing a diagnosis actually needs survives: the separator and
    # the absence of bold.
    assert "—" in sketch and "[ ]" in sketch and "*" not in sketch


def test_unparsed_shapes_are_captured_for_the_debug_log():
    parse = parse_action_items_by_owner(
        "## Action Items\n"
        "- [ ] Send the sizing model to the Acme architects\n"
        "- [ ] Confirm the cutover window with the Globex team\n"
    )
    assert parse.by_owner == {}
    assert len(parse.unparsed_shapes) == 2
    assert all("Acme" not in s and "Globex" not in s
               for s in parse.unparsed_shapes)


def test_unparsed_shapes_are_capped():
    md = "## Action Items\n" + "".join(
        f"- [ ] Send item number {i} onwards\n" for i in range(40))
    parse = parse_action_items_by_owner(md)
    assert parse.candidate_lines == 40
    assert len(parse.unparsed_shapes) <= 8


# ── 7. Both platform backends use the shared parser ──────────────────


def test_parser_is_shared_not_duplicated():
    """If either backend grows its own copy of this again, the whole
    class of bug comes back on one platform only."""
    from services import _follow_up_email_macos as mac
    from services import _follow_up_email_outlook as win
    from services import follow_up_owners

    assert win.build_draft_plan is follow_up_owners.build_draft_plan
    assert mac.build_draft_plan is follow_up_owners.build_draft_plan
    for mod in (win, mac):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "_ACTION_ITEM_RE" not in src
        assert "def _parse_action_items_by_owner" not in src
