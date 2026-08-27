"""The delivery-phase template set.

The first wave of users were pre-sales SAs, and the built-in template
library says so: Requirements Gathering, Design Review, Stakeholder
Update. The second wave is the DELIVERY team — the engineers who take
the opportunity after SOW signature and build, test, cut over, and
stabilize it. Their meetings have different shapes, and a "Requirements
Gathering" summary of a UAT defect-triage call buries exactly the
fields that matter (defect IDs, severities, owners, retest gates).

These tests pin two things:

  1. The six delivery templates exist in DEFAULT_TEMPLATES with the
     content markers that make them delivery templates and not renamed
     copies of the SA set — the go/no-go language in Cutover, defect
     severity language in Triage, the in-scope/out-of-scope language in
     CR Scoping, and so on.

  2. They REACH EXISTING INSTALLS. TemplateService._ensure_seeded
     migrates the on-disk store toward the current DEFAULT_TEMPLATES on
     construction; a store written before these templates existed must
     gain them on next launch without touching the user's own entries
     or their edits to older defaults. (This is the property that makes
     shipping a template a code change rather than an ask-every-user
     runbook step.)
"""

from __future__ import annotations

import json
from pathlib import Path

from services.template_service import DEFAULT_TEMPLATES, TemplateService

DELIVERY_TEMPLATES = {
    # name -> markers whose presence makes it the delivery-shaped prompt
    "Delivery Kickoff": ("scope", "handoff", "assumption"),
    "Technical Working Session": ("integration", "configuration", "blocker"),
    "UAT & Defect Triage": ("defect", "severity", "retest"),
    "Go-Live Readiness": ("go/no-go", "rollback", "cutover"),
    "Hypercare Review": ("hypercare", "exit criteria", "severity"),
    "Change Request Scoping": ("in scope", "out of scope", "effort"),
}


def test_delivery_templates_exist_with_delivery_content():
    for name, markers in DELIVERY_TEMPLATES.items():
        assert name in DEFAULT_TEMPLATES, f"missing template: {name}"
        prompt = DEFAULT_TEMPLATES[name].lower()
        for marker in markers:
            assert marker in prompt, (
                f"{name!r} lacks the {marker!r} focus that makes it a "
                f"delivery template rather than a renamed SA one")


def test_delivery_templates_carry_the_visuals_directive():
    """Every built-in ends with the shared visuals policy; the new six
    must not silently opt out of it."""
    for name in DELIVERY_TEMPLATES:
        assert "## Visuals" in DEFAULT_TEMPLATES[name], name


def test_existing_installs_gain_the_delivery_templates(tmp_path: Path):
    """A pre-delivery-era store — old defaults only, one customized, one
    user-created — must gain the six new templates on construction with
    every existing entry byte-identical."""
    store = tmp_path / "summary_templates.json"
    old = {
        "General": {
            "prompt": "MY CUSTOMIZED GENERAL",  # user-edited default
            "is_default": True,
            "default_prompt": "old canonical general",
            "hidden": False,
        },
        "Board Prep": {  # user-created
            "prompt": "custom board prep prompt",
            "is_default": False,
            "default_prompt": None,
            "hidden": False,
        },
    }
    store.write_text(json.dumps(old), encoding="utf-8")

    svc = TemplateService(tmp_path)
    names = {t.name for t in svc.list_all()}

    for name in DELIVERY_TEMPLATES:
        assert name in names, f"{name} did not reach the existing install"
        t = svc.get(name)
        assert t is not None and t.is_default
        assert t.prompt == DEFAULT_TEMPLATES[name]

    # The user's own data is untouched.
    assert svc.get("General").prompt == "MY CUSTOMIZED GENERAL"
    assert svc.get("Board Prep").prompt == "custom board prep prompt"


def test_delivery_template_can_be_hidden_and_restored(tmp_path: Path):
    """The new entries behave like every other default: delete hides
    (keeps default_prompt for restore), reset brings them back."""
    svc = TemplateService(tmp_path)
    svc.delete("Go-Live Readiness")
    assert svc.get("Go-Live Readiness") is None
    restored = svc.reset("Go-Live Readiness")
    assert restored is not None
    assert restored.prompt == DEFAULT_TEMPLATES["Go-Live Readiness"]
