"""Structured extraction records.

The summarizer historically emitted markdown blobs (session.summary /
action_items / decisions / requirements). Markdown is fine for one
session's UI but can't be rolled up across an engagement — you can't
dedupe a requirement, track a decision's status over time, or count
open commitments across every ACME call when it's prose.

These typed records are the structured counterpart. They live ON the
session (session.*_struct) alongside the markdown fields, which stay
populated by the existing extractors so the current frontend is
untouched. The engagement register (Phase 2) aggregates these across
sessions; export (Phase 3) serialises them for handoff.

The LLM only supplies the content fields. Identity/provenance
(id, session_id, created_at, status) is stamped by us in from_llm so
the model can't fabricate or drift them.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

# Allowed enum-ish values. Kept permissive: an unknown value from a
# weak model is coerced to the safe default rather than raising — a
# best-effort extraction should degrade, not blow up the pipeline.
REQUIREMENT_KINDS = ("functional", "nonfunctional", "constraint", "assumption")
SOURCES = ("transcript", "notes")

# Defect vocabulary. Deliberately the words delivery teams actually use
# in triage, so the extracted value usually IS the spoken one.
DEFECT_SEVERITIES = ("critical", "high", "medium", "low")
DEFECT_STATUSES = ("open", "in_progress", "fixed", "retest", "closed",
                   "deferred", "rejected")
# How the scope argument resolved — the question that decides who pays.
DEFECT_DISPOSITIONS = ("undetermined", "in_scope", "out_of_scope",
                       "change_request", "working_as_designed")

# Statuses that mean "no longer live work". `retest` deliberately is NOT
# here: something awaiting retest is still the delivery team's problem.
DEFECT_CLOSED_STATUSES = ("closed", "rejected")

# Spoken forms that map onto the canonical vocabulary. Triage says "sev1"
# and "P1", not "critical"; a register that dropped those to the default
# would under-report exactly the defects that matter most.
_SEVERITY_ALIASES = {
    "sev1": "critical", "s1": "critical", "p1": "critical",
    "blocker": "critical", "urgent": "critical", "sev 1": "critical",
    "sev2": "high", "s2": "high", "p2": "high", "major": "high",
    "sev3": "medium", "s3": "medium", "p3": "medium", "normal": "medium",
    "sev4": "low", "s4": "low", "p4": "low", "minor": "low",
    "trivial": "low", "cosmetic": "low",
}
_STATUS_ALIASES = {
    "new": "open", "reopened": "open", "reopen": "open", "failed": "open",
    "failed retest": "open", "in progress": "in_progress",
    "wip": "in_progress", "assigned": "in_progress",
    "resolved": "fixed", "done": "fixed", "ready for retest": "retest",
    "retesting": "retest", "verify": "retest", "verifying": "retest",
    "verified": "closed", "passed": "closed", "complete": "closed",
    "wont fix": "rejected", "won't fix": "rejected", "not a bug": "rejected",
    "deferred": "deferred", "backlog": "deferred", "parked": "deferred",
}
_DISPOSITION_ALIASES = {
    "in scope": "in_scope", "out of scope": "out_of_scope",
    "oos": "out_of_scope", "cr": "change_request",
    "change request": "change_request", "wad": "working_as_designed",
    "working as designed": "working_as_designed",
    "by design": "working_as_designed", "as designed": "working_as_designed",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _coerce(value: str, allowed: tuple, default: str) -> str:
    v = (value or "").strip().lower()
    return v if v in allowed else default


def _coerce_alias(value: str, allowed: tuple, aliases: dict,
                  default: str) -> str:
    """Like _coerce, but first tries the spoken-form alias table.

    Also normalises separators, so "in-progress", "in progress" and
    "in_progress" all land on the same canonical value.
    """
    v = (value or "").strip().lower()
    if v in allowed:
        return v
    if v in aliases:
        return aliases[v]
    squashed = v.replace("-", " ").replace("_", " ").strip()
    if squashed in aliases:
        return aliases[squashed]
    underscored = squashed.replace(" ", "_")
    if underscored in allowed:
        return underscored
    return default


@dataclass
class Requirement:
    text: str
    kind: str = "functional"
    status: str = "open"          # open | met | dropped
    source: str = "transcript"    # transcript | notes
    id: str = field(default_factory=_new_id)
    session_id: str = ""
    created_at: str = ""

    @classmethod
    def from_llm(cls, d: dict, session_id: str, created_at: str) -> "Requirement":
        return cls(
            text=str(d.get("text", "")).strip(),
            kind=_coerce(d.get("kind", ""), REQUIREMENT_KINDS, "functional"),
            source=_coerce(d.get("source", ""), SOURCES, "transcript"),
            session_id=session_id,
            created_at=created_at,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "text": self.text, "kind": self.kind,
            "status": self.status, "source": self.source,
            "session_id": self.session_id, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Requirement":
        return cls(
            text=d.get("text", "") or "",
            kind=d.get("kind", "functional") or "functional",
            status=d.get("status", "open") or "open",
            source=d.get("source", "transcript") or "transcript",
            id=d.get("id") or _new_id(),
            session_id=d.get("session_id", "") or "",
            created_at=d.get("created_at", "") or "",
        )


@dataclass
class Decision:
    title: str
    decided: str = ""
    rationale: str = ""
    alternatives: str = ""
    owner: str = ""
    impact: str = ""
    source: str = "transcript"
    id: str = field(default_factory=_new_id)
    session_id: str = ""
    created_at: str = ""

    @classmethod
    def from_llm(cls, d: dict, session_id: str, created_at: str) -> "Decision":
        return cls(
            title=str(d.get("title", "")).strip(),
            decided=str(d.get("decided", "")).strip(),
            rationale=str(d.get("rationale", "")).strip(),
            alternatives=str(d.get("alternatives", "")).strip(),
            owner=str(d.get("owner", "")).strip(),
            impact=str(d.get("impact", "")).strip(),
            source=_coerce(d.get("source", ""), SOURCES, "transcript"),
            session_id=session_id,
            created_at=created_at,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "decided": self.decided,
            "rationale": self.rationale, "alternatives": self.alternatives,
            "owner": self.owner, "impact": self.impact, "source": self.source,
            "session_id": self.session_id, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        return cls(
            title=d.get("title", "") or "",
            decided=d.get("decided", "") or "",
            rationale=d.get("rationale", "") or "",
            alternatives=d.get("alternatives", "") or "",
            owner=d.get("owner", "") or "",
            impact=d.get("impact", "") or "",
            source=d.get("source", "transcript") or "transcript",
            id=d.get("id") or _new_id(),
            session_id=d.get("session_id", "") or "",
            created_at=d.get("created_at", "") or "",
        )


@dataclass
class ActionItem:
    text: str
    owner: str = ""
    due: Optional[str] = None
    status: str = "open"          # open | done
    source: str = "transcript"
    id: str = field(default_factory=_new_id)
    session_id: str = ""
    created_at: str = ""

    @classmethod
    def from_llm(cls, d: dict, session_id: str, created_at: str) -> "ActionItem":
        due = str(d.get("due", "")).strip()
        return cls(
            text=str(d.get("text", "")).strip(),
            owner=str(d.get("owner", "")).strip(),
            due=due or None,
            source=_coerce(d.get("source", ""), SOURCES, "transcript"),
            session_id=session_id,
            created_at=created_at,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "text": self.text, "owner": self.owner,
            "due": self.due, "status": self.status, "source": self.source,
            "session_id": self.session_id, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ActionItem":
        return cls(
            text=d.get("text", "") or "",
            owner=d.get("owner", "") or "",
            due=d.get("due"),
            status=d.get("status", "open") or "open",
            source=d.get("source", "transcript") or "transcript",
            id=d.get("id") or _new_id(),
            session_id=d.get("session_id", "") or "",
            created_at=d.get("created_at", "") or "",
        )


@dataclass
class OpenQuestion:
    text: str
    status: str = "open"          # open | answered
    source: str = "transcript"
    id: str = field(default_factory=_new_id)
    session_id: str = ""
    created_at: str = ""

    @classmethod
    def from_llm(cls, d: dict, session_id: str, created_at: str) -> "OpenQuestion":
        return cls(
            text=str(d.get("text", "")).strip(),
            source=_coerce(d.get("source", ""), SOURCES, "transcript"),
            session_id=session_id,
            created_at=created_at,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "text": self.text, "status": self.status,
            "source": self.source, "session_id": self.session_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OpenQuestion":
        return cls(
            text=d.get("text", "") or "",
            status=d.get("status", "open") or "open",
            source=d.get("source", "transcript") or "transcript",
            id=d.get("id") or _new_id(),
            session_id=d.get("session_id", "") or "",
            created_at=d.get("created_at", "") or "",
        )


@dataclass
class Defect:
    """One defect as discussed in a UAT / triage call.

    `ref` is the customer's own identifier when one was stated. It is
    the register's primary key where present, because triage calls say
    "DEF-142" far more often than they restate the description, and two
    phrasings of the same ID are one defect.

    Unlike the other record types, a defect's status is NOT monotonic:
    fixed → retest → failed → open is its ordinary life. The engagement
    roll-up therefore resolves defect status (and severity) to the most
    recent occurrence rather than to any terminal value ever seen.
    """

    title: str
    ref: str = ""                    # customer's defect ID, if stated
    severity: str = "medium"         # critical | high | medium | low
    status: str = "open"
    owner: str = ""
    due: Optional[str] = None        # expected fix / retest date
    disposition: str = "undetermined"
    source: str = "transcript"
    id: str = field(default_factory=_new_id)
    session_id: str = ""
    created_at: str = ""

    @classmethod
    def from_llm(cls, d: dict, session_id: str, created_at: str) -> "Defect":
        due = str(d.get("due", "")).strip()
        return cls(
            title=str(d.get("title", "")).strip(),
            ref=str(d.get("ref", "")).strip(),
            severity=_coerce_alias(d.get("severity", ""), DEFECT_SEVERITIES,
                                   _SEVERITY_ALIASES, "medium"),
            status=_coerce_alias(d.get("status", ""), DEFECT_STATUSES,
                                 _STATUS_ALIASES, "open"),
            owner=str(d.get("owner", "")).strip(),
            due=due or None,
            disposition=_coerce_alias(d.get("disposition", ""),
                                      DEFECT_DISPOSITIONS,
                                      _DISPOSITION_ALIASES, "undetermined"),
            source=_coerce(d.get("source", ""), SOURCES, "transcript"),
            session_id=session_id,
            created_at=created_at,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "ref": self.ref,
            "severity": self.severity, "status": self.status,
            "owner": self.owner, "due": self.due,
            "disposition": self.disposition, "source": self.source,
            "session_id": self.session_id, "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Defect":
        return cls(
            title=d.get("title", "") or "",
            ref=d.get("ref", "") or "",
            severity=d.get("severity", "medium") or "medium",
            status=d.get("status", "open") or "open",
            owner=d.get("owner", "") or "",
            due=d.get("due"),
            disposition=d.get("disposition", "undetermined") or "undetermined",
            source=d.get("source", "transcript") or "transcript",
            id=d.get("id") or _new_id(),
            session_id=d.get("session_id", "") or "",
            created_at=d.get("created_at", "") or "",
        )


# Maps the JSON keys the LLM returns → (record class, session attribute).
# Single source of truth for the summarizer, the session model, and the
# server wiring so adding a record type is a one-line change.
STRUCTURED_FIELDS = {
    "requirements": (Requirement, "requirements_struct"),
    "decisions": (Decision, "decisions_struct"),
    "action_items": (ActionItem, "action_items_struct"),
    "open_questions": (OpenQuestion, "open_questions"),
    "defects": (Defect, "defects_struct"),
}


def stamp_records(
    parsed: dict, session_id: str, created_at: str
) -> dict:
    """Turn the LLM's {key: [ {..}, .. ]} into typed records, stamping
    provenance. Unknown keys ignored; non-list values treated as empty
    so a malformed slice can't sink the whole extraction."""
    out: dict = {}
    for key, (cls, _attr) in STRUCTURED_FIELDS.items():
        rows = parsed.get(key)
        if not isinstance(rows, list):
            out[key] = []
            continue
        recs = []
        for row in rows:
            if isinstance(row, dict) and str(row.get("text") or row.get("title") or "").strip():
                recs.append(cls.from_llm(row, session_id, created_at))
        out[key] = recs
    return out
