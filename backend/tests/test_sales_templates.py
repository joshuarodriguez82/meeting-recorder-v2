"""The account-management template set.

Third wave of users. The library grew for pre-sales SAs first
(Requirements Gathering, Design Review) and delivery second (UAT
Triage, Go-Live Readiness). Neither fits an account manager: their
meetings turn on commercial facts — who signs, what the budget cycle
is, which competitor is incumbent, what was conceded — and an SA-shaped
summary of a pricing call records the architecture and loses the
discount that was verbally offered.

The distinction that matters most, and the one these templates exist to
hold: **"Discovery" already exists as an SA template and means
something else.** The SA version gathers functional and non-functional
requirements. An AM's discovery call qualifies a deal — pain, decision
process, budget authority, timeline, competition. Same word, different
meeting, and merging them would lose whichever half is not being
written that week. So the AM one is named "Qualification Call".

Pinned here:

  1. The seven templates exist with the content markers that make them
     commercial rather than technical — decision/authority language in
     Qualification, concession language in Pricing, incumbent language
     in Competitive Displacement, and so on.

  2. They REACH EXISTING INSTALLS, without disturbing a user's own
     entries or their edits to older defaults. This is the property
     that makes shipping a template a code change rather than a
     runbook step for every account manager.

  3. They do not collide with the SA or delivery sets, because a
     library where two entries mean nearly the same thing gets picked
     from at random.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.template_service import DEFAULT_TEMPLATES, TemplateService

SALES_TEMPLATES = {
    # name -> markers whose presence makes it the commercial prompt
    "Qualification Call": ("decision", "budget", "timeline", "competitor"),
    "Executive Briefing": ("business outcome", "sponsor", "priorit"),
    "Solution Demo": ("objection", "reaction", "gap"),
    "Pricing & Commercial": ("concession", "procurement", "approval"),
    "Account Review / QBR": ("adoption", "risk", "expansion"),
    "Competitive Displacement": ("incumbent", "switching", "contract end"),
    "Sales-to-Delivery Handoff": ("promised", "in scope", "assumption"),
}


def test_sales_templates_exist_with_commercial_content():
    for name, markers in SALES_TEMPLATES.items():
        assert name in DEFAULT_TEMPLATES, f"missing template: {name}"
        prompt = DEFAULT_TEMPLATES[name].lower()
        for marker in markers:
            assert marker in prompt, (
                f"{name!r} lacks the {marker!r} focus that makes it an "
                f"account-management template rather than a renamed SA one")


def test_sales_templates_carry_the_visuals_directive():
    """Every built-in ends with the shared visuals policy. A demo or an
    exec briefing is exactly where screenshots turn up, so opting out
    here would be the worst place to do it."""
    for name in SALES_TEMPLATES:
        assert "## Visuals" in DEFAULT_TEMPLATES[name], name


def test_qualification_is_not_a_copy_of_requirements_gathering():
    """The two are about different things and must not converge — one
    qualifies a deal, the other specifies a system. If someone edits
    them toward each other, the library stops being a choice."""
    qual = DEFAULT_TEMPLATES["Qualification Call"].lower()
    reqs = DEFAULT_TEMPLATES["Requirements Gathering"].lower()
    assert "budget" in qual and "budget" not in reqs
    assert "non-functional" in reqs and "non-functional" not in qual


def test_handoff_names_the_seam_delivery_kickoff_starts_from():
    """The two halves of the same transition: sales records what was
    promised, delivery records what it is picking up. The handoff
    template has to talk about commitments made during the SALE, which
    is the thing Delivery Kickoff cannot know."""
    handoff = DEFAULT_TEMPLATES["Sales-to-Delivery Handoff"].lower()
    assert "promised" in handoff or "commitment" in handoff
    assert "delivery" in handoff


def test_existing_installs_gain_the_sales_templates(tmp_path: Path):
    """A pre-sales-set store — old defaults only, one customized, one
    user-created — gains the seven on construction, with every existing
    entry byte-identical."""
    store = tmp_path / "summary_templates.json"
    old = {
        "General": {
            "prompt": "MY CUSTOMIZED GENERAL",
            "is_default": True,
            "default_prompt": "old canonical general",
            "hidden": False,
        },
        "Pipeline Notes": {  # user-created
            "prompt": "custom pipeline prompt",
            "is_default": False,
            "default_prompt": None,
            "hidden": False,
        },
    }
    store.write_text(json.dumps(old), encoding="utf-8")

    svc = TemplateService(tmp_path)
    names = {t.name for t in svc.list_all()}

    for name in SALES_TEMPLATES:
        assert name in names, f"{name} did not reach the existing install"
        t = svc.get(name)
        assert t is not None and t.is_default
        assert t.prompt == DEFAULT_TEMPLATES[name]

    assert svc.get("General").prompt == "MY CUSTOMIZED GENERAL"
    assert svc.get("Pipeline Notes").prompt == "custom pipeline prompt"


def test_sales_template_can_be_hidden_and_restored(tmp_path: Path):
    """They behave like every other default: delete hides, reset
    restores. An AM who never runs QBRs should be able to clear it out
    of the picker without losing the ability to get it back."""
    svc = TemplateService(tmp_path)
    svc.delete("Account Review / QBR")
    assert svc.get("Account Review / QBR") is None
    restored = svc.reset("Account Review / QBR")
    assert restored is not None
    assert restored.prompt == DEFAULT_TEMPLATES["Account Review / QBR"]


def test_no_two_default_templates_share_a_name_case_insensitively():
    """Three waves of templates have been added by three different
    people at three different times. A near-duplicate name is how a
    picker becomes a coin flip."""
    lowered = [n.lower() for n in DEFAULT_TEMPLATES]
    assert len(lowered) == len(set(lowered))
