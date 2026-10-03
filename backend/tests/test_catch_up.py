"""
"What I missed": a catch-up brief for a meeting the reader wasn't in.

Rows for previous_meeting() come from a real SessionService's
list_sessions(), not hand-written dicts.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import catch_up as cu
from models.segment import Segment
from models.session import Session
from models.speaker import Speaker


def _svc(tmp_path):
    from services.session_service import SessionService
    return SessionService(str(tmp_path / "rec"), index_enabled=False)


def _meeting(sid, client, day, summary="", segments=True):
    s = Session(session_id=sid)
    s.display_name = f"{client or 'Internal'} sync {day}"
    s.client = client
    s.started_at = dt.datetime.fromisoformat(f"2026-{day}T10:00:00")
    s.ended_at = s.started_at + dt.timedelta(minutes=30)
    s.summary = summary
    if segments:
        s.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", "Jane Doe")}
        s.segments = [Segment("SPEAKER_00", 5.0, 9.0,
                              "Can your team send the routing file by Friday?")]
    return s


def test_reader_context_is_the_email_domain_only():
    assert "example.com" in cu.reader_context("user@example.com")
    assert "user@" not in cu.reader_context("user@example.com")
    assert cu.reader_context("") == ""
    assert cu.reader_context("not-an-email") == ""


def test_the_previous_meeting_is_the_latest_earlier_one_with_that_client(
        tmp_path):
    svc = _svc(tmp_path)
    for s in [
        _meeting("A0000001", "Acme", "09-01", "First."),
        _meeting("A0000002", "acme", "09-15", "Second."),       # case
        _meeting("A0000003", "Acme", "09-20", ""),              # no summary
        _meeting("A0000004", "Globex", "09-25", "Other client."),
        _meeting("A0000005", "Acme", "10-10", "Later."),        # after
    ]:
        svc.save(s)
    now = _meeting("A0000009", "ACME", "10-01")
    svc.save(now)
    prev = cu.previous_meeting(svc.list_sessions(), now)
    assert prev["session_id"] == "A0000002"
    ctx = cu.previous_context(prev)
    assert "2026-09-15" in ctx and "Second." in ctx


def test_no_client_or_no_history_means_no_comparison(tmp_path):
    svc = _svc(tmp_path)
    svc.save(_meeting("B0000001", "Acme", "09-01", "Old."))
    assert cu.previous_meeting(svc.list_sessions(),
                               _meeting("B0000002", "", "10-01")) is None
    assert cu.previous_meeting(svc.list_sessions(),
                               _meeting("B0000003", "Hooli", "10-01")) is None
    assert cu.previous_context(None) == ""


def test_a_reply_is_checked_field_by_field():
    brief = cu.apply_reply({
        "headline": "  Pilot moves to Monday.  ",
        "decisions": ["Pilot Monday", "", None],
        "asks_of_you": [{"ask": "Send routing file", "who_asked": "Jane Doe"}],
        "open_questions": "not a list",
        "changes_since_last": ["Date moved"],
        "worth_hearing": [{"at": "12:40", "why": "Pricing pushback"},
                          {"at": "later", "why": "bad time"},
                          {"at": "1:02:03", "why": ""}],
    }, previous=None)
    assert brief["headline"] == "Pilot moves to Monday."
    assert brief["decisions"] == ["Pilot Monday"]
    assert brief["asks_of_you"] == ["Send routing file — Jane Doe"]
    assert brief["open_questions"] == []
    assert brief["changes_since_last"] == []       # nothing to compare with
    assert brief["worth_hearing"] == [{"at": "12:40", "why": "Pricing pushback"}]
    assert brief["compared_with"] is None
    assert cu.is_empty(cu.apply_reply({}, None))
    assert cu.is_empty(cu.apply_reply("garbage", None))


def test_the_document():
    s = _meeting("C0000001", "Acme", "10-01")
    s.catch_up = cu.apply_reply({
        "headline": "Pilot moves to Monday.",
        "asks_of_you": ["Send the routing file by Friday — Jane Doe"],
        "changes_since_last": ["Go-live moved a week later"],
        "worth_hearing": [{"at": "0:05", "why": "The routing ask"}],
    }, previous={"session_id": "X", "display_name": "Acme sync 09-15",
                 "started_at": "2026-09-15T10:00:00"})
    md = cu.render_markdown(s)
    assert md.startswith("# What I missed — Acme sync 10-01")
    assert "## Asked of you or your team\n- Send the routing file" in md
    assert "## Changed since Acme sync 09-15 (2026-09-15)" in md
    assert "## Worth hearing yourself\n- 0:05 — The routing ask" in md
    assert "Written by AI" in md
    assert cu.render_markdown(_meeting("C0000002", "Acme", "10-01")) == ""


# ── the model call ───────────────────────────────────────────────────


def test_the_request_carries_the_reader_and_the_previous_meeting(monkeypatch):
    if "anthropic" not in sys.modules:
        monkeypatch.setitem(sys.modules, "anthropic", MagicMock())
    from core import summarizer
    obj = summarizer.Summarizer.__new__(summarizer.Summarizer)
    obj._budget = lambda n: n
    calls = []

    async def _chat(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return json.dumps({"headline": "x"})
    obj._chat = _chat
    out = asyncio.run(obj.catch_up(
        "[00:05 → 00:09] Jane Doe: hi", reader=cu.reader_context("u@example.com"),
        previous="The previous meeting with this client was …"))
    assert out == {"headline": "x"}
    c = calls[0]
    assert c["json_mode"] is True
    assert "Jane Doe: hi" in c["cache_prefix"]          # transcript is cached
    assert "example.com" in c["prompt"] and "changes_since_last" in c["prompt"]
    calls.clear()
    asyncio.run(obj.catch_up("t"))
    assert "changes_since_last" not in calls[0]["prompt"]


# ── on request, from the meeting window ──────────────────────────────


def _server(monkeypatch, svc, reply):
    sys.modules.setdefault("dotenv", MagicMock())
    from _app_import import import_app
    import_app()
    import server
    seen = {}

    async def _catch_up(transcript, notes="", reader="", previous=""):
        seen.update(transcript=transcript, reader=reader, previous=previous)
        return reply
    monkeypatch.setattr(server.svc, "settings",
                        SimpleNamespace(email_to="user@example.com"))
    monkeypatch.setattr(server.svc, "_services_ready", True)
    monkeypatch.setattr(server.svc, "session_svc", svc)
    monkeypatch.setattr(server.svc, "summarizer",
                        SimpleNamespace(catch_up=_catch_up))
    monkeypatch.setattr(server, "_export_after_processing", lambda sid: None)
    return server, seen


def test_the_endpoint_makes_and_saves_a_brief(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    svc.save(_meeting("D0000001", "Acme", "09-15", "Earlier summary."))
    svc.save(_meeting("D0000002", "Acme", "10-01"))
    server, seen = _server(monkeypatch, svc, {
        "headline": "Routing file due Friday.",
        "asks_of_you": ["Send the routing file — Jane Doe"]})
    resp = asyncio.run(server.make_catch_up("D0000002"))
    assert resp["ok"]
    assert "Jane Doe: Can your team send" in seen["transcript"]
    assert "example.com" in seen["reader"]
    assert "Earlier summary." in seen["previous"]
    saved = svc.load_full("D0000002")
    assert saved.catch_up["headline"] == "Routing file due Friday."
    assert saved.catch_up["compared_with"]["session_id"] == "D0000001"


def test_no_transcript_and_empty_replies_are_errors(tmp_path, monkeypatch):
    from fastapi import HTTPException
    svc = _svc(tmp_path)
    svc.save(_meeting("E0000001", "Acme", "10-01", segments=False))
    svc.save(_meeting("E0000002", "Acme", "10-02"))
    server, _ = _server(monkeypatch, svc, {})
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.make_catch_up("E0000001"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.make_catch_up("E0000002"))
    assert e.value.status_code == 502
    assert svc.load_full("E0000002").catch_up is None
