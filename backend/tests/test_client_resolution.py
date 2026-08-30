"""
Server-side client resolution for calendar meetings.

THE COMPLAINT
-------------
"For auto record, is there anyway to try and associate a calendar with a
client that way I dont have to go back and do that in the session? For
example it autorecorded a call for <client> for me but I had to go back
into the session and save the client after the call was completed."

WHY NOTHING FIRED
-----------------
`suggestClientFromAttendees` (record-view.tsx) existed and learned from
tagging history, but it could not help this user:

  * it keys entirely on attendee EMAIL DOMAINS and returns null the
    moment there are none — and extension-sourced calendar events carry
    attendee NAMES only, no emails; and
  * it is frontend-only, reached when the user clicks **Use**, while
    auto-record starts server-side and never goes near it.

The signal was in the meeting SUBJECT, and the client was already in the
user's client list.

WHAT THIS FILE PINS DOWN
------------------------
  1. Subject matching on a real-shaped title, with the client's name
     glued to punctuation the way calendar subjects actually are.
  2. THE BOUNDARY RULE. `_` / `-` / `/` and digits are separators (a
     `\\b`-based pattern is not — `_` is a word character, which is
     exactly how `\\bHooli\\b` failed to match `transcript_Hooli` in this
     repo), and a short client name is never matched inside a longer
     word.
  3. Longest name wins for NESTED matches; two genuinely different
     clients matching means NOTHING is tagged. A wrongly-tagged session
     silently files a client conversation under another client and the
     export worker copies it into that client's Designated Folder.
  4. The attendee-domain path still works where emails exist — the
     users it already served must not regress.
  5. Provenance is recorded on the session, so an auto-applied client
     can explain itself.
  6. A resolution failure NEVER costs a recording.
  7. The auto prep-brief loop now gets a resolved client, instead of the
     hard-coded `client=""` that made it retrieve zero Knowledge Folder
     documents.

Client names throughout are the repo's placeholders (Acme / Globex /
Initech / Umbrella / Zorg / Northwind), never a real customer.
"""

from __future__ import annotations

import asyncio
import time
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("dotenv", MagicMock())

from _app_import import import_app  # noqa: E402
from services.client_resolution_service import (  # noqa: E402
    METHOD_AMBIGUOUS,
    METHOD_DOMAIN,
    METHOD_NONE,
    METHOD_SUBJECT,
    known_client_names,
    match_clients_in_subject,
    resolve_client,
    subject_contains_client,
)

import_app()
import server  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────

def _session(client="", project="", attendees=(), started_at="2026-08-01T09:00:00"):
    return {
        "session_id": "S" + str(abs(hash((client, project, started_at))))[:6],
        "client": client,
        "project": project,
        "attendees": list(attendees),
        "started_at": started_at,
    }


def _configs(*names):
    return {n.strip().lower(): {"display_name": n} for n in names}


# ── 1. subject match on a real-shaped title ──────────────────────────

def test_subject_match_on_a_real_shaped_meeting_title():
    """The reported case, anonymised: the client name is the first token
    of a hyphen-glued, acronym-heavy invite subject, and the client is
    already configured."""
    res = resolve_client(
        subject="Acme-Globex Connect MVP DeliveryNotice Flow",
        attendees=["Doe, Jane", "Roe, Richard"],  # names only — no emails
        client_configs=_configs("Acme"),
        sessions=[],
    )
    assert res.client == "Acme"
    assert res.method == METHOD_SUBJECT
    assert res.resolved


def test_candidate_clients_come_from_sessions_as_well_as_configs():
    """A client the user only ever typed onto a session — never created
    in Settings — still counts as a candidate."""
    assert known_client_names({}, [_session(client="Northwind")]) == ["Northwind"]

    res = resolve_client(
        subject="Northwind quarterly review",
        client_configs={},
        sessions=[_session(client="Northwind")],
    )
    assert (res.client, res.method) == ("Northwind", METHOD_SUBJECT)


def test_configured_display_casing_wins_over_session_casing():
    names = known_client_names(
        _configs("Acme Financial"), [_session(client="acme financial")])
    assert names == ["Acme Financial"]


# ── 2. the boundary rule ─────────────────────────────────────────────

@pytest.mark.parametrize("subject", [
    "transcript_Zorg_final",         # underscore — the \b bug's shape
    "Zorg-Umbrella kickoff",         # hyphen
    "Programme/Zorg/Stream 2",       # slash
    "Zorg2026 planning",             # digit immediately after
    "2026Zorg planning",             # digit immediately before
    "Weekly sync (Zorg)",            # parentheses
    "ZORG steering committee",       # casing
    "Re: zorg | status",             # pipe + casing
])
def test_client_name_matches_across_non_letter_adjacency(subject):
    """`_`, `-`, `/`, digits and punctuation are all separators. `\\b`
    would fail the first and both digit cases outright."""
    assert subject_contains_client(subject, "Zorg"), subject


@pytest.mark.parametrize("subject", [
    "Zorgeous product review",       # longer word, same prefix
    "Reorganisation planning",       # substring mid-word
    "Zorgs",                         # trailing letter
    "AZorg",                         # leading letter
])
def test_short_client_name_is_not_matched_inside_a_longer_word(subject):
    assert not subject_contains_client(subject, "Zorg"), subject


def test_client_name_with_a_digit_stays_a_distinct_token():
    """`Initech 360` must not collapse onto `Initech` — digits are
    separators for BOUNDARY purposes but are kept as their own tokens."""
    assert subject_contains_client("Initech 360 discovery", "Initech 360")
    assert not subject_contains_client("Initech discovery", "Initech 360")


def test_multi_word_client_matches_across_odd_separators():
    assert subject_contains_client(
        "ACME_FINANCIAL-Q3/roadmap", "Acme Financial")


# ── 3. longest wins; ambiguity is refused ────────────────────────────

def test_longest_matching_client_name_wins_for_nested_names():
    """"Acme" and "Acme Financial" are one client written at two levels
    of specificity — the more specific one is the answer, NOT an
    ambiguity."""
    matches = match_clients_in_subject(
        "Acme Financial onboarding call", ["Acme", "Acme Financial"])
    assert matches == ["Acme Financial"]

    res = resolve_client(
        subject="Acme Financial onboarding call",
        client_configs=_configs("Acme", "Acme Financial"),
        sessions=[],
    )
    assert (res.client, res.method) == ("Acme Financial", METHOD_SUBJECT)


def test_two_different_clients_in_the_subject_tags_neither():
    """A wrongly-tagged session is worse than an untagged one: the export
    worker would copy this client's artifacts into the OTHER client's
    Designated Folder."""
    res = resolve_client(
        subject="Acme / Globex joint architecture review",
        client_configs=_configs("Acme", "Globex"),
        sessions=[],
    )
    assert res.client == ""
    assert res.project == ""
    assert res.method == METHOD_AMBIGUOUS
    # …and it says WHY, naming both, so the user isn't left guessing.
    assert "Acme" in res.detail and "Globex" in res.detail


def test_ambiguity_does_not_fall_through_to_the_domain_signal():
    """Two clients in the title is a genuine conflict about THIS meeting.
    Quietly answering from a weaker signal instead would produce exactly
    the confident-but-wrong tag the ambiguity check exists to prevent."""
    res = resolve_client(
        subject="Acme / Globex joint architecture review",
        attendees=["ops@globex.example"],
        client_configs=_configs("Acme", "Globex"),
        sessions=[_session(client="Globex", attendees=["ops@globex.example"])],
    )
    assert res.client == ""
    assert res.method == METHOD_AMBIGUOUS


# ── 4. the attendee-domain path must not regress ─────────────────────

def test_domain_history_still_resolves_when_attendees_carry_emails():
    """A local-calendar meeting with real addresses, no client name in
    the subject: the ported history-learning signal answers, exactly as
    the frontend did."""
    history = [
        _session(client="Umbrella",
                 attendees=["me@hooli.example", "ops@umbrella.example"]),
        _session(client="Umbrella",
                 attendees=["me@hooli.example", "cto@umbrella.example"]),
        _session(client="Initech",
                 attendees=["me@hooli.example", "pm@initech.example"]),
    ]
    res = resolve_client(
        subject="Weekly status",  # no client name anywhere in it
        attendees=["me@hooli.example", "ops@umbrella.example"],
        client_configs=_configs("Umbrella", "Initech"),
        sessions=history,
    )
    assert res.client == "Umbrella"
    assert res.method == METHOD_DOMAIN


def test_own_domain_alone_never_resolves_a_client():
    """Every meeting contains the user's own address; counting it would
    make every meeting look like every client."""
    history = [
        _session(client="Umbrella",
                 attendees=["me@hooli.example", "ops@umbrella.example"]),
        _session(client="Initech",
                 attendees=["me@hooli.example", "pm@initech.example"]),
    ]
    res = resolve_client(
        subject="Internal planning",
        attendees=["me@hooli.example"],
        client_configs={},
        sessions=history,
    )
    assert res.client == ""
    assert res.method == METHOD_NONE


def test_domain_tie_is_not_an_answer():
    history = [
        _session(client="Umbrella", attendees=["a@shared.example"]),
        _session(client="Initech", attendees=["a@shared.example"]),
    ]
    res = resolve_client(
        subject="Sync", attendees=["a@shared.example"],
        client_configs={}, sessions=history)
    assert res.client == ""


def test_name_only_attendees_produce_no_domain_match():
    """The extension case that made the old resolver useless — this is
    why the subject signal has to exist."""
    res = resolve_client(
        subject="Weekly status",
        attendees=["Doe, Jane [EMEA]", "Poe, Edgar"],
        client_configs=_configs("Umbrella"),
        sessions=[_session(client="Umbrella",
                           attendees=["ops@umbrella.example"])],
    )
    assert res.client == ""
    assert res.method == METHOD_NONE


def test_subject_outranks_domain_history():
    """Signal order: the title states the client outright; domains only
    infer it."""
    res = resolve_client(
        subject="Initech platform review",
        attendees=["ops@umbrella.example"],
        client_configs=_configs("Initech", "Umbrella"),
        sessions=[_session(client="Umbrella",
                           attendees=["ops@umbrella.example"])],
    )
    assert (res.client, res.method) == ("Initech", METHOD_SUBJECT)


# ── 5. project ───────────────────────────────────────────────────────

def test_most_recent_project_under_the_resolved_client_is_carried():
    res = resolve_client(
        subject="Acme delivery checkpoint",
        client_configs=_configs("Acme"),
        sessions=[
            _session(client="Acme", project="Phase 1",
                     started_at="2026-01-05T09:00:00"),
            _session(client="Acme", project="Phase 2",
                     started_at="2026-07-30T09:00:00"),
            _session(client="Globex", project="Rollout",
                     started_at="2026-08-02T09:00:00"),
        ],
    )
    assert (res.client, res.project) == ("Acme", "Phase 2")


def test_no_client_means_no_project():
    res = resolve_client(
        subject="Untitled sync",
        client_configs=_configs("Acme"),
        sessions=[_session(client="Acme", project="Phase 2")],
    )
    assert (res.client, res.project) == ("", "")


# ── 6. wiring: the record-start path ─────────────────────────────────

class _StartHarness:
    """server._auto_record_start with only its outer edges faked: the
    saved-devices lookup, the device index lookups and the sync start
    call. Everything between — including client resolution — is real."""

    def __init__(self, monkeypatch, *, configs=None, sessions=None):
        self.requests: list = []
        monkeypatch.setattr(
            server.svc, "settings",
            SimpleNamespace(is_configured=False,
                            live_transcription_enabled=False,
                            auto_record_enabled=True))
        monkeypatch.setattr(server.svc, "models_ready", True)
        monkeypatch.setattr(server.svc, "models_loading", False)
        monkeypatch.setattr(server.svc, "auto_record_subject", None)
        monkeypatch.setattr(server.svc, "auto_record_skip_reason", None)
        monkeypatch.setattr(
            server.svc, "client_cfg_svc",
            SimpleNamespace(get_all=lambda: dict(configs or {})))
        monkeypatch.setattr(
            server.svc, "session_svc",
            SimpleNamespace(list_sessions=lambda: list(sessions or [])))
        monkeypatch.setattr(
            server, "_read_last_devices",
            lambda: {"mic_name": "Mic", "output_name": "Speakers"})
        monkeypatch.setattr(server, "_input_index_for_name", lambda n: 1)
        monkeypatch.setattr(server, "_output_index_for_name", lambda n: 2)
        monkeypatch.setattr(
            server, "_start_recording_sync", self.requests.append)

    def start(self, subject, attendees=(), minutes=30):
        start = datetime.now().replace(second=0, microsecond=0)
        server._auto_record_start({
            "subject": subject,
            "start": start,
            "end": start + timedelta(minutes=minutes),
            "attendees": list(attendees),
            "source": "extension",
        })
        return self.requests[-1] if self.requests else None


def test_auto_record_start_tags_the_session_at_creation(monkeypatch):
    """The whole point: the session arrives tagged, so the user never has
    to reopen it after the call to file it."""
    h = _StartHarness(
        monkeypatch,
        configs=_configs("Acme"),
        sessions=[_session(client="Acme", project="Phase 2",
                           started_at="2026-08-01T09:00:00")],
    )
    req = h.start("Acme-Globex Connect MVP DeliveryNotice Flow",
                  attendees=["Doe, Jane"])
    assert req.client == "Acme"
    assert req.project == "Phase 2"
    assert req.client_source == METHOD_SUBJECT
    assert "Acme" in (req.client_source_detail or "")


def test_auto_record_start_records_provenance_when_it_declines_to_guess(
        monkeypatch):
    """An auto-recording that comes back untagged must be able to say why
    — otherwise it just looks broken."""
    h = _StartHarness(monkeypatch, configs=_configs("Acme", "Globex"))
    req = h.start("Acme / Globex joint architecture review")
    assert req.client == ""
    assert req.client_source == METHOD_AMBIGUOUS
    assert "Acme" in req.client_source_detail
    assert "Globex" in req.client_source_detail


def test_resolution_failure_does_not_prevent_the_recording(monkeypatch):
    """THE HARD CONSTRAINT. Client tagging is additive; a tagging failure
    must never cost someone a recording."""
    h = _StartHarness(monkeypatch, configs=_configs("Acme"))

    def _boom(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(
        server.svc, "session_svc",
        SimpleNamespace(list_sessions=_boom))

    req = h.start("Acme delivery checkpoint")
    assert req is not None, "recording did not start"
    assert req.meeting_name  # the real start request, fully formed
    assert req.mic_device_index == 1
    assert req.client == ""  # degraded to untagged, exactly as before


def test_resolution_helper_swallows_a_broken_client_config_store(monkeypatch):
    """An un-downloaded cloud client_configs.json raises from get_all().
    Session-derived client names still work, so resolution carries on."""
    monkeypatch.setattr(
        server.svc, "client_cfg_svc",
        SimpleNamespace(get_all=MagicMock(side_effect=OSError("placeholder"))))
    monkeypatch.setattr(
        server.svc, "session_svc",
        SimpleNamespace(list_sessions=lambda: [_session(client="Northwind")]))

    got = server._resolve_client_for_meeting("Northwind quarterly review", [])
    assert got["client"] == "Northwind"
    assert got["method"] == METHOD_SUBJECT


def test_manual_start_request_carries_no_provenance():
    """The manual Start button sets the client itself; there is nothing
    to explain, so the field stays None rather than claiming a source."""
    req = server.StartRecordingRequest(meeting_name="x", client="Acme")
    assert req.client_source is None
    assert req.client_source_detail is None


# ── 7. provenance survives the session round trip ────────────────────

def test_provenance_round_trips_through_session_json():
    from models.session import Session
    s = Session(session_id="S1")
    s.client = "Acme"
    s.client_source = METHOD_SUBJECT
    s.client_source_detail = "Matched “Acme” in the meeting title."
    back = Session.from_dict(s.to_dict())
    assert back.client_source == METHOD_SUBJECT
    assert back.client_source_detail == s.client_source_detail


def test_legacy_session_json_reads_as_hand_tagged():
    """No key = the user typed it. Old sessions load unchanged."""
    from models.session import Session
    back = Session.from_dict({"session_id": "S1", "client": "Acme"})
    assert back.client_source is None


def test_provenance_is_on_the_session_summary(tmp_path):
    """Surfaced on the LIST shape too, so the Sessions view can badge an
    auto-tagged client without loading each session's full JSON."""
    from services.session_service import SessionService
    svc_ = SessionService(recordings_dir=tmp_path)
    summary = svc_._build_summary("S1", {
        "client": "Acme",
        "client_source": METHOD_SUBJECT,
        "client_source_detail": "Matched “Acme” in the meeting title.",
    })
    assert summary["client_source"] == METHOD_SUBJECT
    assert summary["client_source_detail"]


# ── 8. the auto prep-brief loop gets a resolved client ───────────────

def test_auto_prep_brief_receives_a_resolved_client(monkeypatch):
    """Closes the documented gap: the loop used to build
    PrepBriefFromMeetingRequest(client="", project=""), so automatic
    briefs fell back to corpus-wide recent sessions and retrieved NO
    Knowledge Folder documents (retrieval is gated on a client)."""
    captured: list = []
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay, *a, **k):
        await real_sleep(0)

    async def fake_brief(req):
        captured.append(req)
        return {"markdown": "brief", "related_count": 1, "document_count": 3}

    start_iso = (datetime.now() + timedelta(minutes=5)).isoformat()

    monkeypatch.setattr(
        server.svc, "settings",
        SimpleNamespace(auto_prep_brief_enabled=True,
                        auto_prep_brief_lead_min=10))
    monkeypatch.setattr(server.svc, "summarizer", object())
    monkeypatch.setattr(
        server.svc, "prep_brief_cache_svc",
        SimpleNamespace(has=lambda k: False, put=lambda payload: None))
    monkeypatch.setattr(
        server.svc, "client_cfg_svc",
        SimpleNamespace(get_all=lambda: _configs("Acme")))
    monkeypatch.setattr(
        server.svc, "session_svc",
        SimpleNamespace(list_sessions=lambda: [
            _session(client="Acme", project="Phase 2",
                     started_at="2026-08-01T09:00:00")]))
    monkeypatch.setattr(
        server, "get_upcoming_meetings",
        lambda hours: [{"subject": "Acme delivery checkpoint",
                        "start": start_iso, "end": "", "attendees": []}])
    monkeypatch.setattr(server, "prep_brief_from_meeting", fake_brief)
    monkeypatch.setattr(server.asyncio, "sleep", fast_sleep)

    async def _drive():
        # A DEADLINE, not a yield budget. This used to spin
        # `for _ in range(500): await sleep(0)` — 500 event-loop turns,
        # which is generous on an idle machine and a coin flip on a
        # loaded CI runner, where the scheduler interleaves differently
        # and the loop under test may not have got far enough. Verified
        # by reproduction: 10/10 passes idle, 2 failures in 6 runs under
        # CPU contention, with the code under test untouched.
        #
        # Wall-clock is the right bound because what is being asserted
        # is "the loop eventually produces a brief", not "it produces
        # one within N scheduler turns". asyncio.sleep is monkeypatched
        # to a no-op above, so this cannot actually take 5s unless
        # something is genuinely wrong.
        task = asyncio.create_task(server._auto_prep_brief_loop())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not captured:
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())

    assert captured, "auto-brief loop never generated a brief"
    assert captured[0].client == "Acme"
    assert captured[0].project == "Phase 2"
