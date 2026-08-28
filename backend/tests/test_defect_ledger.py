"""The cross-session defect register.

UAT and defect triage is the highest-volume meeting of a delivery
engagement, and until now its output died inside one session's summary.
What a delivery engineer actually needs is the *register*: every defect
raised across every triage call, with its current severity, owner and
status — the thing they open before the next call rather than the thing
they read once after the last one.

Defects reuse the typed-record + engagement-rollup machinery the other
four record types use, with one deliberate difference that is the whole
reason this needs its own tests:

  STATUS IS LATEST-WINS, NOT TERMINAL-WINS.

For a requirement, "it was met in some call" settles it forever, so the
existing rollup lets a terminal status win permanently. A defect does
not behave that way — fixed → retest → **failed** → open again is the
normal life of a defect, and a register that showed it as "fixed"
because it was fixed once would be actively misleading in the exact
meeting it exists to support. So a defect's status comes from its most
recent occurrence, in session order.

The other domain fact pinned here: defects are keyed on the customer's
own defect ID when one was stated. Triage calls say "DEF-142" far more
often than they restate the description, and two different phrasings of
DEF-142 are one defect, not two.
"""

from __future__ import annotations

from types import SimpleNamespace

from models.extraction import (
    STRUCTURED_FIELDS,
    Defect,
    stamp_records,
)
from services.engagement_service import EngagementService


# ── the record type ──────────────────────────────────────────────────

def test_defect_is_a_registered_structured_field():
    assert "defects" in STRUCTURED_FIELDS
    cls, attr = STRUCTURED_FIELDS["defects"]
    assert cls is Defect
    assert attr == "defects_struct"


def test_from_llm_stamps_provenance_and_coerces_enums():
    d = Defect.from_llm(
        {"title": "Transfer drops after hold", "ref": "DEF-142",
         "severity": "SEV1", "status": "Retest", "owner": "Jane Roe",
         "due": "2026-09-02", "disposition": "in scope"},
        session_id="S1", created_at="2026-09-01T10:00:00")
    assert d.title == "Transfer drops after hold"
    assert d.ref == "DEF-142"
    assert d.session_id == "S1" and d.created_at == "2026-09-01T10:00:00"
    # Unknown/awkward enum values coerce to a safe default rather than
    # raising — a weak model must degrade the record, not sink the run.
    assert d.severity in ("critical", "high", "medium", "low")
    assert d.status in ("open", "in_progress", "fixed", "retest",
                        "closed", "deferred", "rejected")


def test_unknown_enum_values_fall_back_rather_than_raise():
    d = Defect.from_llm({"title": "x", "severity": "spicy",
                         "status": "vibes", "disposition": "???"},
                        session_id="S", created_at="")
    assert d.severity == "medium"
    assert d.status == "open"
    assert d.disposition == "undetermined"


def test_dict_round_trip_preserves_every_field():
    d = Defect.from_llm(
        {"title": "Queue overflow", "ref": "DEF-9", "severity": "high",
         "status": "fixed", "owner": "Ada Poe", "due": "2026-09-10",
         "disposition": "change_request"},
        session_id="S2", created_at="2026-09-02T09:00:00")
    back = Defect.from_dict(d.to_dict())
    assert back.to_dict() == d.to_dict()


def test_stamp_records_builds_defects_and_skips_titleless_rows():
    out = stamp_records(
        {"defects": [{"title": "Real one"}, {"title": "   "}, {"ref": "no title"}]},
        session_id="S", created_at="")
    assert [d.title for d in out["defects"]] == ["Real one"]


# ── the register roll-up ─────────────────────────────────────────────

def _session(sid: str, defects: list) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=sid,
        requirements_struct=[], decisions_struct=[],
        action_items_struct=[], open_questions=[],
        defects_struct=defects,
    )


def _agg(*pairs):
    """pairs: (meta, session) oldest first — the order _aggregate wants."""
    return EngagementService._aggregate(list(pairs))


def test_a_defect_reopening_is_reported_as_open_not_fixed(monkeypatch):
    """THE one that matters. Fixed in call 2, failed retest in call 3.
    Terminal-wins (what requirements use) would report 'fixed' and send
    the team into the next triage believing it was done."""
    early = Defect.from_llm({"title": "Transfer drops", "ref": "DEF-142",
                             "status": "open", "severity": "high"},
                            "S1", "2026-09-01T10:00:00")
    fixed = Defect.from_llm({"title": "Transfer drops", "ref": "DEF-142",
                             "status": "fixed", "severity": "high"},
                            "S2", "2026-09-05T10:00:00")
    reopened = Defect.from_llm({"title": "Transfer still drops",
                                "ref": "DEF-142", "status": "open",
                                "severity": "critical"},
                               "S3", "2026-09-08T10:00:00")
    reg = _agg(({"session_id": "S1"}, _session("S1", [early])),
               ({"session_id": "S2"}, _session("S2", [fixed])),
               ({"session_id": "S3"}, _session("S3", [reopened])))
    rows = reg["defects"]
    assert len(rows) == 1, "same ref must collapse to one register row"
    assert rows[0]["status"] == "open"
    assert len(rows[0]["occurrences"]) == 3


def test_latest_severity_wins_too():
    """Severity gets re-triaged; the register must show what the team
    last agreed, not what it was first called."""
    a = Defect.from_llm({"title": "t", "ref": "D-1", "severity": "low"},
                        "S1", "2026-09-01T10:00:00")
    b = Defect.from_llm({"title": "t", "ref": "D-1", "severity": "critical"},
                        "S2", "2026-09-05T10:00:00")
    reg = _agg(({"session_id": "S1"}, _session("S1", [a])),
               ({"session_id": "S2"}, _session("S2", [b])))
    assert reg["defects"][0]["severity"] == "critical"


def test_defects_collapse_on_customer_ref_not_wording():
    """Triage says the ID, not the description. Two phrasings of DEF-7
    are one defect."""
    a = Defect.from_llm({"title": "Callback never fires", "ref": "DEF-7"},
                        "S1", "2026-09-01T10:00:00")
    b = Defect.from_llm({"title": "no callback on abandon", "ref": "DEF-7"},
                        "S2", "2026-09-02T10:00:00")
    reg = _agg(({"session_id": "S1"}, _session("S1", [a])),
               ({"session_id": "S2"}, _session("S2", [b])))
    assert len(reg["defects"]) == 1


def test_defects_without_a_ref_still_collapse_on_title():
    a = Defect.from_llm({"title": "Audio cuts out on transfer"},
                        "S1", "2026-09-01T10:00:00")
    b = Defect.from_llm({"title": "audio cuts out on transfer"},
                        "S2", "2026-09-02T10:00:00")
    reg = _agg(({"session_id": "S1"}, _session("S1", [a])),
               ({"session_id": "S2"}, _session("S2", [b])))
    assert len(reg["defects"]) == 1


def test_two_different_defects_stay_separate():
    a = Defect.from_llm({"title": "A", "ref": "D-1"}, "S1", "2026-09-01T10:00:00")
    b = Defect.from_llm({"title": "B", "ref": "D-2"}, "S1", "2026-09-01T10:00:00")
    reg = _agg(({"session_id": "S1"}, _session("S1", [a, b])))
    assert len(reg["defects"]) == 2


def test_open_defect_count_excludes_closed_and_rejected():
    rows = [
        Defect.from_llm({"title": "a", "ref": "1", "status": "open"}, "S", ""),
        Defect.from_llm({"title": "b", "ref": "2", "status": "retest"}, "S", ""),
        Defect.from_llm({"title": "c", "ref": "3", "status": "closed"}, "S", ""),
        Defect.from_llm({"title": "d", "ref": "4", "status": "rejected"}, "S", ""),
    ]
    reg = _agg(({"session_id": "S"}, _session("S", rows)))
    # open + retest are live work; closed/rejected are not.
    assert reg["counts"]["open_defects"] == 2


def test_critical_and_high_open_defects_are_counted_separately():
    """The number a delivery lead is asked for in every status call."""
    rows = [
        Defect.from_llm({"title": "a", "ref": "1", "severity": "critical",
                         "status": "open"}, "S", ""),
        Defect.from_llm({"title": "b", "ref": "2", "severity": "high",
                         "status": "open"}, "S", ""),
        Defect.from_llm({"title": "c", "ref": "3", "severity": "critical",
                         "status": "closed"}, "S", ""),
        Defect.from_llm({"title": "d", "ref": "4", "severity": "low",
                         "status": "open"}, "S", ""),
    ]
    reg = _agg(({"session_id": "S"}, _session("S", rows)))
    assert reg["counts"]["open_critical_high_defects"] == 2


def test_open_defects_sort_above_resolved_ones():
    closed = Defect.from_llm({"title": "z", "ref": "1", "status": "closed"},
                             "S", "2026-09-01T10:00:00")
    live = Defect.from_llm({"title": "a", "ref": "2", "status": "open"},
                           "S", "2026-09-01T10:00:00")
    reg = _agg(({"session_id": "S"}, _session("S", [closed, live])))
    assert reg["defects"][0]["ref"] == "2"


# ── it must not disturb the existing register ────────────────────────

def test_existing_record_types_are_unaffected():
    from models.extraction import Requirement
    r1 = Requirement.from_llm({"text": "must log calls"}, "S1", "2026-09-01")
    r2 = Requirement.from_llm({"text": "must log calls"}, "S2", "2026-09-02")
    r2.status = "met"
    reg = _agg(({"session_id": "S1"}, _session("S1", [])),
               ({"session_id": "S2"}, _session("S2", [])))
    assert reg["defects"] == []
    assert reg["counts"]["open_defects"] == 0
    # requirements still use terminal-wins, unchanged by the defect work
    s1 = _session("S1", []); s1.requirements_struct = [r1]
    s2 = _session("S2", []); s2.requirements_struct = [r2]
    reg2 = _agg(({"session_id": "S1"}, s1), ({"session_id": "S2"}, s2))
    assert reg2["requirements"][0]["status"] == "met"
