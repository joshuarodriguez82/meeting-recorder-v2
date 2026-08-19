"""MCP server exposing the Meeting Recorder archive to Claude.

Transport: stdio (what Claude Desktop and Claude Code both speak).
SDK: the official Anthropic `mcp` Python SDK, 2.x. In 2.0 the high-level
server class was renamed FastMCP -> MCPServer and moved to
`mcp.server.mcpserver`; `mcp.server.fastmcp` no longer exists, so code
written against the 1.x import will not run here.

Every tool is read-only. Nothing exposed here deletes a session, writes
a setting, or kicks off indexing — a reindex over a large Knowledge
Folder runs for minutes and would look like a hung tool call.

Every tool returns a plain string, and returns one on failure too:
a raised exception reaches the model as a protocol-level error with no
guidance, while "Meeting Recorder isn't running - start the app" is
something Claude can act on and relay. The one thing that must never
happen is a failure rendering as an empty result, so failure strings
always start with a MEETING RECORDER ERROR banner.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .client import MeetingRecorderClient
from .errors import MeetingRecorderError
from .formatting import (
    SESSION_PARTS,
    render_answer,
    render_clients,
    render_search_results,
    render_session_list,
    render_session_part,
)

logger = logging.getLogger("meeting_recorder_mcp")

SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
Read-only access to the user's Meeting Recorder archive: recorded
meeting transcripts, AI summaries, action items, decisions and
requirements, plus per-client Knowledge Folder documents — all through
the app's own semantic index.

Two kinds of material live in that index and they are NOT
interchangeable:
  * MEETINGS — recorded sessions. They have a session_id, a date, and
    timestamps. Only these can be passed to get_session.
  * DOCUMENTS — files from a client's Knowledge Folder (SOWs,
    estimates, RFPs, notes). They have a filename and a path, no
    session_id. Cite them by filename.

search_meetings labels every hit as one or the other. Preserve that
distinction when citing.

The backend only runs while the Meeting Recorder desktop app is open.
If a tool reports the app isn't running, tell the user to start it
rather than retrying.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=False)

def _log_level() -> str:
    level = (os.environ.get("MEETING_RECORDER_MCP_LOG_LEVEL") or "WARNING").upper()
    return level if level in (
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "WARNING"


server: MCPServer = MCPServer(
    name="meeting-recorder",
    title="Meeting Recorder",
    version=SERVER_VERSION,
    instructions=INSTRUCTIONS,
    # The SDK calls configure_logging(log_level) inside run(), and its
    # default of INFO makes httpx log one "HTTP Request: ..." line per
    # backend call to stderr. Harmless to the protocol (stdout is the
    # wire) but noisy in Claude Desktop's MCP log pane, so default to
    # WARNING and let MEETING_RECORDER_MCP_LOG_LEVEL turn it up for
    # debugging.
    log_level=_log_level(),
)

#: Overridden by tests to inject a stubbed client.
_client_factory: Callable[[], MeetingRecorderClient] = MeetingRecorderClient


def set_client_factory(factory: Callable[[], MeetingRecorderClient]) -> None:
    global _client_factory
    _client_factory = factory


def _error(exc: Exception) -> str:
    """Render any failure as actionable text.

    Banner first so an error can never be mistaken for a result, and so
    a user scanning the transcript can see at a glance that the call
    failed rather than found nothing.
    """
    if isinstance(exc, MeetingRecorderError):
        return f"MEETING RECORDER ERROR — {exc.message}"
    logger.exception("Unexpected failure in a Meeting Recorder MCP tool")
    return (
        "MEETING RECORDER ERROR — unexpected failure in the MCP server "
        f"itself ({type(exc).__name__}: {exc}). This is a bug in the MCP "
        "server, not in the Meeting Recorder app. Nothing was read or "
        "changed."
    )


# ── tools ───────────────────────────────────────────────────────────

@server.tool(
    name="search_meetings",
    title="Search meetings and documents",
    annotations=READ_ONLY,
    description=(
        "Semantic search across every indexed meeting transcript AND every "
        "indexed Knowledge Folder document. Results are labelled MEETING or "
        "DOCUMENT: meetings carry a session_id, a date and timestamps; "
        "documents carry a filename and path and have no session_id. Use "
        "this first for any question about what was said, agreed, or "
        "written down."
    ),
)
async def search_meetings(
    query: str,
    client: Optional[str] = None,
    project: Optional[str] = None,
    top_k: int = 10,
) -> str:
    """Args:
    query: What to look for, in natural language.
    client: Optional exact client name to restrict to (see list_clients).
    project: Optional exact project name. Note: a project filter excludes
        Knowledge Folder documents entirely, because documents are scoped
        to a client and have no project.
    top_k: How many hits to return (1-50, default 10).
    """
    try:
        api = _client_factory()
        hits = await api.semantic_search(
            query, top_k=top_k, client=client, project=project)
        return render_search_results(
            hits, query=query, client=client, project=project)
    except Exception as exc:  # noqa: BLE001 — every failure becomes text
        return _error(exc)


@server.tool(
    name="ask_knowledge_base",
    title="Ask the meeting knowledge base",
    annotations=READ_ONLY,
    description=(
        "Ask a natural-language question and get an answer synthesised by "
        "the Meeting Recorder app from its own index, together with the "
        "meetings and documents it drew on. Use this for cross-meeting "
        "questions ('what did we commit to for ACME?'). For raw excerpts "
        "to quote, prefer search_meetings. Requires an AI provider "
        "configured in the app; if none is, this reports that clearly."
    ),
)
async def ask_knowledge_base(
    question: str,
    client: Optional[str] = None,
    top_k: int = 8,
) -> str:
    """Args:
    question: The question, in natural language.
    client: Optional exact client name to restrict the retrieval to.
    top_k: How many source chunks to retrieve for grounding (1-20).
    """
    try:
        api = _client_factory()
        result = await api.ask(question, top_k=top_k, client=client)
        return render_answer(
            result.answer, result.sources,
            question=question, client=client, error=result.error)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@server.tool(
    name="list_clients",
    title="List clients and Knowledge Folder status",
    annotations=READ_ONLY,
    description=(
        "List every configured client with its designated (export) folder, "
        "its Knowledge Folder, whether that folder is currently reachable, "
        "and how many documents and chunks are indexed for it. Also reports "
        "overall semantic-index coverage. Use this to get exact client "
        "names before filtering other tools."
    ),
)
async def list_clients() -> str:
    try:
        api = _client_factory()
        configs = await api.client_configs()
        rows: List[Dict[str, Any]] = []
        for name, cfg in sorted((configs or {}).items()):
            cfg = cfg if isinstance(cfg, dict) else {}
            row: Dict[str, Any] = {
                "client": name,
                "display_name": cfg.get("display_name") or name,
                "export_folder": cfg.get("export_folder") or "",
                "knowledge_folder": cfg.get("knowledge_folder") or "",
            }
            if row["knowledge_folder"]:
                try:
                    row.update(await api.client_knowledge(name))
                except MeetingRecorderError as exc:
                    # One client's knowledge lookup failing must not
                    # take down the whole listing — same isolation the
                    # backend applies to a bad recordings root.
                    row["knowledge_error"] = exc.message
            row["double_indexing_risk"] = _same_folder(
                row["export_folder"], row["knowledge_folder"])
            rows.append(row)

        try:
            index = await api.index_status()
        except MeetingRecorderError:
            index = {}
        return render_clients(rows, index)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@server.tool(
    name="list_sessions",
    title="List recent meetings",
    annotations=READ_ONLY,
    description=(
        "List recorded meetings, newest first, with their session_id, date, "
        "duration, client/project and which parts (transcript, summary, "
        "action items, decisions, requirements) actually exist. Use it to "
        "find a session_id for get_session, or to see what was recorded in "
        "a period. Filters match client/project names exactly."
    ),
)
async def list_sessions(
    client: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Args:
    client: Optional exact client name filter.
    project: Optional exact project name filter.
    limit: Maximum sessions to return (1-200, default 20).
    """
    try:
        limit = max(1, min(200, int(limit)))
        api = _client_factory()
        rows = await api.list_sessions()
        # The backend has no filtered variant of GET /sessions, so
        # filtering happens here. Exact match, matching how the backend
        # filters semantic search.
        if client:
            rows = [r for r in rows if (r.get("client") or "") == client]
        if project:
            rows = [r for r in rows if (r.get("project") or "") == project]
        total = len(rows)
        return render_session_list(
            rows[:limit], client=client, project=project,
            limit=limit, total_before_limit=total)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@server.tool(
    name="get_session",
    title="Get one meeting's content",
    annotations=READ_ONLY,
    description=(
        "Fetch one part of a single recorded meeting by session_id: "
        "'transcript', 'summary', 'action_items', 'decisions', "
        "'requirements', or 'metadata'. Long transcripts are truncated and "
        "the truncation is stated inline. Only meetings have a session_id — "
        "a DOCUMENT hit from search_meetings cannot be fetched here."
    ),
)
async def get_session(session_id: str, part: str = "summary") -> str:
    """Args:
    session_id: The meeting's id, from list_sessions or a MEETING hit.
    part: One of transcript, summary, action_items, decisions,
        requirements, metadata. Defaults to summary.
    """
    part = (part or "summary").strip().lower()
    if part not in SESSION_PARTS:
        return (
            f"MEETING RECORDER ERROR — unknown part {part!r}. Valid parts: "
            f"{', '.join(sorted(SESSION_PARTS))}."
        )
    try:
        api = _client_factory()
        session = await api.get_session(session_id)
        return render_session_part(session, part)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ── helpers ─────────────────────────────────────────────────────────

def _same_folder(export_folder: str, knowledge_folder: str) -> bool:
    """True when the Knowledge Folder is the export folder, or inside it.

    Why this matters: the export ("Designated") folder receives
    transcript_*.txt / summary_*.txt / action_items_*.txt /
    decisions_*.txt / requirements_*.txt per session
    (backend/services/export_service.py). `.txt` is in
    document_service._PLAIN_TEXT_EXTENSIONS, and index_folder() walks
    the Knowledge Folder with rglob("*") excluding only dot-prefixed
    paths — nothing excludes exported transcripts. So pointing both at
    one directory indexes every transcript twice: once as session
    chunks, once as document chunks, and searches return near-duplicate
    hits under two different labels.

    Purely a path comparison — no filesystem access, since the folders
    named in the config live on the user's machine, not necessarily on
    the machine running this server.
    """
    if not export_folder or not knowledge_folder:
        return False
    try:
        exp = os.path.normcase(os.path.normpath(os.path.expanduser(export_folder)))
        knw = os.path.normcase(os.path.normpath(os.path.expanduser(knowledge_folder)))
    except (TypeError, ValueError):
        return False
    if exp == knw:
        return True
    # Knowledge folder nested under the export folder is the same trap:
    # index_folder recurses, so it would still be indexing exports.
    return knw.startswith(exp.rstrip("/\\") + os.sep)


def main() -> None:
    # stdout is the MCP wire — anything printed there corrupts the
    # JSON-RPC framing. basicConfig's default handler writes to stderr,
    # which is what we want; never add a stdout handler here.
    logging.basicConfig(
        level=_log_level(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
