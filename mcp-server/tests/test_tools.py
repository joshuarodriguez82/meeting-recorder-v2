"""Every MCP tool, end to end, against the stubbed backend.

These call the tool functions the way the SDK does and assert on the
text a model would actually see. Run with `-s` to read the output:

    pytest tests/test_tools.py -s
"""

from __future__ import annotations

import pytest

from meeting_recorder_mcp import server as srv
from tests import stub_backend
from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _stub_factory():
    srv.set_client_factory(lambda: make_client())
    yield
    srv.set_client_factory(srv.MeetingRecorderClient)


def _use(transport):
    srv.set_client_factory(lambda: make_client(transport))


# ── search_meetings ─────────────────────────────────────────────────

async def test_search_labels_meetings_and_documents_separately():
    out = await srv.search_meetings("when is the cutover?")
    assert "3 result(s): 2 from meeting transcripts, 1 from Knowledge " \
           "Folder documents." in out
    assert "[1] MEETING — ACME — Discovery Call" in out
    assert "[2] DOCUMENT — ACME_SOW_v3.docx" in out
    # Provenance a model can cite from.
    assert "date: 2026-07-14 10:15" in out
    assert "session_id: session_20260714_101500" in out
    assert "client/project: ACME / CCaaS Migration" in out
    assert "at 06:52-07:28" in out
    # And an explicit warning that a document is not a session.
    assert "cannot be passed to get_meeting" in out


async def test_search_empty_is_clearly_empty_not_failed():
    _use(stub_backend.make_transport(search_results=[]))
    out = await srv.search_meetings("nothing at all")
    assert "0 result(s)" in out
    assert "empty result, not an error" in out
    assert "MEETING RECORDER ERROR" not in out


async def test_search_when_the_app_is_closed():
    _use(stub_backend.unreachable_transport())
    out = await srv.search_meetings("anything")
    assert out.startswith("MEETING RECORDER ERROR —")
    assert "Meeting Recorder isn't running" in out


async def test_search_with_a_rejected_token():
    _use(stub_backend.make_transport(token="stale"))
    out = await srv.search_meetings("anything")
    assert out.startswith("MEETING RECORDER ERROR —")
    assert "rejected the auth token" in out


async def test_long_excerpts_are_truncated_and_say_so():
    long_hit = dict(stub_backend.SESSION_HIT, text="word " * 4000)
    _use(stub_backend.make_transport(search_results=[long_hit]))
    out = await srv.search_meetings("x")
    assert "[TRUNCATED by the MCP server" in out
    assert "omitted.]" in out


# ── ask_knowledge_base ──────────────────────────────────────────────

async def test_ask_returns_answer_plus_typed_sources():
    out = await srv.ask_knowledge_base("when is the ACME cutover?")
    assert "Answer from the Meeting Recorder knowledge base:" in out
    assert "first weekend of October 2026" in out
    assert "Sources used (2):" in out
    assert "[1] MEETING \"ACME — Discovery Call\"" in out
    assert "[2] DOCUMENT \"ACME_SOW_v3.docx\"" in out


async def test_ask_reports_a_mid_stream_failure():
    _use(stub_backend.make_transport(qa_error="model rate-limited"))
    out = await srv.ask_knowledge_base("when is the cutover?")
    assert "WARNING — the answer stream failed partway through" in out
    assert "model rate-limited" in out


async def test_ask_without_an_ai_provider():
    _use(stub_backend.qa_unavailable_transport())
    out = await srv.ask_knowledge_base("anything")
    assert out.startswith("MEETING RECORDER ERROR —")
    assert "Settings -> AI Provider" in out


# ── list_clients ────────────────────────────────────────────────────

async def test_list_clients_reports_knowledge_counts_and_the_folder_trap():
    out = await srv.list_clients()
    assert "3 client(s) configured." in out
    assert "ACME Health" in out
    assert "indexed documents: 12    chunks: 341" in out
    # Globex points its knowledge folder at its export folder.
    assert "indexed twice" in out
    # And its folder is currently unreachable.
    assert "folder reachable: NO" in out
    # Index coverage.
    assert "71 of 73 sessions embedded" in out


async def test_list_clients_when_one_clients_knowledge_lookup_fails():
    _use(stub_backend.make_transport(knowledge_status=503))
    out = await srv.list_clients()
    # The listing survives; the one failure is named in place.
    assert "3 client(s) configured." in out
    assert "knowledge status: UNAVAILABLE" in out


# ── list_meetings ───────────────────────────────────────────────────

async def test_list_meetings_shows_ids_dates_and_available_parts():
    out = await srv.list_meetings()
    assert "3 session(s), newest first." in out
    assert "session_id: session_20260714_101500" in out
    assert "available parts: transcript, summary, action_items, decisions" in out
    assert "duration: 47:00" in out
    assert "WARNING — audio integrity" in out


async def test_list_meetings_filter_and_limit():
    out = await srv.list_meetings(client="ACME")
    assert "2 session(s) (filtered: client=ACME)" in out
    assert "Globex" not in out

    out = await srv.list_meetings(limit=1)
    assert "showing 1 of 3; limit=1" in out


async def test_list_meetings_no_match_is_empty_not_error():
    out = await srv.list_meetings(client="Nonexistent")
    assert "No sessions matched (filtered: client=Nonexistent)" in out
    assert "empty result, not an error" in out
    assert "MEETING RECORDER ERROR" not in out


# ── get_meeting ─────────────────────────────────────────────────────

@pytest.mark.parametrize("part", ["transcript", "summary", "action_items",
                                  "decisions", "metadata"])
async def test_get_meeting_parts(part):
    out = await srv.get_meeting("session_20260714_101500", part=part)
    assert "session_id: session_20260714_101500" in out
    assert f"part: {part}" in out
    assert "MEETING RECORDER ERROR" not in out


async def test_transcript_uses_speaker_names_and_timestamps():
    out = await srv.get_meeting("session_20260714_101500", part="transcript")
    assert "[00:00 -> 00:06] Sam Doe:" in out
    assert "[06:52 -> 07:28] Sam Doe:" in out
    assert "Priya Raman:" in out


async def test_absent_extraction_is_empty_not_failed():
    out = await srv.get_meeting("session_20260714_101500", part="requirements")
    assert "has no Requirements content" in out
    assert "empty field, not a failed call" in out
    assert "MEETING RECORDER ERROR" not in out


async def test_metadata_flags_populated_parts_and_notes():
    out = await srv.get_meeting("session_20260714_101500", part="metadata")
    assert "transcript segments: 3" in out
    assert "speakers: Sam Doe, Priya Raman" in out
    assert "populated parts: Transcript, Summary, Action items, Decisions" in out
    assert "user notes:" in out


async def test_unknown_part_is_rejected_with_the_valid_list():
    out = await srv.get_meeting("session_20260714_101500", part="vibes")
    assert out.startswith("MEETING RECORDER ERROR —")
    assert "action_items" in out and "transcript" in out


async def test_unknown_session_id():
    out = await srv.get_meeting("session_nope")
    assert out.startswith("MEETING RECORDER ERROR —")
    assert "No session with id 'session_nope'" in out


async def test_long_transcript_is_truncated_with_a_count():
    big = dict(stub_backend.FULL_SESSION)
    big["segments"] = [
        {"speaker_id": "SPEAKER_00", "start": i, "end": i + 1,
         "text": "This is a long line of transcript text. " * 8}
        for i in range(400)
    ]
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/sessions/"):
            return httpx.Response(200, json=big)
        return stub_backend.make_transport().handler(request)

    _use(httpx.MockTransport(handler))
    out = await srv.get_meeting("session_20260714_101500", part="transcript")
    assert "[TRUNCATED by the MCP server: showing the first 12,000 of" in out


# ── double-indexing detection ───────────────────────────────────────

@pytest.mark.parametrize("export,knowledge,expected", [
    ("/a/Exports", "/a/Exports", True),
    ("/a/Exports/", "/a/Exports", True),
    ("/a/Exports", "/a/Exports/Docs", True),   # nested: rglob still walks it
    ("/a/Exports", "/a/Knowledge", False),
    ("", "/a/Knowledge", False),
    ("/a/Exports", "", False),
    ("/a/Exports", "/a/ExportsOther", False),  # prefix but not a child
])
def test_same_folder_detection(export, knowledge, expected):
    assert srv._same_folder(export, knowledge) is expected
