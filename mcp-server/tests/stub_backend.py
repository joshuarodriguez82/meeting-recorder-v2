"""An httpx.MockTransport standing in for backend/server.py.

Shapes are copied from the real thing, not invented:

  /search/semantic          backend/services/search_service.py::search
                            (the session|document discriminated union)
  /qa/stream                backend/server.py::qa_stream (SSE framing)
  /clients/config           backend/server.py::get_client_configs
  /clients/{c}/knowledge    backend/server.py::get_client_knowledge_status
  /sessions                 session_service.py::_build_summary
  /sessions/{id}            session_service.py::load (raw session JSON)
  401 body                  backend/server.py::require_backend_token
                            (RFC 7807 application/problem+json)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

VALID_TOKEN = "a" * 64

SESSION_HIT = {
    "source": "session",
    "session_id": "session_20260714_101500",
    "display_name": "ACME — Discovery Call",
    "started_at": "2026-07-14T10:15:00",
    "client": "ACME",
    "project": "CCaaS Migration",
    "start_s": 412.5,
    "end_s": 448.0,
    "text": "We agreed the cutover window is the first weekend in October, "
            "and Priya owns the Genesys decommission plan.",
    "similarity": 0.8123,
}

# No session_id / display_name / started_at / start_s / end_s. This is
# the shape that once got rendered as an unopenable "Untitled" session.
DOCUMENT_HIT = {
    "source": "document",
    "doc_name": "ACME_SOW_v3.docx",
    "doc_path": "/Users/j/Knowledge/ACME/ACME_SOW_v3.docx",
    "client": "ACME",
    "text": "Cutover is scheduled for the first weekend of October 2026. "
            "Northwind Digital provides 40 hours of hypercare.",
    "similarity": 0.7710,
}

# A session hit from a backend predating the additive `source` field.
LEGACY_SESSION_HIT = {k: v for k, v in SESSION_HIT.items() if k != "source"}
LEGACY_SESSION_HIT["session_id"] = "session_20260610_090000"
LEGACY_SESSION_HIT["display_name"] = "ACME — Kickoff"

CLIENT_CONFIGS = {
    "ACME": {
        "export_folder": "/Users/j/Drive/ACME/Exports",
        "knowledge_folder": "/Users/j/Knowledge/ACME",
        "display_name": "ACME Health",
    },
    # The double-indexing trap: knowledge folder == export folder.
    "Globex": {
        "export_folder": "/Users/j/Drive/Globex/Exports",
        "knowledge_folder": "/Users/j/Drive/Globex/Exports",
        "display_name": "Globex",
    },
    "Initech": {
        "export_folder": "",
        "knowledge_folder": "",
        "display_name": "Initech",
    },
}

KNOWLEDGE = {
    "ACME": {
        "client": "ACME",
        "knowledge_folder": "/Users/j/Knowledge/ACME",
        "folder_present": True,
        "indexed_documents": 12,
        "total_chunks": 341,
    },
    "Globex": {
        "client": "Globex",
        "knowledge_folder": "/Users/j/Drive/Globex/Exports",
        "folder_present": False,
        "indexed_documents": 88,
        "total_chunks": 1904,
    },
}

SESSION_SUMMARIES: List[Dict[str, Any]] = [
    {
        "session_id": "session_20260714_101500",
        "display_name": "ACME — Discovery Call",
        "started_at": "2026-07-14T10:15:00",
        "ended_at": "2026-07-14T11:02:00",
        "duration_s": 2820,
        "client": "ACME",
        "project": "CCaaS Migration",
        "has_transcript": True,
        "has_summary": True,
        "has_action_items": True,
        "has_decisions": True,
        "has_requirements": False,
        "audio_integrity_warning": None,
        "processing_error": None,
    },
    {
        "session_id": "session_20260702_140000",
        "display_name": "Globex — Weekly Sync",
        "started_at": "2026-07-02T14:00:00",
        "ended_at": "2026-07-02T14:31:00",
        "duration_s": 1860,
        "client": "Globex",
        "project": "",
        "has_transcript": True,
        "has_summary": False,
        "has_action_items": False,
        "has_decisions": False,
        "has_requirements": False,
        "audio_integrity_warning": "WAV is 12m shorter than the recording window",
        "processing_error": None,
    },
    {
        "session_id": "session_20260610_090000",
        "display_name": "ACME — Kickoff",
        "started_at": "2026-06-10T09:00:00",
        "ended_at": "2026-06-10T10:00:00",
        "duration_s": 3600,
        "client": "ACME",
        "project": "CCaaS Migration",
        "has_transcript": True,
        "has_summary": True,
        "has_action_items": False,
        "has_decisions": False,
        "has_requirements": True,
        "audio_integrity_warning": None,
        "processing_error": None,
    },
]

FULL_SESSION: Dict[str, Any] = {
    "session_id": "session_20260714_101500",
    "display_name": "ACME — Discovery Call",
    "started_at": "2026-07-14T10:15:00",
    "ended_at": "2026-07-14T11:02:00",
    "client": "ACME",
    "project": "CCaaS Migration",
    "template": "Discovery",
    "attendees": ["priya@acme.example", "josh@northwind.example"],
    "notes": "Priya joined 10 minutes late; the Genesys license question "
             "is still open.",
    "speakers": {
        "SPEAKER_00": {"display_name": "Josh Rodriguez"},
        "SPEAKER_01": {"display_name": "Priya Raman"},
    },
    "segments": [
        {"speaker_id": "SPEAKER_00", "start": 0.0, "end": 6.2,
         "text": "Thanks for making time — let's start with the current "
                 "Genesys footprint."},
        {"speaker_id": "SPEAKER_01", "start": 6.4, "end": 19.8,
         "text": "We're on Genesys Cloud CX 3, about 400 concurrent agents "
                 "across four sites."},
        {"speaker_id": "SPEAKER_00", "start": 412.5, "end": 448.0,
         "text": "We agreed the cutover window is the first weekend in "
                 "October, and Priya owns the Genesys decommission plan."},
    ],
    "summary": "ACME is migrating 400 agents off Genesys Cloud CX 3 to "
               "Amazon Connect. Cutover targeted for the first weekend of "
               "October 2026.",
    "action_items": "- Priya: produce the Genesys decommission plan (due "
                    "2026-08-01)\n- Josh: size the hypercare window",
    "decisions": "- Cutover window: first weekend of October 2026\n"
                 "- Single Connect instance, four routing profiles",
    "requirements": None,
}

QA_SOURCES = [SESSION_HIT, DOCUMENT_HIT]
QA_ANSWER_FRAGMENTS = [
    "ACME's cutover is the first weekend of October 2026. ",
    "Priya Raman owns the Genesys decommission plan, and the SOW "
    "commits 40 hours of hypercare.",
]


def _problem(status: int, title: str, detail: str, path: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/problem+json"},
        content=json.dumps({
            "type": f"tag:meeting-recorder/errors/{title.lower().replace(' ', '-')}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": path,
        }).encode(),
    )


def make_transport(
    *,
    token: str = VALID_TOKEN,
    search_results: Optional[List[Dict[str, Any]]] = None,
    qa_error: Optional[str] = None,
    knowledge_status: Optional[int] = None,
) -> httpx.MockTransport:
    """A MockTransport that behaves like the real backend.

    token: the value the stub will accept; anything else gets the real
        401 problem+json body.
    """
    results = (search_results if search_results is not None
               else [SESSION_HIT, DOCUMENT_HIT, LEGACY_SESSION_HIT])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        presented = request.headers.get("authorization", "")
        if path != "/health" and presented != f"Bearer {token}":
            return _problem(401, "Unauthorized",
                            "Missing or invalid backend auth token.", path)

        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "2.0.0"})

        if path == "/search/index/status":
            return httpx.Response(200, json={
                "available": True, "total_sessions": 73,
                "indexed_sessions": 71,
                "model_id": "sentence-transformers/all-MiniLM-L6-v2"})

        if path == "/search/semantic":
            body = json.loads(request.content or b"{}")
            hits = list(results)
            if body.get("client"):
                hits = [h for h in hits if h.get("client") == body["client"]]
            if body.get("project"):
                hits = [h for h in hits
                        if h.get("source") != "document"
                        and h.get("project") == body["project"]]
            return httpx.Response(200, json={
                "results": hits[: body.get("top_k", 10)],
                "query": body.get("query", "")})

        if path == "/clients/config":
            return httpx.Response(200, json=CLIENT_CONFIGS)

        if path.startswith("/clients/") and path.endswith("/knowledge"):
            name = path[len("/clients/"):-len("/knowledge")]
            from urllib.parse import unquote
            name = unquote(name)
            if knowledge_status:
                return _problem(knowledge_status, "Service Unavailable",
                                "client_configs.json hasn't downloaded yet.",
                                path)
            if name in KNOWLEDGE:
                return httpx.Response(200, json=KNOWLEDGE[name])
            return httpx.Response(200, json={
                "client": name, "knowledge_folder": "", "folder_present": False,
                "indexed_documents": 0, "total_chunks": 0})

        if path == "/sessions":
            return httpx.Response(200, json=SESSION_SUMMARIES)

        if path.startswith("/sessions/"):
            from urllib.parse import unquote
            sid = unquote(path[len("/sessions/"):])
            if sid == FULL_SESSION["session_id"]:
                return httpx.Response(200, json=FULL_SESSION)
            return _problem(404, "Not Found", "Session not found", path)

        if path == "/qa/stream":
            frames = [": connected\n\n",
                      "event: sources\ndata: " + json.dumps(QA_SOURCES) + "\n\n"]
            frames += ["data: " + json.dumps({"text": f}) + "\n\n"
                       for f in QA_ANSWER_FRAGMENTS]
            if qa_error:
                frames.append(
                    "event: error\ndata: " + json.dumps({"error": qa_error}) + "\n\n")
            else:
                frames.append("event: done\ndata: \n\n")
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content="".join(frames).encode())

        return _problem(404, "Not Found", f"no stub route for {path}", path)

    return httpx.MockTransport(handler)


def unreachable_transport() -> httpx.MockTransport:
    """Backend down: the connection itself fails, as it does when the
    Meeting Recorder app isn't running."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed",
                                 request=request)
    return httpx.MockTransport(handler)


def timeout_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)
    return httpx.MockTransport(handler)


def qa_unavailable_transport() -> httpx.MockTransport:
    """/qa/stream 409 — the app has no AI provider configured."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _problem(
            409, "Conflict",
            "Q&A needs both the semantic-search index and an AI provider "
            "configured. Save an Anthropic / OpenRouter / Ollama key in "
            "Settings -> AI Provider, then try again.",
            request.url.path)
    return httpx.MockTransport(handler)
