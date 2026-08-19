"""Typed views over the backend's wire shapes.

The one that matters is the /search/semantic discriminated union.
backend/services/search_service.py::SearchService.search returns EITHER

    {"source": "session",  session_id, display_name, started_at,
                           client, project, start_s, end_s, text, similarity}

or

    {"source": "document", doc_name, doc_path, client, text, similarity}

A document hit has NO session_id / display_name / started_at / start_s /
end_s. src/lib/api.ts carries a comment about the frontend once treating
document hits as sessions and rendering unopenable "Untitled" rows —
this module exists so that class of bug can't recur here: parsing is the
only place `source` is interpreted, and every consumer gets a concrete
SessionHit or DocumentHit.

`source` is treated as optional-on-session for the same backward-compat
reason the frontend does: the field is additive, so a backend predating
it emits session hits with no `source`. A hit with no `source` AND no
`session_id` is neither, and becomes an UnknownHit rather than being
silently coerced or dropped — a hit we can't classify must still be
visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Union


@dataclass(frozen=True)
class SessionHit:
    session_id: str
    display_name: str
    started_at: str
    client: str
    project: str
    start_s: float
    end_s: float
    text: str
    similarity: float


@dataclass(frozen=True)
class DocumentHit:
    doc_name: str
    doc_path: str
    client: str
    text: str
    similarity: float


@dataclass(frozen=True)
class UnknownHit:
    """A hit that matched neither variant. Kept, never dropped."""
    raw: Dict[str, Any] = field(default_factory=dict)


SearchHit = Union[SessionHit, DocumentHit, UnknownHit]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def parse_hit(raw: Dict[str, Any]) -> SearchHit:
    if not isinstance(raw, dict):
        return UnknownHit(raw={"value": raw})
    source = raw.get("source")
    if source == "document":
        return DocumentHit(
            doc_name=_s(raw.get("doc_name")),
            doc_path=_s(raw.get("doc_path")),
            client=_s(raw.get("client")),
            text=_s(raw.get("text")),
            similarity=_f(raw.get("similarity")),
        )
    if source == "session" or (source is None and raw.get("session_id")):
        return SessionHit(
            session_id=_s(raw.get("session_id")),
            display_name=_s(raw.get("display_name")),
            started_at=_s(raw.get("started_at")),
            client=_s(raw.get("client")),
            project=_s(raw.get("project")),
            start_s=_f(raw.get("start_s")),
            end_s=_f(raw.get("end_s")),
            text=_s(raw.get("text")),
            similarity=_f(raw.get("similarity")),
        )
    return UnknownHit(raw=raw)


def parse_hits(raw_results: Any) -> List[SearchHit]:
    if not isinstance(raw_results, list):
        return []
    return [parse_hit(r) for r in raw_results]


@dataclass(frozen=True)
class QASource:
    """A /qa/stream `sources` entry.

    qa_svc.retrieve() feeds off the same index as /search/semantic, so a
    source can be a document chunk too. Parsed with the same union.
    """
    hit: SearchHit


@dataclass(frozen=True)
class QAAnswer:
    answer: str
    sources: List[SearchHit]
    #: Set when the stream ended with an `event: error` frame. The
    #: partial answer (if any) is still returned alongside it.
    error: str = ""
