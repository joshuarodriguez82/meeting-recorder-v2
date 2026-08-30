"""Live co-pilot meeting types for the account-management motions.

The co-pilot composes a MODE (persona) with a MEETING TYPE (lens). The
Sales persona already exists; the meeting types did not cover the
motions an account manager actually runs. `Discovery` is an SA
requirements lens, `Customer Demo` is about the demo rather than the
deal, and there was nothing at all for a pricing conversation — the one
meeting where a number said out loud becomes a commitment, and the one
where live prompting is worth most.

These four fill that. They are the LIVE half of the same work the sales
summary templates do afterwards: same motions, different job. A summary
records what happened; a co-pilot lens is trying to change what happens
next, so it is written as things to probe and risks to flag, not as
sections to produce.

Pinned: the four exist with lens-shaped content, they reach existing
installs without disturbing user edits, and they stay distinct from the
SA types they sit beside.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.copilot_meeting_type_service import (
    DEFAULT_MEETING_TYPES, CoPilotMeetingTypeService,
)

SALES_TYPES = {
    # name -> markers that make it the commercial lens
    "Qualification": ("budget", "authority", "compelling event"),
    "Pricing / Negotiation": ("concession", "anchor", "authority"),
    "Executive Briefing": ("outcome", "sponsor", "jargon"),
    "Renewal / Account Review": ("renewal", "risk", "expansion"),
}


def test_sales_types_exist_with_lens_content():
    for name, markers in SALES_TYPES.items():
        assert name in DEFAULT_MEETING_TYPES, f"missing meeting type: {name}"
        prompt = DEFAULT_MEETING_TYPES[name].lower()
        for marker in markers:
            assert marker in prompt, (
                f"{name!r} lacks the {marker!r} focus that makes it a "
                f"commercial lens rather than a renamed SA one")


def test_they_are_written_as_a_lens_not_a_summary_spec():
    """A meeting type tells the co-pilot what to WATCH FOR while the
    meeting is happening. One written as a list of summary sections
    produces a co-pilot that narrates instead of coaching."""
    for name in SALES_TYPES:
        prompt = DEFAULT_MEETING_TYPES[name].lower()
        assert "flag" in prompt, f"{name} names no risks to flag"
        assert "probe" in prompt, f"{name} names nothing to probe"


def test_qualification_is_distinct_from_the_sa_discovery_lens():
    """Both are first-conversation lenses and they must not converge.
    Discovery is about the system; Qualification is about the deal."""
    qual = DEFAULT_MEETING_TYPES["Qualification"].lower()
    disc = DEFAULT_MEETING_TYPES["Discovery"].lower()
    assert "budget" in qual and "budget" not in disc
    assert "current-state diagram" in disc and "current-state diagram" not in qual


def test_existing_installs_gain_the_sales_types(tmp_path: Path):
    store = tmp_path / "copilot_meeting_types.json"
    old = {
        "General": {
            "prompt": "MY CUSTOMIZED GENERAL",
            "is_default": True,
            "default_prompt": "old canonical general",
            "hidden": False,
        },
        "Board Sync": {
            "prompt": "custom board sync lens",
            "is_default": False,
            "default_prompt": None,
            "hidden": False,
        },
    }
    store.write_text(json.dumps(old), encoding="utf-8")

    svc = CoPilotMeetingTypeService(tmp_path)
    names = {t.name for t in svc.list_all()}
    for name in SALES_TYPES:
        assert name in names, f"{name} did not reach the existing install"
        assert svc.get(name).prompt == DEFAULT_MEETING_TYPES[name]

    assert svc.get("General").prompt == "MY CUSTOMIZED GENERAL"
    assert svc.get("Board Sync").prompt == "custom board sync lens"


def test_no_duplicate_meeting_type_names():
    lowered = [n.lower() for n in DEFAULT_MEETING_TYPES]
    assert len(lowered) == len(set(lowered))
