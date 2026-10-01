"""
After the call: which Co-Pilot questions were answered, and what's left.

The Co-Pilot's questions and asks were only a live list. Afterwards the
useful part is which of them the meeting closed, what the answer was,
and which are still open — candidates to send the customer — exported
with the rest of the meeting files.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.copilot_board import CopilotBoard
from core.copilot_followups import (
    ANSWERED, OPEN, PARTLY, apply_resolutions, board_for, items_to_check,
    render_markdown,
)
from models.session import Session


def _session(board=None, ticks=None, name="Acme — Connect discovery"):
    s = Session(session_id="S1")
    s.display_name = name
    s.copilot_board = board or []
    s.copilot_ticks = ticks or []
    return s


def _board():
    b = CopilotBoard()
    b.merge({
        "clarifying_questions": [
            "Ask how many agents move to Amazon Connect in phase one",
            "Ask whether they need Salesforce CTI on day one"],
        "risks": ["Number porting lead time from the current carrier"],
        "follow_ups": ["Send the routing workbook to Pat Roe"],
    })
    return b.to_list()


# ── What gets checked ───────────────────────────────────────────────

def test_questions_and_follow_ups_are_checked_risks_are_not():
    kinds = {i["kind"] for i in items_to_check(_board())}
    assert kinds == {"clarifying_questions", "follow_ups"}


def test_dismissed_and_already_checked_items_are_skipped():
    board = _board()
    board[0]["status"] = "dismissed"
    board[1]["resolution"] = ANSWERED
    ids = {i["id"] for i in items_to_check(board)}
    assert board[0]["id"] not in ids and board[1]["id"] not in ids


def test_an_older_meeting_gets_a_board_from_its_saved_updates():
    s = _session(ticks=[
        {"clarifying_questions": ["Ask about SSO with Okta for agents"],
         "generated_at": "2026-10-01T12:40:00"},
        {"clarifying_questions": ["Ask about Okta SSO for the agents"],
         "generated_at": "2026-10-01T12:45:00"},
    ])
    board = board_for(s)
    assert len(board) == 1 and board[0]["times_suggested"] == 2


# ── Applying results ────────────────────────────────────────────────

def test_results_are_written_onto_the_board():
    board = _board()
    q1, q2 = board[0]["id"], board[1]["id"]
    n = apply_resolutions(board, {
        q1: {"status": "answered", "answer": "About 400 agents — Jane Roe."},
        q2: {"status": "open", "answer": "should be dropped"},
    })
    assert n == 2
    assert board[0]["resolution"] == ANSWERED
    assert "400 agents" in board[0]["answer"]
    assert board[1]["resolution"] == OPEN and board[1]["answer"] == ""


def test_the_model_cannot_invent_an_item_or_a_state():
    board = _board()
    n = apply_resolutions(board, {
        "made-up-id": {"status": "answered", "answer": "x"},
        board[0]["id"]: {"status": "probably", "answer": "x"},
    })
    assert n == 0
    assert all("resolution" not in i for i in board)


# ── The exported document ───────────────────────────────────────────

def test_the_document_leads_with_what_is_still_open():
    board = _board()
    apply_resolutions(board, {
        board[0]["id"]: {"status": "answered", "answer": "About 400."},
        board[1]["id"]: {"status": "open"},
        board[3]["id"]: {"status": "partly",
                         "answer": "Agreed to send; owner not named."},
    })
    md = render_markdown(_session(board))
    assert md.index("## Still open") < md.index("## Partly answered") \
        < md.index("## Answered during the call")
    assert "Salesforce CTI" in md.split("## Partly answered")[0]
    assert "About 400." in md
    assert "Number porting" in md      # risks listed for context
    assert "suggestions, not things anyone agreed to" in md


def test_dismissed_items_stay_out_of_the_document():
    board = _board()
    board[0]["status"] = "dismissed"
    md = render_markdown(_session(board))
    assert "phase one" not in md


def test_an_item_marked_done_during_the_call_is_not_listed_open():
    board = _board()
    board[0]["status"] = "done"
    md = render_markdown(_session(board))
    assert "phase one" not in md.split("## Answered")[0].split(
        "## Partly")[0].split("## Risks")[0]


def test_no_co_pilot_means_no_document():
    assert render_markdown(_session()) == ""


def test_the_export_writes_the_file_with_the_other_artifacts(tmp_path):
    from services.export_service import ExportService
    board = _board()
    s = _session(board)
    s.summary = "Summary text."
    out = ExportService(str(tmp_path)).export_all(s, copy_audio=False)
    files = [p for p in out if "copilot_followups_" in p]
    assert len(files) == 1 and files[0].endswith(".md")
    assert "## Still open" in open(files[0], encoding="utf-8").read()


def test_the_export_skips_it_without_a_co_pilot(tmp_path):
    from services.export_service import ExportService
    s = _session()
    s.summary = "Summary text."
    out = ExportService(str(tmp_path)).export_all(s, copy_audio=False)
    assert not [p for p in out if "copilot_followups_" in p]


# ── The model call ──────────────────────────────────────────────────

def test_the_check_reads_the_reply_and_shares_the_transcript_cache(
        monkeypatch):
    import sys
    from unittest.mock import MagicMock
    if "anthropic" not in sys.modules:
        monkeypatch.setitem(sys.modules, "anthropic", MagicMock())
    from core.summarizer import Summarizer
    seen = {}

    async def chat(prompt, **kw):
        seen.update(prompt=prompt, **kw)
        return ('{"items": [{"id": "a1", "status": "answered", '
                '"answer": "400 agents, said Jane Roe."}, '
                '{"id": "b2", "status": "open", "answer": ""}]}')

    summ = Summarizer.__new__(Summarizer)
    summ._chat = chat
    summ._budget = lambda n: n
    out = asyncio.run(summ.resolve_copilot_items(
        "Jane Roe: about 400 agents.",
        [{"id": "a1", "text": "How many agents?"},
         {"id": "b2", "text": "Do they need CTI?"}], notes="n"))
    assert out["a1"]["status"] == "answered"
    assert out["b2"]["status"] == "open"
    assert seen["json_mode"] is True
    assert "Jane Roe: about 400 agents." in seen["cache_prefix"]
    assert 'id "a1"' in seen["prompt"]


# ── Through process_full ────────────────────────────────────────────

def test_process_full_checks_the_board_and_saves_it(monkeypatch):
    from tests._app_import import import_app
    import_app()
    import server

    session = _session(_board())
    session.segments = [{"speaker_id": "A", "start": 0, "end": 1,
                         "text": "hi"}]
    session.screenshots = []
    session.full_transcript = lambda: "Jane Roe: about 400 agents."
    settings = SimpleNamespace(is_configured=True)
    monkeypatch.setattr(server.svc, "load_settings", lambda: settings)
    monkeypatch.setattr(server.svc, "settings", settings)
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)
    monkeypatch.setattr(server.svc, "session_svc", SimpleNamespace(
        load_full=lambda sid: session, save=lambda s: None))
    for name in ("search_svc", "commitments_svc", "engagement_svc"):
        monkeypatch.setattr(server.svc, name, None)
    monkeypatch.setattr(server, "_export_after_processing", lambda sid: None)
    q = session.copilot_board[0]["id"]

    async def text(*a, **k):
        return "x"

    async def structured(*a, **k):
        return {}

    async def resolve(transcript, items, notes=""):
        assert {i["kind"] for i in items} <= {"clarifying_questions",
                                              "follow_ups"}
        return {q: {"status": "answered", "answer": "About 400."}}

    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace(
        summarize=text, extract_action_items=text, extract_decisions=text,
        extract_requirements=text, extract_structured=structured,
        resolve_copilot_items=resolve))
    monkeypatch.setattr(server.svc, "template_svc",
                        SimpleNamespace(get_prompt=lambda n: "p"))
    result = asyncio.run(server.process_full("S1",
                                             server.ProcessFullRequest()))
    assert result["stages"]["copilot_followups"].startswith("ok")
    assert session.copilot_board[0]["resolution"] == ANSWERED


def test_a_failed_check_never_fails_processing(monkeypatch):
    from tests._app_import import import_app
    import_app()
    import server

    session = _session(_board())

    async def boom(*a, **k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(server.svc, "summarizer",
                        SimpleNamespace(resolve_copilot_items=boom))
    stage = asyncio.run(server._resolve_copilot_followups(
        session, "t", ""))
    assert stage.startswith("failed")
