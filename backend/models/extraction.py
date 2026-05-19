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


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _coerce(value: str, allowed: tuple, default: str) -> str:
    v = (value or "").strip().lower()
    return v if v in allowed else default


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


# Maps the JSON keys the LLM returns → (record class, session attribute).
# Single source of truth for the summarizer, the session model, and the
# server wiring so adding a record type is a one-line change.
STRUCTURED_FIELDS = {
    "requirements": (Requirement, "requirements_struct"),
    "decisions": (Decision, "decisions_struct"),
    "action_items": (ActionItem, "action_items_struct"),
    "open_questions": (OpenQuestion, "open_questions"),
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
