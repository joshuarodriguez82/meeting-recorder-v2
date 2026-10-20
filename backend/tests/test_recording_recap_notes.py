"""
In-call Recap button and notes panel.

Both act on the recording's IN-MEMORY session: when a recording stops,
that object is written over the session JSON on disk, so anything
written to disk mid-call is lost. The last test pins that for the
existing rename/tag edits on the record screen, which went to disk only.

Names are fictional (AGENTS.md).
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from _app_import import import_app  # noqa: E402

import_app()
import server  # noqa: E402

from core.live_transcriber import HISTORY_MAX_SEGMENTS, LiveTranscriber  # noqa: E402
from core.summarizer import Summarizer  # noqa: E402
from models.session import Session  # noqa: E402


def _segments(n, speaker="them", label="Speaker 1"):
    return [{"id": i, "start": i * 2.0, "end": i * 2.0 + 1.5,
             "text": f"line {i}", "speaker": speaker,
             "speaker_label": label} for i in range(n)]


# ── the recap prompt ─────────────────────────────────────────────────

def _summarizer():
    s = Summarizer.__new__(Summarizer)
    s._provider = "anthropic"
    sent = {}

    async def _chat(prompt, max_tokens=0, timeout=0.0, **kw):
        sent["prompt"], sent["max_tokens"] = prompt, max_tokens
        return "## Discussed\n- line 0 came up\n"
    s._chat = _chat
    return s, sent


def test_the_recap_reads_every_line_of_the_call():
    """Not a recent window: the start of a long call is where much of
    what a recap must say was settled."""
    s, sent = _summarizer()
    segs = _segments(2500)
    out = asyncio.run(s.live_recap(segs, notes="Budget is fixed at 40k."))
    assert out.startswith("## Discussed")
    assert "line 0" in sent["prompt"] and "line 2499" in sent["prompt"]
    assert "Budget is fixed at 40k." in sent["prompt"]


def test_nothing_said_means_no_model_call():
    s, sent = _summarizer()
    assert asyncio.run(s.live_recap([])) == ""
    assert sent == {}


def test_the_live_history_keeps_a_long_call():
    lt = LiveTranscriber(engine_provider=lambda: None)
    for seg in _segments(5000):
        lt._history.append(seg)
    assert len(lt.all_segments()) == 5000
    assert HISTORY_MAX_SEGMENTS >= 20000


# ── the endpoints ────────────────────────────────────────────────────

class _Model:
    def __init__(self, reply="## Discussed\n- pricing\n"):
        self.reply = reply
        self.calls = []

    async def live_recap(self, segments, meeting_context="", notes=""):
        self.calls.append({"lines": len(segments), "notes": notes})
        return self.reply


@pytest.fixture
def recording(monkeypatch):
    session = Session("S-live")
    session.display_name = "Acme weekly"
    transcriber = SimpleNamespace(is_running=True,
                                  all_segments=lambda: _segments(2500))
    rec = SimpleNamespace(is_recording=True, current_session=session,
                          live_transcriber=transcriber)
    model = _Model()
    settings = SimpleNamespace(copilot_custom_context="")
    monkeypatch.setattr(server.svc, "settings", settings)
    monkeypatch.setattr(server.svc, "load_settings", lambda: settings)
    monkeypatch.setattr(server.svc, "recording_svc", rec)
    monkeypatch.setattr(server.svc, "live_summarizer", model)
    return session, rec, model


def test_recap_covers_the_whole_call_and_is_kept(recording):
    session, _, model = recording
    session.notes = "Jane Doe owns the SOW."
    out = asyncio.run(server.recording_recap())
    assert model.calls == [{"lines": 2500, "notes": "Jane Doe owns the SOW."}]
    assert out["recap"].startswith("## Discussed")
    assert out["lines"] == 2500 and out["as_of_s"] == pytest.approx(4999.5)
    assert session.live_recap == out
    assert asyncio.run(server.recording_recap_latest()) == out


def test_a_newer_recap_replaces_the_last(recording):
    session, _, model = recording
    asyncio.run(server.recording_recap())
    model.reply = "## Decided\n- go with option B\n"
    asyncio.run(server.recording_recap())
    assert session.live_recap["recap"].startswith("## Decided")


@pytest.mark.parametrize("setup,status", [
    (lambda rec, sess: setattr(rec, "is_recording", False), 409),
    (lambda rec, sess: setattr(rec.live_transcriber, "is_running", False), 409),
    (lambda rec, sess: setattr(rec.live_transcriber, "all_segments", lambda: []), 409),
])
def test_recap_says_why_it_cannot_run(recording, setup, status):
    session, rec, _ = recording
    setup(rec, session)
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.recording_recap())
    assert e.value.status_code == status


def test_recap_without_a_model(recording, monkeypatch):
    monkeypatch.setattr(server.svc, "live_summarizer", None)
    monkeypatch.setattr(server.svc, "summarizer", None)
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.recording_recap())
    assert e.value.status_code == 503


def test_a_model_failure_is_a_clear_error(recording):
    _, _, model = recording

    async def boom(*a, **k):
        raise TimeoutError()
    model.live_recap = boom
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.recording_recap())
    assert e.value.status_code == 502


def test_the_recap_survives_into_the_saved_session():
    s = Session("S-roundtrip")
    s.live_recap = {"recap": "## Discussed\n- x\n", "as_of_s": 61.0,
                    "lines": 3, "generated_at": "2026-10-20T10:00:00"}
    assert Session.from_dict(s.to_dict()).live_recap == s.live_recap
    assert Session.from_dict({"session_id": "old"}).live_recap == {}


# ── notes during the call ────────────────────────────────────────────

def _put(notes, base):
    return asyncio.run(server.save_recording_notes(
        server.RecordingNotesRequest(notes=notes, base=base)))["notes"]


def test_notes_typed_during_the_call_land_on_the_recording(recording):
    session, _, _ = recording
    assert _put("Pricing call next week", "") == "Pricing call next week"
    assert session.notes == "Pricing call next week"
    assert asyncio.run(server.recording_notes()) == {
        "notes": "Pricing call next week"}


def test_a_co_pilot_save_is_kept_when_the_user_keeps_typing(recording):
    """Co-Pilot's "Save to my notes" appends on the server while the
    panel is open; the user's next save must not wipe it."""
    session, _, _ = recording
    _put("First note", "")
    asyncio.run(server.save_copilot_suggestion(server.CoPilotSaveRequest(
        kind="note", text="Ask about the renewal date")))
    merged = _put("First note, edited", "First note")
    assert merged.startswith("First note, edited")
    assert "• Ask about the renewal date" in merged


def test_the_users_own_deletions_stand(recording):
    session, _, _ = recording
    _put("Keep this\nDrop this", "")
    assert _put("Keep this", "Keep this\nDrop this") == "Keep this"


@pytest.mark.parametrize("current,base,incoming,expected", [
    ("", None, "a", "a"),
    ("a", "a", "b", "b"),
    ("a\n\n• tip", "a", "a2", "a2\n\n• tip"),
    ("a\n\n• tip", "a", "a2\n\n• tip", "a2\n\n• tip"),   # already there
    ("rewritten elsewhere", "a", "b", "b"),
])
def test_merge_rules(current, base, incoming, expected):
    assert server.merge_recording_notes(current, base, incoming) == expected


def test_notes_need_a_recording(recording):
    _, rec, _ = recording
    rec.is_recording = False
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.recording_notes())
    assert e.value.status_code == 409


def test_renaming_during_the_call_survives_stop(recording, monkeypatch):
    """The record screen renames and tags the meeting mid-call through
    PATCH /sessions/{id}, which wrote only the file on disk — and Stop
    writes the in-memory session over that file."""
    session, _, _ = recording
    on_disk = Session("S-live")
    saved = []
    monkeypatch.setattr(server.svc, "session_svc", SimpleNamespace(
        load_full=lambda sid: on_disk if sid == "S-live" else None,
        save=lambda s: saved.append(s)))
    monkeypatch.setattr(server, "_auto_export_to_client", lambda s: None)

    asyncio.run(server.patch_session("S-live", server.SessionPatchRequest(
        display_name="Globex renewal", client="Globex")))

    assert (session.display_name, session.client) == ("Globex renewal", "Globex")
    assert session.project == on_disk.project
