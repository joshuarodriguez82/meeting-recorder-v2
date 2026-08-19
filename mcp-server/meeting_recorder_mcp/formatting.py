"""Render backend payloads as text a model can cite from.

Two rules run through everything here:

1. Provenance on every fact. A session hit names the meeting, its date,
   client/project and timestamp range; a document hit names the file and
   its client. Without that, Claude can quote the archive but can't
   attribute the quote.

2. An empty result is stated, never implied. Every renderer emits an
   explicit "No ... matched" line. Blank output is indistinguishable
   from a failed call, and this repo has shipped that bug before.

Truncation is always announced inline, with the omitted amount, so the
model knows it is looking at part of a document and can ask for more.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import DocumentHit, SearchHit, SessionHit, UnknownHit

#: Per-hit snippet cap. Chunks out of the index run ~1-2k chars; this
#: keeps a 10-hit search well under ~10k chars of context.
SNIPPET_CHARS = 900
#: Transcript cap for get_session(part="transcript"). A full transcript
#: can be 36 KB+; the brief calls that out explicitly.
TRANSCRIPT_CHARS = 12_000
#: Cap for the shorter derived parts (summary / action items / ...).
PART_CHARS = 8_000


def truncate(text: str, limit: int, *, label: str = "characters") -> str:
    """Cut to `limit`, appending an explicit, countable notice."""
    text = text or ""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        text[:limit].rstrip()
        + f"\n\n[TRUNCATED by the MCP server: showing the first {limit:,} of "
          f"{len(text):,} {label}; {omitted:,} omitted.]"
    )


def fmt_time(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_date(iso: str) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM'. Unparseable input passes through."""
    if not iso:
        return "date unknown"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


def _scope(client: str, project: str = "") -> str:
    parts = [p for p in (client, project) if p]
    return " / ".join(parts) if parts else "unassigned"


# ── search ──────────────────────────────────────────────────────────

def render_search_results(
    hits: Sequence[SearchHit],
    *,
    query: str,
    client: Optional[str] = None,
    project: Optional[str] = None,
) -> str:
    scope_bits = []
    if client:
        scope_bits.append(f"client={client}")
    if project:
        scope_bits.append(f"project={project}")
    scope = f" (filtered: {', '.join(scope_bits)})" if scope_bits else ""

    sessions = [h for h in hits if isinstance(h, SessionHit)]
    documents = [h for h in hits if isinstance(h, DocumentHit)]
    unknown = [h for h in hits if isinstance(h, UnknownHit)]

    header = (
        f"Semantic search for {query!r}{scope}\n"
        f"{len(hits)} result(s): {len(sessions)} from meeting transcripts, "
        f"{len(documents)} from Knowledge Folder documents"
        + (f", {len(unknown)} unrecognised" if unknown else "")
        + "."
    )

    if not hits:
        return (
            header
            + "\n\nNo indexed content matched this query. This is an empty "
              "result, not an error — the backend answered normally. If you "
              "expected hits, check list_clients (a Knowledge Folder may not "
              "be indexed) or widen/reword the query."
        )

    lines: List[str] = [header, ""]
    for i, hit in enumerate(hits, 1):
        lines.append(_render_hit(i, hit))
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_hit(index: int, hit: SearchHit) -> str:
    if isinstance(hit, SessionHit):
        return (
            f"[{index}] MEETING — {hit.display_name or '(untitled meeting)'}\n"
            f"    date: {fmt_date(hit.started_at)}    "
            f"client/project: {_scope(hit.client, hit.project)}\n"
            f"    session_id: {hit.session_id}    "
            f"at {fmt_time(hit.start_s)}-{fmt_time(hit.end_s)}    "
            f"similarity: {hit.similarity:.3f}\n"
            f"    excerpt: {truncate(hit.text, SNIPPET_CHARS)}\n"
            f"    (cite as: \"{hit.display_name or 'meeting'}\", "
            f"{fmt_date(hit.started_at)}; full text via "
            f"get_session('{hit.session_id}'))"
        )
    if isinstance(hit, DocumentHit):
        return (
            f"[{index}] DOCUMENT — {hit.doc_name or '(unnamed file)'}\n"
            f"    client: {hit.client or 'unassigned'}    "
            f"similarity: {hit.similarity:.3f}\n"
            f"    path: {hit.doc_path or '(unknown)'}\n"
            f"    excerpt: {truncate(hit.text, SNIPPET_CHARS)}\n"
            f"    (this is a Knowledge Folder document, NOT a meeting — it "
            f"has no session_id and cannot be passed to get_session; cite it "
            f"by filename)"
        )
    return (
        f"[{index}] UNRECOGNISED RESULT SHAPE — the backend returned a hit "
        f"this server could not classify as a meeting or a document. Raw "
        f"keys: {sorted(hit.raw.keys()) if hit.raw else '(none)'}. Reporting "
        f"it rather than dropping it."
    )


# ── Q&A ─────────────────────────────────────────────────────────────

def render_answer(
    answer: str,
    sources: Sequence[SearchHit],
    *,
    question: str,
    client: Optional[str] = None,
    error: str = "",
) -> str:
    scope = f" (client={client})" if client else ""
    parts: List[str] = [f"Question{scope}: {question}", ""]

    if answer.strip():
        parts.append("Answer from the Meeting Recorder knowledge base:")
        parts.append(truncate(answer, PART_CHARS))
    else:
        parts.append(
            "The knowledge base returned no answer text." if not error else
            "The knowledge base returned no answer text before failing.")
    parts.append("")

    if sources:
        parts.append(f"Sources used ({len(sources)}):")
        for i, hit in enumerate(sources, 1):
            parts.append("  " + _render_source_line(i, hit))
    else:
        parts.append(
            "Sources used (0): the backend retrieved no matching material, "
            "so any answer above is not grounded in the archive.")

    if error:
        parts += [
            "",
            f"WARNING — the answer stream failed partway through: {error}. "
            "The text above, if any, is incomplete. Re-run the question.",
        ]
    return "\n".join(parts)


def _render_source_line(index: int, hit: SearchHit) -> str:
    if isinstance(hit, SessionHit):
        return (
            f"[{index}] MEETING \"{hit.display_name or '(untitled)'}\" "
            f"({fmt_date(hit.started_at)}, {_scope(hit.client, hit.project)}) "
            f"at {fmt_time(hit.start_s)}-{fmt_time(hit.end_s)} "
            f"— session_id: {hit.session_id}"
        )
    if isinstance(hit, DocumentHit):
        return (
            f"[{index}] DOCUMENT \"{hit.doc_name or '(unnamed)'}\" "
            f"(client: {hit.client or 'unassigned'}) — {hit.doc_path or ''}"
        )
    return f"[{index}] UNRECOGNISED SOURCE SHAPE: {sorted(hit.raw.keys())}"


# ── clients ─────────────────────────────────────────────────────────

def render_clients(rows: Sequence[Dict[str, Any]], index: Dict[str, Any]) -> str:
    if not rows:
        return (
            "No clients are configured in Meeting Recorder. This is an empty "
            "result, not an error. Clients are created by tagging a session "
            "with a client name, or from Settings -> Clients in the app."
        )

    lines = [f"{len(rows)} client(s) configured.", ""]
    for row in rows:
        name = row.get("client") or "(unnamed)"
        display = row.get("display_name") or name
        title = f"{display}" + (f"  [key: {name}]" if display != name else "")
        lines.append(f"- {title}")
        lines.append(
            f"    designated (export) folder: "
            f"{row.get('export_folder') or '(not set)'}")
        kf = row.get("knowledge_folder") or ""
        lines.append(f"    knowledge folder: {kf or '(not set)'}")
        if row.get("knowledge_error"):
            lines.append(
                f"    knowledge status: UNAVAILABLE — {row['knowledge_error']}")
        elif kf:
            reachable = ("yes" if row.get("folder_present")
                         else "NO — the path is missing or on a "
                              "disconnected/unsynced drive")
            lines.append(f"    folder reachable: {reachable}")
            lines.append(
                f"    indexed documents: {row.get('indexed_documents', 0)}"
                f"    chunks: {row.get('total_chunks', 0)}")
            if row.get("double_indexing_risk"):
                lines.append(
                    "    WARNING: this client's knowledge folder is the same "
                    "as (or inside) its designated export folder, so exported "
                    "transcripts are indexed twice — once as meetings, once "
                    "as documents. Searches will return near-duplicates.")
        else:
            lines.append(
                "    indexed documents: 0 (no knowledge folder configured)")
        lines.append("")

    if index:
        lines.append(
            f"Semantic index: {index.get('indexed_sessions', 0)} of "
            f"{index.get('total_sessions', 0)} sessions embedded"
            + (f", model {index.get('model_id')}" if index.get("model_id") else "")
            + ("" if index.get("available", True)
               else " — INDEX UNAVAILABLE (sentence-transformers not "
                    "installed in the app's backend, so semantic search and "
                    "Q&A will return nothing)")
            + "."
        )
    return "\n".join(lines).rstrip()


# ── sessions ────────────────────────────────────────────────────────

def render_session_list(
    rows: Sequence[Dict[str, Any]],
    *,
    client: Optional[str] = None,
    project: Optional[str] = None,
    limit: int,
    total_before_limit: int,
) -> str:
    scope_bits = []
    if client:
        scope_bits.append(f"client={client}")
    if project:
        scope_bits.append(f"project={project}")
    scope = f" (filtered: {', '.join(scope_bits)})" if scope_bits else ""

    if not rows:
        return (
            f"No sessions matched{scope}. This is an empty result, not an "
            f"error — the backend answered normally. Filters are matched "
            f"exactly; use list_clients to see the exact client names."
        )

    header = f"{len(rows)} session(s){scope}, newest first"
    if total_before_limit > len(rows):
        header += f" (showing {len(rows)} of {total_before_limit}; limit={limit})"
    lines = [header + ".", ""]
    for row in rows:
        lines.append(_render_session_row(row))
    return "\n".join(lines).rstrip()


def _render_session_row(row: Dict[str, Any]) -> str:
    """One session summary. Keys come from
    backend/services/session_service.py::_build_summary."""
    duration = row.get("duration_s") or 0
    # Which parts get_session can actually return for this session.
    # Saying so up front stops the model burning a call on an
    # extraction that was never run.
    have = [
        label for key, label in (
            ("has_transcript", "transcript"),
            ("has_summary", "summary"),
            ("has_action_items", "action_items"),
            ("has_decisions", "decisions"),
            ("has_requirements", "requirements"),
        ) if row.get(key)
    ]
    warnings = []
    if row.get("audio_integrity_warning"):
        warnings.append(f"audio integrity: {row['audio_integrity_warning']}")
    if row.get("processing_error"):
        warnings.append(f"processing failed: {row['processing_error']}")
    warn_line = f"\n    WARNING — {'; '.join(warnings)}" if warnings else ""
    return (
        f"- {row.get('display_name') or '(untitled)'}\n"
        f"    session_id: {row.get('session_id') or '(missing)'}\n"
        f"    started: {fmt_date(row.get('started_at') or '')}    "
        f"duration: {fmt_time(duration)}    "
        f"client/project: "
        f"{_scope(row.get('client') or '', row.get('project') or '')}\n"
        f"    available parts: {', '.join(have) if have else '(none — unprocessed)'}"
        f"{warn_line}\n"
    )


#: get_session parts -> (session JSON key, human label).
SESSION_PARTS: Dict[str, str] = {
    "transcript": "Transcript",
    "summary": "Summary",
    "action_items": "Action items",
    "decisions": "Decisions",
    "requirements": "Requirements",
    "metadata": "Metadata",
}


def render_session_part(session: Dict[str, Any], part: str) -> str:
    session_id = session.get("session_id") or "(unknown)"
    head = (
        f"Meeting: {session.get('display_name') or '(untitled)'}\n"
        f"session_id: {session_id}\n"
        f"date: {fmt_date(session.get('started_at') or '')}\n"
        f"client/project: "
        f"{_scope(session.get('client') or '', session.get('project') or '')}\n"
        f"part: {part}\n"
    )

    if part == "metadata":
        return head + "\n" + _render_metadata(session)

    if part == "transcript":
        body = _render_transcript(session)
        if not body:
            return head + (
                "\nNo transcript on this session. This is an empty field, "
                "not a failed call — the session record loaded fine but has "
                "no segments yet (still recording, or never processed)."
            )
        return head + "\n" + truncate(body, TRANSCRIPT_CHARS)

    value = session.get(part)
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return head + (
            f"\nThis session has no {SESSION_PARTS.get(part, part)} content. "
            f"This is an empty field, not a failed call — the session loaded "
            f"but that extraction hasn't been run (process the session in the "
            f"app to generate it)."
        )
    return head + "\n" + truncate(text, PART_CHARS)


def _speaker_names(session: Dict[str, Any]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    speakers = session.get("speakers")
    if isinstance(speakers, dict):
        for sid, data in speakers.items():
            if isinstance(data, dict):
                names[sid] = data.get("display_name") or sid
            else:
                names[sid] = str(data)
    elif isinstance(speakers, list):
        for entry in speakers:
            if isinstance(entry, dict):
                sid = entry.get("speaker_id") or entry.get("id") or ""
                if sid:
                    names[sid] = entry.get("display_name") or sid
    return names


def _render_transcript(session: Dict[str, Any]) -> str:
    segments = session.get("segments")
    if not isinstance(segments, list) or not segments:
        return ""
    names = _speaker_names(session)
    lines: List[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        sid = seg.get("speaker_id") or ""
        who = names.get(sid, sid or "Unknown")
        lines.append(
            f"[{fmt_time(seg.get('start') or 0)} -> "
            f"{fmt_time(seg.get('end') or 0)}] {who}: {seg.get('text') or ''}")
    return "\n".join(lines)


def _render_metadata(session: Dict[str, Any]) -> str:
    segments = session.get("segments")
    seg_count = len(segments) if isinstance(segments, list) else 0
    names = _speaker_names(session)
    available = [
        label for key, label in SESSION_PARTS.items()
        if key not in ("metadata", "transcript")
        and isinstance(session.get(key), str) and session[key].strip()
    ]
    if seg_count:
        available.insert(0, "Transcript")
    attendees = session.get("attendees")
    lines = [
        f"ended: {fmt_date(session.get('ended_at') or '')}",
        f"template: {session.get('template') or '(none)'}",
        f"transcript segments: {seg_count}",
        f"speakers: {', '.join(sorted(names.values())) or '(none identified)'}",
        f"attendees: "
        f"{', '.join(attendees) if isinstance(attendees, list) and attendees else '(none recorded)'}",
        f"populated parts: {', '.join(available) if available else '(none — session is unprocessed)'}",
    ]
    if session.get("audio_integrity_warning"):
        lines.append(
            f"AUDIO INTEGRITY WARNING: {session['audio_integrity_warning']} "
            f"— the recording may be incomplete, so treat the transcript as "
            f"partial.")
    if session.get("notes"):
        lines.append("")
        lines.append("user notes:")
        lines.append(truncate(str(session["notes"]), 2000))
    return "\n".join(lines)


def bullet_list(items: Iterable[str]) -> str:
    return "\n".join(f"  - {i}" for i in items)
