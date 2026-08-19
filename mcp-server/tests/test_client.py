"""API client against the stubbed backend."""

from __future__ import annotations

import pytest

from meeting_recorder_mcp.client import MeetingRecorderClient
from meeting_recorder_mcp.errors import (
    BackendTimeout,
    BackendUnauthorized,
    BackendUnavailable,
    BackendUnreachable,
    SessionNotFound,
    TokenUnavailable,
)
from meeting_recorder_mcp.models import DocumentHit, SessionHit, UnknownHit
from tests import stub_backend
from tests.conftest import STUB_LOCATION, make_client


# ── the discriminated union ─────────────────────────────────────────

async def test_search_splits_sessions_from_documents(client):
    hits = await client.semantic_search("cutover window")
    kinds = [type(h).__name__ for h in hits]
    assert kinds == ["SessionHit", "DocumentHit", "SessionHit"]

    session, document, legacy = hits
    assert isinstance(session, SessionHit)
    assert session.session_id == "session_20260714_101500"
    assert session.start_s == 412.5

    # The bug this repo already shipped once: a document hit has no
    # session_id at all. It must not become a SessionHit.
    assert isinstance(document, DocumentHit)
    assert document.doc_name == "ACME_SOW_v3.docx"
    assert not hasattr(document, "session_id")

    # A pre-`source` backend still yields a session hit.
    assert isinstance(legacy, SessionHit)
    assert legacy.session_id == "session_20260610_090000"


async def test_unclassifiable_hit_is_surfaced_not_dropped():
    transport = stub_backend.make_transport(
        search_results=[{"weird": "shape", "similarity": 0.4}])
    api = make_client(transport)
    hits = await api.semantic_search("anything")
    assert len(hits) == 1
    assert isinstance(hits[0], UnknownHit)
    assert hits[0].raw["weird"] == "shape"


async def test_empty_results_are_an_empty_list_not_an_error():
    api = make_client(stub_backend.make_transport(search_results=[]))
    assert await api.semantic_search("nothing matches this") == []


async def test_client_filter_is_forwarded(client):
    hits = await client.semantic_search("cutover", client="Nobody")
    assert hits == []
    hits = await client.semantic_search("cutover", client="ACME")
    assert len(hits) == 3


async def test_top_k_is_clamped_to_the_backend_range(client):
    hits = await client.semantic_search("cutover", top_k=999)
    assert len(hits) == 3  # stub only has 3; the point is it didn't 422
    hits = await client.semantic_search("cutover", top_k=1)
    assert len(hits) == 1


# ── failure modes ───────────────────────────────────────────────────

async def test_backend_down_says_start_the_app():
    api = make_client(stub_backend.unreachable_transport())
    with pytest.raises(BackendUnreachable) as excinfo:
        await api.semantic_search("anything")
    message = excinfo.value.message
    assert "isn't running" in message
    assert "Start the Meeting Recorder app" in message
    assert "17645" in message  # names the port it tried


async def test_bad_token_says_the_token_and_where_it_came_from():
    api = make_client(stub_backend.make_transport(token="a-different-token"))
    with pytest.raises(BackendUnauthorized) as excinfo:
        await api.semantic_search("anything")
    message = excinfo.value.message
    assert "rejected the auth token" in message
    assert "extension-token" in message
    # The two failures must be distinguishable to a reader.
    assert "isn't running" not in message


async def test_missing_token_never_sends_a_request(monkeypatch, tmp_path):
    monkeypatch.delenv("MEETING_RECORDER_TOKEN", raising=False)
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    with pytest.raises(TokenUnavailable) as excinfo:
        MeetingRecorderClient()
    assert "No Meeting Recorder auth token found" in excinfo.value.message
    assert "extension-token" in excinfo.value.message


async def test_timeout_is_its_own_failure():
    api = make_client(stub_backend.timeout_transport())
    with pytest.raises(BackendTimeout) as excinfo:
        await api.semantic_search("anything")
    assert "didn't answer within" in excinfo.value.message


async def test_missing_session_is_not_a_generic_error(client):
    with pytest.raises(SessionNotFound) as excinfo:
        await client.get_session("session_does_not_exist")
    assert "document hits from search_meetings have no session_id" \
        in excinfo.value.message.lower()


async def test_409_relays_the_backends_own_actionable_detail():
    api = make_client(stub_backend.qa_unavailable_transport())
    with pytest.raises(BackendUnavailable) as excinfo:
        await api.ask("what did we decide?")
    assert "Settings -> AI Provider" in excinfo.value.message


async def test_health_is_reachable_even_with_a_bad_token():
    # /health is auth-exempt in backend/server.py, which is exactly why
    # it can't be the auth probe.
    api = make_client(stub_backend.make_transport(token="other"))
    assert (await api.health())["status"] == "ok"
    with pytest.raises(BackendUnauthorized):
        await api.verify()


# ── SSE / Q&A ───────────────────────────────────────────────────────

async def test_qa_stream_is_assembled_with_typed_sources(client):
    result = await client.ask("when is the ACME cutover?")
    assert result.answer.startswith("ACME's cutover is the first weekend")
    assert result.answer.endswith("40 hours of hypercare.")
    assert result.error == ""
    assert [type(s).__name__ for s in result.sources] == \
        ["SessionHit", "DocumentHit"]


async def test_qa_error_frame_keeps_the_partial_answer():
    api = make_client(stub_backend.make_transport(qa_error="model rate-limited"))
    result = await api.ask("when is the ACME cutover?")
    assert result.error == "model rate-limited"
    assert result.answer  # partial text is preserved, not discarded


# ── other endpoints ─────────────────────────────────────────────────

async def test_client_configs_and_knowledge(client):
    configs = await client.client_configs()
    assert set(configs) == {"ACME", "Globex", "Initech"}
    knowledge = await client.client_knowledge("ACME")
    assert knowledge["indexed_documents"] == 12


async def test_client_name_is_url_encoded(client):
    # A client called "R&D / EMEA" must not break the path.
    knowledge = await client.client_knowledge("R&D / EMEA")
    assert knowledge["client"] == "R&D / EMEA"


async def test_list_and_get_session(client):
    rows = await client.list_sessions()
    assert len(rows) == 3
    full = await client.get_session("session_20260714_101500")
    assert full["display_name"] == "ACME — Discovery Call"
    assert len(full["segments"]) == 3


# ── read-only guarantee ─────────────────────────────────────────────

async def test_client_never_issues_a_mutating_request():
    """Walk every public client method and assert the HTTP verb/path.

    The brief forbids deletion, settings mutation and reindexing. The
    cheapest durable guarantee is to record every request the client
    makes and check it against an allowlist.
    """
    seen = []

    import httpx

    def recorder(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return stub_backend.make_transport().handler(request)

    api = MeetingRecorderClient(STUB_LOCATION,
                                transport=httpx.MockTransport(recorder))
    await api.health()
    await api.verify()
    await api.index_status()
    await api.semantic_search("x")
    await api.client_configs()
    await api.client_knowledge("ACME")
    await api.list_sessions()
    await api.get_session("session_20260714_101500")
    await api.ask("x")

    allowed = {
        ("GET", "/health"),
        ("GET", "/search/index/status"),
        ("POST", "/search/semantic"),
        ("GET", "/clients/config"),
        ("GET", "/clients/ACME/knowledge"),
        ("GET", "/sessions"),
        ("GET", "/sessions/session_20260714_101500"),
        ("POST", "/qa/stream"),
    }
    assert set(seen) <= allowed, f"unexpected requests: {set(seen) - allowed}"
    # And nothing that could mutate or run for minutes.
    for method, path in seen:
        assert method in ("GET", "POST")
        assert "reindex" not in path
        assert "backfill" not in path
        assert "embed" not in path
