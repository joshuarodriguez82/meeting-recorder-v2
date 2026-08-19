"""
Knowledge-Folder retrieval in the prep brief.

Before this, a prep brief for an upcoming client meeting read past call
summaries and completely ignored that client's SOWs, requirements docs
and notes — even though the app already indexes them into the same
embedding space (one real client: 114 documents / 1171 chunks).

What these tests pin, in the order the task cares about:

  1. Documents reach the prompt for a client that has them, and reach
     the API response as `referenced_documents`.
  2. A client with NO Knowledge Folder gets byte-for-byte the prompt it
     got before this feature existed — the strongest available
     statement of "never worse".
  3. An unreachable folder / unreadable index / missing embedding model
     degrades to exactly that same prompt. No error, no "no documents
     found" line.
  4. Document material and meeting material are attributed distinctly,
     end to end: distinct prompt headers, distinct citation forms, and
     two disjoint provenance lists on the response.
  5. The budget split holds when BOTH sources are oversupplied — 8
     sessions stay 8, documents stay inside their own separate cap, and
     one long document can't own the whole document budget.

The LLM is never called: the summarizer's `_chat` is monkeypatched to
capture the prompt string, which is the actual artifact under test.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# summarizer.py imports the anthropic SDK at module load and
# config.settings imports python-dotenv; neither is in the lightweight
# test env. Same stub pattern as test_coach_tick.py.
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core.summarizer import Summarizer  # noqa: E402
from services import prep_brief_context as ctx  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── Fakes ───────────────────────────────────────────────────────────


class FakeSearch:
    """Stands in for SearchService.

    `hits` maps a substring of the query to the ranked list that query
    should return, so a test can give different probes different
    results. `raises` makes every call blow up, which is how a
    disconnected Drive / unreadable sidecar / missing sentence-
    transformers surfaces from the real service.
    """

    def __init__(self, hits=None, raises: bool = False):
        self._hits = hits or {}
        self._raises = raises
        self.calls: list[dict] = []

    def search(self, query, top_k=10, client=None, project=None):
        self.calls.append({"query": query, "top_k": top_k,
                           "client": client, "project": project})
        if self._raises:
            raise OSError("knowledge folder is on a disconnected drive")
        for needle, results in self._hits.items():
            if needle.lower() in query.lower():
                return list(results)
        return []


def _doc(name, text, sim=0.6, path=None):
    return {
        "source": "document",
        "doc_name": name,
        "doc_path": path if path is not None else f"/kf/{name}",
        "client": "ACME",
        "text": text,
        "similarity": sim,
    }


def _session_hit(sid="S1", text="we said this out loud", sim=0.9):
    """A session chunk — must never be treated as document context."""
    return {
        "source": "session",
        "session_id": sid,
        "display_name": "Call",
        "started_at": "2026-01-01T00:00:00",
        "client": "ACME",
        "project": "",
        "start_s": 0.0,
        "end_s": 10.0,
        "text": text,
        "similarity": sim,
    }


def _summarizer_capturing() -> tuple[Summarizer, list[str]]:
    s = Summarizer(api_key="x", model="claude-haiku-4-5", provider="anthropic")
    prompts: list[str] = []

    async def _fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return "## The story so far\n- brief body"

    s._chat = _fake_chat  # type: ignore[assignment]
    return s, prompts


# ── Query construction ──────────────────────────────────────────────


def test_query_probes_are_one_per_signal_and_topic_leads():
    probes = ctx.build_document_queries(
        subject="Cutover Planning",
        project="Phase 2 Migration",
        agenda="Walk through the October cutover runbook.",
        user_context="Procurement flagged the SLA section.",
    )
    assert probes[0] == "Cutover Planning — Phase 2 Migration"
    assert "October cutover runbook" in probes[1]
    assert "SLA section" in probes[2]
    assert len(probes) == 3


def test_query_drops_invite_boilerplate_and_urls():
    probes = ctx.build_document_queries(
        subject="Weekly Sync",
        agenda=(
            "Agenda: review the data migration cutover plan.\n"
            "________________________________\n"
            "Microsoft Teams meeting\n"
            "Join the meeting now\n"
            "Meeting ID: 123 456 789\n"
            "Passcode: abc123\n"
            "https://teams.microsoft.com/l/meetup-join/xyz\n"
        ),
    )
    agenda_probe = probes[1]
    assert "data migration cutover plan" in agenda_probe
    for noise in ("Teams", "Meeting ID", "Passcode", "https://", "____"):
        assert noise not in agenda_probe


def test_query_omits_attendees_by_design():
    """Attendee names are deliberately NOT embedded — they retrieve
    RACI tables and signature blocks, not scope. They still reach the
    model through the prompt's Attendees: line."""
    probes = ctx.build_document_queries(
        subject="Cutover Planning",
        agenda="dana.reeves@acme.com will walk the runbook",
    )
    assert probes[0] == "Cutover Planning"
    # Only the agenda mention survives, because the agenda is a signal
    # in its own right; nothing synthesises an attendee probe.
    assert len(probes) == 2


def test_no_signal_means_no_probes():
    assert ctx.build_document_queries() == []
    assert ctx.build_document_queries(subject="   ") == []


# ── Retrieval / degradation ─────────────────────────────────────────


def test_retrieval_is_scoped_to_client_and_never_passes_project():
    """SearchService excludes every document from a project-filtered
    query by design (documents carry a client, never a project), so
    passing a project through here would silently return nothing."""
    search = FakeSearch({"Cutover": [_doc("SOW.docx", "scope text")]})
    hits = ctx.retrieve_for_brief(
        search, "ACME", subject="Cutover", project="Phase 2")
    assert [h["doc_name"] for h in hits] == ["SOW.docx"]
    assert all(c["client"] == "ACME" for c in search.calls)
    assert all(c["project"] is None for c in search.calls)


def test_session_hits_are_never_treated_as_documents():
    search = FakeSearch({"Cutover": [_session_hit(), _doc("SOW.docx", "x")]})
    hits = ctx.retrieve_for_brief(search, "ACME", subject="Cutover")
    assert [h["doc_name"] for h in hits] == ["SOW.docx"]


def test_low_similarity_chunks_are_dropped_not_used_as_padding():
    search = FakeSearch({"Cutover": [
        _doc("Unrelated.docx", "totally unrelated prose", sim=0.05),
    ]})
    assert ctx.retrieve_for_brief(search, "ACME", subject="Cutover") == []


def test_no_client_means_no_retrieval_at_all():
    """Documents can only be filtered by client. With no client
    resolved, an unscoped pull would put another account's SOW in this
    brief — worse than no documents."""
    search = FakeSearch({"Cutover": [_doc("SOW.docx", "scope text")]})
    assert ctx.retrieve_for_brief(search, "", subject="Cutover") == []
    assert search.calls == []


def test_unreachable_folder_degrades_to_empty_not_error():
    search = FakeSearch(raises=True)
    assert ctx.retrieve_for_brief(search, "ACME", subject="Cutover") == []


def test_missing_search_service_degrades_to_empty():
    assert ctx.retrieve_for_brief(None, "ACME", subject="Cutover") == []


def test_empty_index_degrades_to_empty():
    assert ctx.retrieve_for_brief(FakeSearch(), "ACME", subject="Cutover") == []


# ── Budget ──────────────────────────────────────────────────────────


def test_one_long_document_cannot_own_the_document_budget():
    many = [_doc("BigSOW.docx", f"clause {i} " * 20, sim=0.9 - i * 0.01)
            for i in range(10)]
    search = FakeSearch({"Cutover": many})
    hits = ctx.retrieve_for_brief(search, "ACME", subject="Cutover")
    assert len(hits) == ctx.MAX_CHUNKS_PER_DOCUMENT


def test_document_budget_spans_multiple_documents():
    pool = []
    for d in range(6):
        for c in range(4):
            pool.append(_doc(f"Doc{d}.docx", f"doc {d} chunk {c} " * 20,
                             sim=0.9 - (d * 4 + c) * 0.01))
    search = FakeSearch({"Cutover": pool})
    hits = ctx.retrieve_for_brief(search, "ACME", subject="Cutover")
    assert len(hits) <= ctx.MAX_DOCUMENT_CHUNKS
    assert len({h["doc_name"] for h in hits}) >= 3
    assert sum(len(h["text"]) for h in hits) <= ctx.MAX_DOCUMENT_CONTEXT_CHARS


def test_budget_is_filled_breadth_first_across_documents():
    """With realistic ~350-word chunks the char budget admits only about
    three, so a plain rank walk would spend two on whichever single
    document matched best. One chunk per document comes first."""
    realistic = "clause " * 350           # ~2.4 KB, like a real doc chunk
    pool = [
        _doc("BestMatch.docx", "a " + realistic, sim=0.90),
        _doc("BestMatch.docx", "b " + realistic, sim=0.89),
        _doc("Second.docx", "c " + realistic, sim=0.80),
        _doc("Third.docx", "d " + realistic, sim=0.70),
    ]
    hits = ctx.retrieve_for_brief(
        FakeSearch({"Cutover": pool}), "ACME", subject="Cutover")
    assert [h["doc_name"] for h in hits] == [
        "BestMatch.docx", "Second.docx", "Third.docx"]


def test_second_chunks_are_taken_once_every_document_has_one():
    small = "clause " * 40
    pool = [
        _doc("A.docx", "a1 " + small, sim=0.9),
        _doc("A.docx", "a2 " + small, sim=0.85),
        _doc("B.docx", "b1 " + small, sim=0.8),
    ]
    hits = ctx.retrieve_for_brief(
        FakeSearch({"Cutover": pool}), "ACME", subject="Cutover")
    assert [h["doc_name"] for h in hits] == ["A.docx", "B.docx", "A.docx"]
    # And no chunk is selected twice.
    assert len({h["text"] for h in hits}) == 3


def test_char_budget_binds_before_chunk_count_for_large_chunks():
    big = "word " * 900  # ~4500 chars, two of these blow the 8000 cap
    pool = [_doc(f"Doc{i}.docx", big, sim=0.9 - i * 0.01) for i in range(6)]
    search = FakeSearch({"Cutover": pool})
    hits = ctx.retrieve_for_brief(search, "ACME", subject="Cutover")
    assert sum(len(h["text"]) for h in hits) <= ctx.MAX_DOCUMENT_CONTEXT_CHARS
    assert len(hits) < ctx.MAX_DOCUMENT_CHUNKS


def test_single_oversized_chunk_is_truncated_not_dropped():
    pool = [_doc("Huge.docx", "x" * (ctx.MAX_SINGLE_CHUNK_CHARS * 3))]
    hits = ctx.retrieve_for_brief(
        FakeSearch({"Cutover": pool}), "ACME", subject="Cutover")
    assert len(hits) == 1
    assert len(hits[0]["text"]) <= ctx.MAX_SINGLE_CHUNK_CHARS + 20
    assert hits[0]["text"].endswith("…(truncated)")


def test_every_probe_that_hits_gets_representation():
    """Round-robin over per-probe rank: the agenda probe's best hit is
    not buried behind four topic-probe hits."""
    search = FakeSearch({
        "Cutover Planning": [
            _doc(f"Topic{i}.docx", f"topic chunk {i}", sim=0.9)
            for i in range(5)
        ],
        "runbook": [_doc("Runbook.docx", "the october runbook", sim=0.4)],
    })
    hits = ctx.retrieve_for_brief(
        search, "ACME", subject="Cutover Planning",
        agenda="Walk the october runbook end to end.")
    assert "Runbook.docx" in {h["doc_name"] for h in hits}


def test_duplicate_chunk_across_probes_appears_once():
    same = _doc("SOW.docx", "the cutover is scheduled for October")
    search = FakeSearch({"Cutover": [same], "October": [same]})
    hits = ctx.retrieve_for_brief(
        search, "ACME", subject="Cutover", agenda="October dates please")
    assert len(hits) == 1


# ── Prompt shape: attribution + the never-worse invariant ───────────


def _calendar_brief(document_notes: str) -> str:
    s, prompts = _summarizer_capturing()
    _run(s.meeting_prep_brief_from_calendar(
        upcoming_subject="Cutover Planning",
        upcoming_attendees=["dana@acme.com"],
        upcoming_when="Monday Sep 01 at 10:00 AM",
        identified_client="ACME",
        identified_project="Phase 2",
        prior_notes="### [S1] Kickoff  (2026-01-01)\n**Summary:**\ntalked",
        agenda="Walk the runbook.",
        user_context="",
        document_notes=document_notes,
    ))
    return prompts[0]


def _simple_brief(document_notes: str) -> str:
    s, prompts = _summarizer_capturing()
    _run(s.meeting_prep_brief(
        "### Kickoff (2026-01-01)\n**Summary:**\ntalked",
        "Cutover Planning",
        user_context="",
        document_notes=document_notes,
    ))
    return prompts[0]


@pytest.mark.parametrize("build", [_calendar_brief, _simple_brief])
def test_no_documents_yields_the_pre_feature_prompt_exactly(build):
    """A client with no Knowledge Folder must get the brief they got
    before. With document_notes="" the prompt contains not one word
    about documents — no header, no citation rule, not even a
    'no documents found' line."""
    baseline = build("")
    assert "DOC:" not in baseline
    assert "KNOWLEDGE FOLDER" not in baseline
    assert "document" not in baseline.lower()
    # A folder that exists but retrieved nothing is the same as no
    # folder at all — whitespace-only notes take the same path.
    assert build("   \n  ") == baseline


def test_omitting_document_notes_entirely_matches_passing_empty():
    """The kwarg is additive: old callers that never pass it get the
    identical prompt to callers passing ""."""
    s1, p1 = _summarizer_capturing()
    _run(s1.meeting_prep_brief("notes", "Subject"))
    s2, p2 = _summarizer_capturing()
    _run(s2.meeting_prep_brief("notes", "Subject", document_notes=""))
    assert p1[0] == p2[0]


@pytest.mark.parametrize("build", [_calendar_brief, _simple_brief])
def test_documents_are_attributed_distinctly_from_meetings(build):
    prompt = build("### [DOC: ACME SOW v3.docx]\ncutover is October 14")
    assert "=== CLIENT KNOWLEDGE FOLDER — DOCUMENT EXCERPTS ===" in prompt
    assert "### [DOC: ACME SOW v3.docx]" in prompt
    assert "cutover is October 14" in prompt
    # The instruction that makes the two distinguishable at a glance.
    assert "[DOC: <file name>]" in prompt
    assert "never attribute a document" in prompt.lower()
    # Meeting material is still there, under its own header.
    assert "=== PRIOR MEETING NOTES ===" in prompt


def test_document_block_never_precedes_prior_meeting_notes():
    """Recent calls stay the spine of the brief: they lead the context,
    documents follow."""
    prompt = _calendar_brief("### [DOC: SOW.docx]\nclause")
    assert prompt.index("=== PRIOR MEETING NOTES ===") < prompt.index(
        "=== CLIENT KNOWLEDGE FOLDER")


def test_format_document_context_headers_name_the_file():
    blob = ctx.format_document_context([
        _doc("ACME SOW v3.docx", "the cutover is in October"),
        _doc("Requirements.md", "must support SSO"),
    ])
    assert "### [DOC: ACME SOW v3.docx]" in blob
    assert "### [DOC: Requirements.md]" in blob


def test_format_document_context_is_empty_when_nothing_retrieved():
    assert ctx.format_document_context([]) == ""


def test_referenced_documents_is_one_entry_per_document():
    refs = ctx.referenced_documents([
        _doc("SOW.docx", "a", sim=0.4),
        _doc("SOW.docx", "b", sim=0.7),
        _doc("Reqs.md", "c", sim=0.5),
    ])
    assert [r["doc_name"] for r in refs] == ["SOW.docx", "Reqs.md"]
    assert refs[0]["chunk_count"] == 2
    assert refs[0]["similarity"] == pytest.approx(0.7)


# ── Lockstep with the real SearchService ────────────────────────────


def test_fake_search_matches_the_real_document_hit_shape():
    """FakeSearch above stands in for SearchService, and the retrieval
    code reads specific keys off each hit. sentence-transformers isn't
    in the CI env so the real service can't be driven here — this pins
    the contract instead, by reading the fields SearchService actually
    emits for a document hit straight out of its source. If that dict
    is renamed or loses a key, this fails rather than the prep brief
    silently retrieving nothing in production."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "services" / "search_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # The document *result* literal: source="document" plus a
        # similarity score. (search_service.py also builds an internal
        # source="document" metadata row, which has no similarity and
        # is not what callers see.)
        if "similarity" not in keys:
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "source"
                    and isinstance(v, ast.Constant) and v.value == "document"):
                emitted |= keys
    assert emitted, "no document-hit dict literal found in search_service.py"
    assert {"source", "doc_name", "doc_path", "client", "text",
            "similarity"} <= emitted
    assert "session_id" not in emitted, (
        "document hits must not carry a session_id — the brief relies "
        "on source= to tell document material from meeting material")
    assert set(_doc("n", "t")) == emitted


# ── End to end through the real endpoints ───────────────────────────
#
# The module tests above prove the retrieval contract; these prove the
# two endpoints are actually wired to it — that the client, subject,
# project, invite body and user context reach the query builder in the
# right order, that the retrieved blob reaches the summarizer, and that
# provenance reaches the response.


class _StubSessionSvc:
    def __init__(self, sessions):
        self._sessions = sessions

    def list_sessions(self):
        return list(self._sessions)


class _StubSvc:
    """Minimal stand-in for server.svc — only what the two prep-brief
    handlers touch."""

    def __init__(self, sessions, search, summarizer):
        self.session_svc = _StubSessionSvc(sessions)
        self.search_svc = search
        self.summarizer = summarizer
        self.commitments_svc = None
        self.settings = None

    def load_settings(self):
        return self.settings


def _session(sid, started_at, client="ACME", summary="we discussed things"):
    return {
        "session_id": sid,
        "display_name": f"Call {sid}",
        "started_at": started_at,
        "client": client,
        "project": "",
        "has_summary": True,
        "has_transcript": True,
        "summary": summary,
        "action_items": "",
        "decisions": "",
    }


@pytest.fixture
def server_module():
    from _app_import import import_app
    import_app()
    import server
    return server


def _wire(server, monkeypatch, sessions, search):
    summarizer, prompts = _summarizer_capturing()
    monkeypatch.setattr(
        server, "svc", _StubSvc(sessions, search, summarizer))
    return prompts


def test_from_meeting_endpoint_puts_documents_in_context(
        server_module, monkeypatch):
    search = FakeSearch({"Cutover": [
        _doc("ACME SOW v3.docx", "the cutover window is 14 October"),
    ]})
    prompts = _wire(
        server_module, monkeypatch,
        [_session("S1", "2026-02-01T00:00:00")], search)

    req = server_module.PrepBriefFromMeetingRequest(
        subject="Cutover Planning", attendees=["dana@acme.com"],
        scheduled_start_iso="2026-03-01T10:00:00", client="ACME")
    res = _run(server_module.prep_brief_from_meeting(req))

    assert "### [DOC: ACME SOW v3.docx]" in prompts[0]
    assert "the cutover window is 14 October" in prompts[0]
    # …and both provenance lists come back, disjoint and named.
    assert [d["doc_name"] for d in res["referenced_documents"]] == [
        "ACME SOW v3.docx"]
    assert res["document_count"] == 1
    assert [s["session_id"] for s in res["referenced_sessions"]] == ["S1"]
    assert res["related_count"] == 1


def test_from_meeting_endpoint_passes_every_signal_to_the_query(
        server_module, monkeypatch):
    """Guards the positional hand-off from the handler into
    retrieve_for_brief — a swapped argument here would silently query
    for the wrong thing."""
    search = FakeSearch()
    _wire(server_module, monkeypatch,
          [_session("S1", "2026-02-01T00:00:00")], search)

    req = server_module.PrepBriefFromMeetingRequest(
        subject="Cutover Planning", client="ACME", project="Phase 2",
        body="Walk the october runbook.",
        user_context="Procurement flagged the SLA section.")
    _run(server_module.prep_brief_from_meeting(req))

    queries = [c["query"] for c in search.calls]
    assert queries[0] == "Cutover Planning — Phase 2"
    assert "october runbook" in queries[1]
    assert "SLA section" in queries[2]
    assert all(c["client"] == "ACME" for c in search.calls)


def test_from_meeting_endpoint_without_knowledge_folder_is_unchanged(
        server_module, monkeypatch):
    search = FakeSearch()   # client has no indexed documents
    prompts = _wire(
        server_module, monkeypatch,
        [_session("S1", "2026-02-01T00:00:00")], search)

    req = server_module.PrepBriefFromMeetingRequest(
        subject="Cutover Planning", client="ACME")
    res = _run(server_module.prep_brief_from_meeting(req))

    assert "KNOWLEDGE FOLDER" not in prompts[0]
    assert "document" not in prompts[0].lower()
    assert res["referenced_documents"] == []
    assert res["document_count"] == 0
    assert res["markdown"]


def test_from_meeting_endpoint_survives_unreachable_folder(
        server_module, monkeypatch):
    """Disconnected Drive: SearchService raises on every probe. The
    brief must be exactly the no-documents brief, not a 500."""
    search = FakeSearch(raises=True)
    prompts = _wire(
        server_module, monkeypatch,
        [_session("S1", "2026-02-01T00:00:00")], search)

    req = server_module.PrepBriefFromMeetingRequest(
        subject="Cutover Planning", client="ACME")
    res = _run(server_module.prep_brief_from_meeting(req))

    assert "KNOWLEDGE FOLDER" not in prompts[0]
    assert res["document_count"] == 0
    assert res["markdown"]


def test_from_meeting_endpoint_budget_split_holds_when_both_oversupplied(
        server_module, monkeypatch):
    sessions = [
        _session(f"S{i:02d}", f"2026-02-{i + 1:02d}T00:00:00")
        for i in range(20)
    ]
    pool = []
    for d in range(8):
        for c in range(5):
            pool.append(_doc(f"Doc{d}.docx", f"doc {d} chunk {c} " * 20,
                             sim=0.9 - (d * 5 + c) * 0.005))
    search = FakeSearch({"Cutover": pool})
    prompts = _wire(server_module, monkeypatch, sessions, search)

    req = server_module.PrepBriefFromMeetingRequest(
        subject="Cutover Planning", client="ACME")
    res = _run(server_module.prep_brief_from_meeting(req))

    # Sessions keep their full, unchanged allowance — documents are
    # budgeted beside them, never out of them.
    assert res["related_count"] == ctx.MAX_CONTEXT_SESSIONS == 8
    # Most recent first.
    assert res["referenced_sessions"][0]["session_id"] == "S19"
    # Documents stay inside their own cap, spread across files.
    doc_headers = prompts[0].count("### [DOC: ")
    assert 0 < doc_headers <= ctx.MAX_DOCUMENT_CHUNKS
    assert len(res["referenced_documents"]) >= 3
    assert all(d["chunk_count"] <= ctx.MAX_CHUNKS_PER_DOCUMENT
               for d in res["referenced_documents"])


def test_from_meeting_endpoint_briefs_a_client_with_docs_but_no_meetings(
        server_module, monkeypatch):
    """New logo: Knowledge Folder indexed, zero processed calls. This
    used to short-circuit to 'no prior meetings available'."""
    search = FakeSearch({"Cutover": [_doc("SOW.docx", "scope of work")]})
    prompts = _wire(server_module, monkeypatch, [], search)

    req = server_module.PrepBriefFromMeetingRequest(
        subject="Cutover Planning", client="ACME")
    res = _run(server_module.prep_brief_from_meeting(req))

    assert res["document_count"] == 1
    assert res["related_count"] == 0
    assert "No prior meetings with this client yet" in prompts[0]
    assert "### [DOC: SOW.docx]" in prompts[0]


def test_from_meeting_endpoint_with_nothing_at_all_still_short_circuits(
        server_module, monkeypatch):
    _wire(server_module, monkeypatch, [], FakeSearch())
    req = server_module.PrepBriefFromMeetingRequest(subject="Cutover")
    res = _run(server_module.prep_brief_from_meeting(req))
    assert res["related_count"] == 0
    assert res["document_count"] == 0
    assert res["referenced_documents"] == []
    assert "No prior meetings with summaries" in res["markdown"]


def test_prep_brief_endpoint_puts_documents_in_context(
        server_module, monkeypatch):
    search = FakeSearch({"Cutover": [_doc("SOW.docx", "cutover in October")]})
    prompts = _wire(
        server_module, monkeypatch,
        [_session("S1", "2026-02-01T00:00:00")], search)

    req = server_module.PrepBriefRequest(
        subject="Cutover Planning", client="ACME")
    res = _run(server_module.prep_brief(req))

    assert "### [DOC: SOW.docx]" in prompts[0]
    assert [d["doc_name"] for d in res["referenced_documents"]] == ["SOW.docx"]
    assert res["document_count"] == 1


def test_prep_brief_endpoint_unscoped_fallback_pulls_no_documents(
        server_module, monkeypatch):
    """With no client there is nothing to filter documents by, so an
    unscoped pull could put another account's SOW in this brief. The
    fallback path deliberately stays session-only."""
    search = FakeSearch({"Cutover": [_doc("OtherClientSOW.docx", "secret")]})
    prompts = _wire(
        server_module, monkeypatch,
        [_session("S1", "2026-02-01T00:00:00", client="OTHER")], search)

    req = server_module.PrepBriefRequest(subject="Cutover Planning")
    res = _run(server_module.prep_brief(req))

    assert search.calls == []
    assert res["referenced_documents"] == []
    assert "KNOWLEDGE FOLDER" not in prompts[0]


def test_prep_brief_endpoint_without_knowledge_folder_is_unchanged(
        server_module, monkeypatch):
    prompts = _wire(
        server_module, monkeypatch,
        [_session("S1", "2026-02-01T00:00:00")], FakeSearch())
    req = server_module.PrepBriefRequest(
        subject="Cutover Planning", client="ACME")
    res = _run(server_module.prep_brief(req))

    assert "document" not in prompts[0].lower()
    assert res["document_count"] == 0
    assert res["brief"]
