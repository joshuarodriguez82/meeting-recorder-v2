"""
Co-Pilot out of beta: one board, real context, a backend loop.

Defects these pin, each observed in the code as shipped:

* The tick loop lived in the Record tab, so leaving the tab stopped all
  coaching (and the ticks the summary reads).
* ``session.meeting_name`` doesn't exist — the model was never told the
  meeting's name, client, project, organiser or attendees.
* Transcript lines said ``[them]`` for every other participant,
  discarding the live speaker split.
* Every tick became a new card; the model saw one tick of memory;
  nothing could be marked asked or dismissed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.copilot_board import CopilotBoard, same_suggestion
from core.copilot_context import format_transcript, meeting_context
from services.copilot_runner import CopilotRunner, TickDeps


# ── The board ───────────────────────────────────────────────────────

def test_a_reworded_repeat_merges_into_one_entry():
    b = CopilotBoard()
    b.merge({"clarifying_questions": [
        "Ask how many agents will move to Amazon Connect in phase one"]})
    new = b.merge({"clarifying_questions": [
        "How many agents move to Amazon Connect in phase one?"]})
    assert new == []
    assert len(b.items) == 1 and b.items[0].times_suggested == 2


def test_different_suggestions_stay_separate():
    assert not same_suggestion("Ask about the IVR migration timeline",
                               "Ask about the Salesforce CTI budget")
    b = CopilotBoard()
    b.merge({"risks": ["IVR migration timeline slips past Q3",
                       "Salesforce CTI budget is not approved"]})
    assert len(b.items) == 2


def test_the_same_words_under_a_different_kind_are_separate():
    b = CopilotBoard()
    b.merge({"risks": ["Data residency for EU callers"],
             "follow_ups": ["Data residency for EU callers"]})
    assert len(b.items) == 2


def test_a_dismissed_suggestion_stays_dismissed_when_raised_again():
    b = CopilotBoard()
    b.merge({"risks": ["Vendor lock-in with the CCaaS platform"]})
    b.set_status(b.items[0].id, "dismissed")
    b.merge({"risks": ["Lock-in to the CCaaS platform vendor"]})
    assert [i.status for i in b.items] == ["dismissed"]
    assert b.open_items() == []


def test_status_must_be_known():
    b = CopilotBoard()
    b.merge({"risks": ["x y z"]})
    with pytest.raises(ValueError):
        b.set_status(b.items[0].id, "archived")
    with pytest.raises(KeyError):
        b.set_status("nope", "done")


def test_the_model_is_shown_the_users_verdicts():
    b = CopilotBoard()
    b.merge({"clarifying_questions": ["Ask about SSO with Okta"],
             "risks": ["Telephony carrier porting lead time"],
             "follow_ups": ["Send the routing workbook to Jane Roe"]})
    q, r, f = b.items
    b.set_status(q.id, "done")
    b.set_status(r.id, "dismissed")
    mem = b.prompt_memory()
    assert "ALREADY ASKED OR HANDLED" in mem and "SSO with Okta" in mem
    assert "DISMISSED" in mem and "porting lead time" in mem
    assert "ALREADY ON THE USER'S BOARD" in mem and "routing workbook" in mem


def test_the_board_round_trips_through_the_session():
    b = CopilotBoard()
    b.merge({"risks": ["Telephony carrier porting lead time"]})
    b.set_status(b.items[0].id, "saved")
    again = CopilotBoard(b.to_list())
    assert again.items[0].status == "saved"
    assert again.items[0].text == b.items[0].text


def test_fresh_marks_only_what_this_tick_brought():
    b = CopilotBoard()
    b.merge({"risks": ["Telephony carrier porting lead time"]})
    b.merge({"follow_ups": ["Send the routing workbook to Jane Roe"]})
    assert [i.fresh for i in b.items] == [False, True]


# ── The context ─────────────────────────────────────────────────────

def test_other_participants_keep_their_names():
    """Was `[them]` for everyone."""
    text = format_transcript([
        {"speaker": "you", "text": "What's the timeline?"},
        {"speaker": "them", "speaker_label": "Jane Roe", "text": "Q3."},
        {"speaker": "them", "speaker_label": "Speaker 2", "text": "Q4."},
        {"speaker": "them", "text": "Maybe."},
    ])
    assert text.splitlines() == [
        "You: What's the timeline?", "Jane Roe: Q3.", "Speaker 2: Q4.",
        "Other participant: Maybe."]


def test_consecutive_lines_from_one_speaker_read_as_one_turn():
    text = format_transcript([
        {"speaker": "them", "speaker_label": "Jane Roe", "text": "We use"},
        {"speaker": "them", "speaker_label": "Jane Roe", "text": "Genesys."},
    ])
    assert text == "Jane Roe: We use Genesys."


def test_the_meeting_is_named_and_described():
    """The tick read session.meeting_name, which never existed."""
    from models.session import Session
    s = Session(session_id="S")
    s.display_name = "Acme — Connect discovery"
    s.client, s.project = "Acme", "IVR migration"
    s.organizer = "Doe, Jane"
    s.attendees = ["John Doe", "Pat Roe"]
    s.notes = "They run Genesys today."
    ctx = meeting_context(s)
    for want in ("Meeting: Acme — Connect discovery", "Client: Acme",
                 "Project: IVR migration", "Organiser: Doe, Jane",
                 "Invited: John Doe, Pat Roe", "They run Genesys today."):
        assert want in ctx
    assert not hasattr(s, "meeting_name")


def test_long_attendee_lists_and_notes_are_bounded():
    s = SimpleNamespace(display_name="", client="", project="", organizer="",
                        attendees=[f"Person {i}" for i in range(40)],
                        notes="x" * 5000)
    ctx = meeting_context(s)
    assert "(+25 more)" in ctx and len(ctx) < 2000


# ── The runner ──────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Coach:
    _provider = "anthropic"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def coach_tick(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0) if self.replies else {}


class _Transcriber:
    def recent_segments(self, last_seconds):
        return [{"speaker": "them", "speaker_label": "Jane Roe",
                 "text": "We run Genesys and want Connect by Q3."}]


def _session(sid="S1"):
    from models.session import Session
    s = Session(session_id=sid)
    s.display_name = "Acme discovery"
    s.client = "Acme"
    return s


def _runner(coach, session=None, wide=45, hot=0, clock=None):
    session = session or _session()
    state = {"deps": TickDeps(session=session, transcriber=_Transcriber(),
                              coach=coach, wide_interval_s=wide,
                              hot_interval_s=hot)}
    r = CopilotRunner(lambda: state["deps"], clock=clock or _Clock())
    return r, state, session


def test_ticks_on_schedule_without_any_panel_open():
    clock = _Clock()
    coach = _Coach([{"risks": ["Q3 cutover leaves no time for UAT"]}])
    r, _, session = _runner(coach, clock=clock)
    assert asyncio.run(r.step()) is None        # binds, nothing due yet
    clock.t += 44
    assert asyncio.run(r.step()) is None
    clock.t += 2
    payload = asyncio.run(r.step())
    assert payload["risks"] == ["Q3 cutover leaves no time for UAT"]
    assert session.copilot_board[0]["text"].startswith("Q3 cutover")
    assert len(session.copilot_ticks) == 1


def test_a_changed_interval_applies_on_the_next_pass():
    clock = _Clock()
    coach = _Coach([{"risks": ["a b c d"]}])
    r, state, _ = _runner(coach, clock=clock, wide=300)
    asyncio.run(r.step())
    clock.t += 50
    assert asyncio.run(r.step()) is None
    state["deps"].wide_interval_s = 30
    assert asyncio.run(r.step()) is not None


def test_pause_stops_ticking():
    clock = _Clock()
    r, _, _ = _runner(_Coach([{}]), clock=clock)
    asyncio.run(r.step())
    r.paused = True
    clock.t += 500
    assert asyncio.run(r.step()) is None


def test_the_model_gets_the_meeting_and_the_board():
    clock = _Clock()
    coach = _Coach([{"risks": ["Q3 cutover leaves no time for UAT"]}, {}])
    r, _, _ = _runner(coach, clock=clock)
    asyncio.run(r.tick())
    r.set_item_status(r.board.items[0].id, "dismissed")
    asyncio.run(r.tick())
    second = coach.calls[1]
    assert "Meeting: Acme discovery" in second["meeting_context"]
    assert "Client: Acme" in second["meeting_context"]
    assert "DISMISSED" in second["board_memory"]
    assert second["meeting_name"] == "Acme discovery"


def test_a_new_recording_starts_a_new_board():
    coach = _Coach([{"risks": ["a b c d"]}])
    r, state, _ = _runner(coach)
    asyncio.run(r.tick())
    assert len(r.board.items) == 1
    state["deps"].session = _session("S2")
    assert r.state()["board"] == []


def test_a_model_failure_is_reported_not_raised():
    class Boom(_Coach):
        async def coach_tick(self, **kw):
            raise TimeoutError()
    r, _, _ = _runner(Boom([]))
    payload = asyncio.run(r.tick())
    assert payload["error"] == "error"
    assert r.state()["error"] == "error"


def test_nothing_to_coach_means_no_tick():
    r = CopilotRunner(lambda: None)
    assert asyncio.run(r.tick()) is None
    assert r.state()["active"] is False


def test_hot_ticks_only_when_a_hot_interval_is_set():
    clock = _Clock()
    r, state, _ = _runner(_Coach([{}, {}]), clock=clock, wide=300, hot=0)
    asyncio.run(r.step())
    clock.t += 30
    assert r.due(state["deps"]) is None
    state["deps"].hot_interval_s = 15
    assert r.due(state["deps"]) is True


# ── Through the real endpoints ──────────────────────────────────────

@pytest.fixture
def server_with_recording(monkeypatch):
    from tests._app_import import import_app
    import_app()
    import server
    session = _session()
    coach = _Coach([{"clarifying_questions": [
        "Ask Jane Roe which Genesys features they rely on"]}])
    coach.answer_question = None
    rec = SimpleNamespace(is_recording=True, current_session=session,
                          live_transcriber=SimpleNamespace(
                              is_running=True,
                              recent_segments=_Transcriber().recent_segments))
    settings = SimpleNamespace(
        live_copilot_enabled=True, live_copilot_wide_interval_sec=45,
        live_copilot_hot_interval_sec=0, live_copilot_mode="SA",
        live_copilot_meeting_type="General", copilot_custom_context="")
    monkeypatch.setattr(server.svc, "settings", settings)
    monkeypatch.setattr(server.svc, "load_settings", lambda: settings)
    monkeypatch.setattr(server.svc, "recording_svc", rec)
    monkeypatch.setattr(server.svc, "live_summarizer", coach)
    monkeypatch.setattr(server.svc, "copilot_mode_svc", None)
    monkeypatch.setattr(server.svc, "copilot_meeting_type_svc", None)
    monkeypatch.setattr(server, "_copilot_runner",
                        server._make_copilot_runner())
    return server, session, coach


def test_refresh_returns_the_tick_and_the_board(server_with_recording):
    server, session, _ = server_with_recording
    payload = asyncio.run(server.copilot_tick())
    assert payload["clarifying_questions"]
    assert payload["board"][0]["status"] == "open"
    state = asyncio.run(server.copilot_state())
    assert state["active"] and len(state["board"]) == 1


def test_marking_an_item_done_persists_on_the_session(server_with_recording):
    server, session, _ = server_with_recording
    asyncio.run(server.copilot_tick())
    item_id = asyncio.run(server.copilot_state())["board"][0]["id"]
    out = asyncio.run(server.copilot_set_item(
        item_id, server.CoPilotItemRequest(status="done")))
    assert out["status"] == "done"
    assert session.copilot_board[0]["status"] == "done"


def test_an_unknown_item_is_404_and_a_bad_status_400(server_with_recording):
    from fastapi import HTTPException
    server, _, _ = server_with_recording
    asyncio.run(server.copilot_tick())
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.copilot_set_item(
            "nope", server.CoPilotItemRequest(status="done")))
    assert e.value.status_code == 404
    item_id = asyncio.run(server.copilot_state())["board"][0]["id"]
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.copilot_set_item(
            item_id, server.CoPilotItemRequest(status="archived")))
    assert e.value.status_code == 400


def test_ask_answers_from_the_call_and_keeps_the_exchange(
        server_with_recording):
    server, session, coach = server_with_recording
    seen = {}

    async def answer(question, segments, meeting_context="",
                     custom_context=""):
        seen.update(question=question, segments=segments,
                    context=meeting_context)
        return "Jane Roe said Q3."
    coach.answer_question = answer
    out = asyncio.run(server.copilot_ask(
        server.CoPilotAskRequest(question="When do they want to go live?")))
    assert out["answer"] == "Jane Roe said Q3."
    assert "Meeting: Acme discovery" in seen["context"]
    assert session.copilot_qa[0]["question"].startswith("When do they")


def test_ask_rejects_an_empty_question(server_with_recording):
    from fastapi import HTTPException
    server, _, _ = server_with_recording
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.copilot_ask(server.CoPilotAskRequest(question=" ")))
    assert e.value.status_code == 400


def test_disabled_is_403_and_not_recording_is_409(server_with_recording,
                                                  monkeypatch):
    from fastapi import HTTPException
    server, _, _ = server_with_recording
    server.svc.settings.live_copilot_enabled = False
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.copilot_tick())
    assert e.value.status_code == 403
    server.svc.settings.live_copilot_enabled = True
    server.svc.recording_svc.is_recording = False
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.copilot_tick())
    assert e.value.status_code == 409


def test_the_summary_leaves_out_what_the_user_dismissed():
    from tests._app_import import import_app
    import_app()
    import server
    s = _session()
    s.copilot_ticks = [{"risks": ["Vendor lock-in with the CCaaS platform",
                                  "Q3 cutover leaves no time for UAT"]}]
    s.copilot_board = [{"kind": "risks", "status": "dismissed",
                        "text": "Lock-in to the CCaaS platform vendor"}]
    blob = server._copilot_observations_blob(s)
    assert "UAT" in blob
    assert "lock-in" not in blob.lower()


# ── The payload the panel is tested against ─────────────────────────

import json  # noqa: E402
from pathlib import Path  # noqa: E402

PANEL_FIXTURE = (Path(__file__).resolve().parents[2] / "src" / "lib"
                 / "__fixtures__" / "copilot-state.json")


def _state_payload(server):
    """A board with one item in each state, a Q&A exchange, through the
    real handlers."""
    async def answer(question, segments, meeting_context="",
                     custom_context=""):
        return "Jane Roe said they want Connect live by Q3."
    server.svc.live_summarizer.answer_question = answer
    server.svc.live_summarizer.replies = [
        {"clarifying_questions": [
            "Ask Jane Roe which Genesys routing features they rely on"],
         "risks": ["Q3 go-live leaves no time for UAT",
                   "Vendor lock-in with the CCaaS platform"],
         "follow_ups": ["Send the routing workbook to Pat Roe"]},
        {"risks": ["Number porting lead time from the current carrier"]},
    ]
    asyncio.run(server.copilot_tick())
    board = asyncio.run(server.copilot_state())["board"]
    by_text = {i["text"]: i["id"] for i in board}
    for text, status in (
            ("Q3 go-live leaves no time for UAT", "done"),
            ("Vendor lock-in with the CCaaS platform", "dismissed"),
            ("Send the routing workbook to Pat Roe", "saved")):
        asyncio.run(server.copilot_set_item(
            by_text[text], server.CoPilotItemRequest(status=status)))
    asyncio.run(server.copilot_tick())
    asyncio.run(server.copilot_ask(server.CoPilotAskRequest(
        question="When do they want to go live?")))
    return json.loads(json.dumps(asyncio.run(server.copilot_state())))


def test_the_panel_fixture_is_what_the_endpoint_sends(server_with_recording):
    """src/lib/__fixtures__/copilot-state.json was captured from
    _state_payload(). Held to the producer by SHAPE — keys and types of
    the state and of each board item — so a renamed field fails here
    rather than the panel's tests passing against a payload nobody
    sends."""
    server, _, _ = server_with_recording

    def shape(d):
        return {k: type(v).__name__ for k, v in sorted(d.items())}

    live = _state_payload(server)
    fixture = json.loads(PANEL_FIXTURE.read_text(encoding="utf-8"))
    assert shape(fixture) == shape(live)
    assert [shape(i) for i in fixture["board"]] == \
        [shape(i) for i in live["board"]]
    assert {i["status"] for i in live["board"]} == {
        "open", "done", "dismissed", "saved"}
