"""The invite's ORGANISER, carried from the calendar event to the session.

THE GAP THIS CLOSES
-------------------
v2.40.0 shipped a speaker-identification roster (core/speaker_roster.py)
built from ``Session.attendees``. For a user whose entire calendar is
Chrome-extension-sourced that list is ALWAYS EMPTY, so the feature was
inert for them: the Record tab's expanded meeting row read

    ATTENDEES (0)
    None listed.

on every meeting. The extension scrapes Outlook Web's calendar GRID, and
the grid's ``aria-label`` carries the organiser but not the attendee list
— attendees live in the event detail pane, one click deeper, for exactly
the same reason join links have to be resolved separately.

v2.40.0 also taught the extension to capture that organiser (the ``By
Jane Doe`` tail of the label), and ``roster_names(attendees, organizer)``
already accepted an organiser and put it first. Nothing connected the
two: ``StartRecordingRequest`` had no organiser field, ``Session`` had no
organiser field, and the roster call site read
``getattr(session, "organizer", "")`` off an object that never had one.
So the one invite-derived name that path CAN supply was captured and
discarded.

WHAT THIS FILE PINS DOWN
------------------------
  1. ``Session.organizer`` round-trips through ``to_dict``/``from_dict``,
     and a session written before the field existed reads as "" rather
     than raising.
  2. Both record-start paths set it: ``server._auto_record_start`` (the
     calendar auto-recorder) and the request the Record tab's Use button
     sends. A backend-only fix would leave every manually started
     meeting without one.
  3. The roster actually RECEIVES it — asserted on the prompt string, no
     model call, the same way test_speaker_roster.py does — and an
     organiser with NO attendees produces a non-empty roster. That last
     one is the whole point: it is the extension user's entire roster.
  4. FAILING TO RESOLVE AN ORGANISER NEVER BLOCKS A RECORDING. Calendar
     display and auto-record were broken for weeks and fixed across
     v2.30.0–v2.34.0; a name is worth nothing next to a recording that
     cannot be re-run, so the organiser lookup is total by construction
     and the start path is belt-and-braces on top of it.

Every name here is fictional and every domain is a reserved
``.example`` — see AGENTS.md.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# summarizer.py imports the anthropic SDK at module load and
# config.settings imports python-dotenv; neither is in the lightweight
# test env. Same stub pattern as test_speaker_roster.py.
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from _app_import import import_app  # noqa: E402
from core.speaker_roster import roster_names  # noqa: E402
from core.summarizer import Summarizer  # noqa: E402
from models.segment import Segment  # noqa: E402
from models.session import Session  # noqa: E402

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

# The organiser forms the three calendar sources actually deliver:
# a plain display name (extension aria-label / EventKit), the
# surname-first-with-suffix-and-region form Outlook produces, and an
# address (Outlook COM's `Organizer` can be either).
ORGANIZER_PLAIN = "Jane Doe"
ORGANIZER_OUTLOOK_FORM = "Roe, Pat Jr. [EMEA]"
ORGANIZER_ADDRESS = "sam.noh@initech.example"

TRANSCRIPT = (
    "[00:00 → 00:09] SPEAKER_00: Right, let's start. Jane, where did we "
    "land on the Globex routing map?\n"
    "[00:09 → 00:24] SPEAKER_01: Six intents, and the overflow queue "
    "still has no owner.\n"
    "[00:24 → 00:31] SPEAKER_00: Thanks, Jane. I'll take the overflow "
    "queue.\n"
)


# ── 1. persistence ───────────────────────────────────────────────────

def test_organizer_round_trips_through_to_dict_and_from_dict():
    """The field has to survive the JSON on disk, or the roster only
    ever sees it for a session that is still in memory — and speaker
    identification runs AFTER the recording, off the reloaded session."""
    session = Session(session_id="s-round-trip")
    session.organizer = ORGANIZER_OUTLOOK_FORM

    data = session.to_dict()
    assert data["organizer"] == ORGANIZER_OUTLOOK_FORM

    back = Session.from_dict(data)
    assert back.organizer == ORGANIZER_OUTLOOK_FORM
    # Not re-ordered, not tag-stripped, not comma-split. The prompt
    # tells the model to read this form; doing it in Python is the
    # regression owner_service.py's no-comma-split rule exists to
    # prevent ("Roe, Pat Jr." is ONE person's whole name).
    assert back.organizer == session.organizer


def test_a_legacy_session_without_the_key_reads_as_empty_not_an_error():
    """Every session JSON written before this field existed lacks the
    key, as does every ad-hoc recording that was never started from a
    calendar entry. Both must load, and both must read as "" — the same
    value a resolved-but-absent organiser produces, which
    roster_names() drops."""
    legacy = {
        "session_id": "s-legacy",
        "display_name": "Acme sync",
        "attendees": [],
        # note: no "organizer" key at all
    }
    session = Session.from_dict(legacy)
    assert session.organizer == ""
    # And it survives a re-save, so the next load is identical.
    assert Session.from_dict(session.to_dict()).organizer == ""


@pytest.mark.parametrize("stored", [None, "", 0, False])
def test_a_null_or_blank_stored_organizer_reads_as_the_empty_string(stored):
    """A store written by an older/other writer can carry the key with
    a null. `str(None)` would be the string "None" — a fake organiser,
    and the roster would print it as a person."""
    session = Session.from_dict({"session_id": "s", "organizer": stored})
    assert session.organizer == ""
    assert roster_names([], session.organizer) == []


# ── 2a. the auto-record path ─────────────────────────────────────────

def _meeting(subject: str = "Globex routing sync", *,
             organizer: str = ORGANIZER_PLAIN,
             attendees=None,
             source: str = "extension") -> dict:
    """One merged calendar meeting, in the shape calendar_feed hands to
    AutoRecordService — naive-local datetimes, `organizer` present."""
    start = datetime.now().replace(second=0, microsecond=0)
    return {
        "subject": subject,
        "start": start,
        "end": start + timedelta(minutes=30),
        "location": "",
        "organizer": organizer,
        "attendees": list(attendees or []),
        "duration": 30,
        "join_url": "",
        "source": source,
    }


@pytest.fixture
def auto_start(monkeypatch):
    """`server._auto_record_start` with only its outermost edges faked:
    the saved-device lookup, client resolution, and the sync start call
    whose request we capture. Everything between is the real code."""
    captured: list = []

    monkeypatch.setattr(
        server, "_read_last_devices",
        lambda: {"mic_name": "Test Mic", "output_name": "Test Out"})
    monkeypatch.setattr(server, "_input_index_for_name", lambda name: 1)
    monkeypatch.setattr(server, "_output_index_for_name", lambda name: 2)
    monkeypatch.setattr(
        server, "_resolve_client_for_meeting",
        lambda subject, attendees: {
            "client": "", "project": "", "method": "none", "detail": ""})
    monkeypatch.setattr(server, "_start_recording_sync", captured.append)
    # is_configured False short-circuits the model pre-warm thread.
    monkeypatch.setattr(
        server.svc, "settings",
        SimpleNamespace(is_configured=False, live_transcription_enabled=False,
                        auto_record_enabled=True))
    return captured


def test_the_auto_record_path_sets_the_organizer_on_the_request(auto_start):
    """The auto-recorder already has the meeting in hand — it pulls
    client/project off it the same way. The organiser rides along."""
    server._auto_record_start(_meeting())

    assert len(auto_start) == 1
    assert auto_start[0].organizer == ORGANIZER_PLAIN


def test_the_auto_record_path_preserves_the_outlook_surname_first_form(auto_start):
    """Local Outlook's `Organizer` is often `Last, First Suffix
    [REGION]`. Passed through unparsed — core/speaker_roster.py and the
    prompt own the reading of it."""
    server._auto_record_start(_meeting(organizer=ORGANIZER_OUTLOOK_FORM))
    assert auto_start[0].organizer == ORGANIZER_OUTLOOK_FORM


def test_a_meeting_with_no_organizer_still_starts_with_an_empty_one(auto_start):
    """An extension row whose aria-label had no `By …` segment. Normal,
    not an error: "" is what every pre-organiser session already is."""
    server._auto_record_start(_meeting(organizer=""))
    assert auto_start[0].organizer == ""


# ── 2b. the manual path ──────────────────────────────────────────────

def test_the_manual_start_path_sets_the_organizer_on_the_session(monkeypatch):
    """The Record tab's Use button — `POST /recording/start` →
    `_start_recording_sync`. A backend that only wired the auto path
    would leave every manually started meeting without an organiser,
    which is most of them."""
    session = Session(session_id="s-manual")
    monkeypatch.setattr(
        server.svc, "recording_svc",
        SimpleNamespace(start_recording=lambda **kw: session))

    req = server.StartRecordingRequest(
        meeting_name="Globex routing sync",
        organizer=ORGANIZER_PLAIN,
        attendees=[],
    )
    out = server._start_recording_sync(req)

    assert out is session
    assert session.organizer == ORGANIZER_PLAIN
    # And it lands on the persisted shape, not just the live object.
    assert session.to_dict()["organizer"] == ORGANIZER_PLAIN


def test_a_request_that_omits_the_organizer_starts_exactly_as_before(monkeypatch):
    """An older frontend, a script, or the Start button on an ad-hoc
    recording. The field defaults to "" and nothing else changes."""
    session = Session(session_id="s-adhoc")
    monkeypatch.setattr(
        server.svc, "recording_svc",
        SimpleNamespace(start_recording=lambda **kw: session))

    req = server.StartRecordingRequest(meeting_name="Ad-hoc")
    assert req.organizer == ""
    server._start_recording_sync(req)
    assert session.organizer == ""


def test_the_record_view_sends_the_organizer_it_took_off_the_tile():
    """The frontend half of the manual path. record-view.tsx can't be
    imported here, so this reads the source the way
    test_crash_recency.py and test_diagnostics_export.py's shell checks
    do: the Use handler has to STORE the tile's organiser and the start
    call has to SEND it. Either half alone is a no-op."""
    tsx = REPO_ROOT / "src" / "components" / "record-view.tsx"
    if not tsx.exists():  # pragma: no cover - source-tree-only check
        pytest.skip("Frontend source not present in this tree")
    source = tsx.read_text(encoding="utf-8")

    assert "setOrganizer(m.organizer || \"\")" in source, (
        "record-view.tsx's Use handler must take the organiser off the "
        "calendar tile — without it a manually started meeting reaches "
        "the backend with no organiser and the roster loses its lead "
        "name.")
    assert "        organizer,\n" in source, (
        "record-view.tsx must send `organizer` in the /recording/start "
        "payload; storing it in state alone does nothing.")

    api_ts = REPO_ROOT / "src" / "lib" / "api.ts"
    assert "organizer?: string;" in api_ts.read_text(encoding="utf-8"), (
        "api.ts's startRecording body must declare `organizer`, or the "
        "field is dropped before the request is built.")


# ── 3. the roster actually receives it ───────────────────────────────

def _capturing_summarizer(reply: str = "{}"):
    """A Summarizer whose `_chat` records the prompt instead of calling
    a model. Same pattern as test_speaker_roster.py /
    test_no_invented_precision.py — the prompt is the thing that ships."""
    s = Summarizer(api_key="x", model="claude-haiku-4-5",
                   provider="anthropic")
    prompts: list[str] = []

    async def _fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return reply

    s._chat = _fake_chat  # type: ignore[assignment]
    return s, prompts


def test_an_organizer_with_no_attendees_produces_a_non_empty_roster():
    """THE assertion this whole change exists for. The extension user
    has `attendees == []` on every session, so before the organiser was
    carried through, `roster_names` returned [] and `roster_block`
    returned "" — the roster feature was inert for them. One organiser
    is a roster of one, and one is the difference between "Jane" and
    "Jane Doe" reaching follow_up_recipients.py."""
    assert roster_names([], ORGANIZER_PLAIN) == ["Jane Doe"]
    assert roster_names(attendees=[], organizer=ORGANIZER_ADDRESS) == ["Sam Noh"]
    # The surname-first form is carried verbatim for the prompt to read.
    assert roster_names([], ORGANIZER_OUTLOOK_FORM) == [ORGANIZER_OUTLOOK_FORM]


def test_the_organizer_reaches_the_speaker_id_prompt_with_no_attendees():
    """End of the wire, asserted on the prompt string. No model call."""
    s, prompts = _capturing_summarizer()
    asyncio.run(s.identify_speakers(
        TRANSCRIPT, attendees=[], organizer=ORGANIZER_PLAIN))

    prompt = prompts[0]
    assert "=== INVITE ROSTER" in prompt
    assert "\n- Jane Doe\n" in prompt
    assert "PREFER THE ROSTER'S FULLER FORM." in prompt
    # The guard against the failure the roster itself introduces is
    # still stated against this input.
    assert "BEING INVITED IS NOT EVIDENCE OF SPEAKING." in prompt


def test_the_roster_call_site_reads_session_organizer(monkeypatch):
    """The seam in `server._auto_identify_and_save_speakers`. It has
    always passed `getattr(session, "organizer", "")`; this asserts the
    attribute it reads is the one record-start now sets, so the two
    can't drift apart into a permanently-empty kwarg again."""
    captured: dict = {}

    async def _fake_identify(transcript, attendees=None, organizer=""):
        captured["attendees"] = list(attendees or [])
        captured["organizer"] = organizer
        return {}  # nothing named → the function returns 0 immediately

    monkeypatch.setattr(
        server.svc, "summarizer",
        SimpleNamespace(identify_speakers=_fake_identify))
    monkeypatch.setattr(server.svc, "speaker_profile_svc", MagicMock())

    session = Session(session_id="s-roster")
    session.organizer = ORGANIZER_PLAIN
    session.attendees = []
    session.segments = [Segment(speaker_id="SPEAKER_00", start=0.0,
                                end=1.0, text="Thanks, Jane.")]
    session.get_or_create_speaker("SPEAKER_00")

    named = asyncio.run(server._auto_identify_and_save_speakers(session))

    assert named == 0
    assert captured["organizer"] == ORGANIZER_PLAIN
    assert captured["attendees"] == []


def test_a_legacy_session_object_without_the_attribute_still_identifies(monkeypatch):
    """`_auto_identify_and_save_speakers` is handed sessions rehydrated
    from JSON, and its `getattr` default is what keeps a session
    predating the field from raising AttributeError mid-processing."""
    captured: dict = {}

    async def _fake_identify(transcript, attendees=None, organizer=""):
        captured["organizer"] = organizer
        return {}

    monkeypatch.setattr(
        server.svc, "summarizer",
        SimpleNamespace(identify_speakers=_fake_identify))
    monkeypatch.setattr(server.svc, "speaker_profile_svc", MagicMock())

    session = Session(session_id="s-no-attr")
    session.segments = [Segment(speaker_id="SPEAKER_00", start=0.0,
                                end=1.0, text="Hello.")]
    session.get_or_create_speaker("SPEAKER_00")
    del session.organizer  # a session object from before the field

    assert asyncio.run(server._auto_identify_and_save_speakers(session)) == 0
    assert captured["organizer"] == ""


# ── 4. a missing organiser never costs the recording ─────────────────

@pytest.mark.parametrize("meeting", [
    {},                                   # key absent entirely
    {"organizer": None},                  # present but null
    {"organizer": 42},                    # wrong type
    {"organizer": "  Jane Doe  "},        # whitespace-padded
    None,                                 # not a mapping at all
    "not a dict",
])
def test_the_organizer_lookup_is_total(meeting):
    """`_organizer_for_meeting` must never raise, for the same reason
    `_resolve_client_for_meeting` must never raise: its caller's real
    job is starting a recording."""
    got = server._organizer_for_meeting(meeting)
    assert isinstance(got, str)
    # A non-string organiser is REFUSED, not str()'d: roster entries are
    # rendered as people, so "42" would print as a participant who does
    # not exist. Same refusal roster_names makes for a non-string
    # attendee entry.
    assert got in ("", "Jane Doe")


def test_a_hostile_meeting_object_yields_an_empty_organizer():
    """Belt and braces: even a `.get` that throws degrades to ""."""
    class _Hostile:
        def get(self, key, *a):
            raise RuntimeError("calendar backend exploded")

    assert server._organizer_for_meeting(_Hostile()) == ""


def test_organizer_resolution_failing_does_not_block_the_recording(
        auto_start, monkeypatch):
    """THE HARD CONSTRAINT. Calendar display and auto-record were broken
    for weeks and only fixed across v2.30.0–v2.34.0. A name is worth
    nothing next to a recording that cannot be re-run, so even a
    programming error in the organiser lookup — one that gets past the
    lookup's own swallowing — must leave the recording starting exactly
    as it does today, with organizer "".
    """
    def _explode(meeting):
        raise RuntimeError("organiser resolution is broken")

    monkeypatch.setattr(server, "_organizer_for_meeting", _explode)

    server._auto_record_start(_meeting(organizer=ORGANIZER_PLAIN))

    assert len(auto_start) == 1, "the recording must still have started"
    req = auto_start[0]
    assert req.organizer == ""
    # Everything else the auto path carries is untouched.
    assert req.meeting_name
    assert req.mic_device_index == 1
    assert req.scheduled_end_iso


def test_client_resolution_failing_still_leaves_the_organizer(auto_start,
                                                             monkeypatch):
    """The reverse direction — the two additive lookups are independent,
    so one failing must not take the other's field with it."""
    def _explode(subject, attendees):
        raise RuntimeError("client resolution is broken")

    monkeypatch.setattr(server, "_resolve_client_for_meeting", _explode)

    server._auto_record_start(_meeting(organizer=ORGANIZER_PLAIN))

    assert len(auto_start) == 1
    assert auto_start[0].organizer == ORGANIZER_PLAIN
    assert auto_start[0].client == ""


# ── 5. the sources really do deliver one ─────────────────────────────

def test_all_three_calendar_sources_emit_an_organizer_key():
    """Source-text check on the three backends, so a fourth source (or
    a refactor of one) can't quietly stop emitting the key and leave the
    roster silently empty again. The record path reads the merged dict
    `calendar_feed` produces, and `calendar_feed` merges whole dicts —
    it neither adds this key nor drops it."""
    backend = Path(__file__).resolve().parents[1]
    for rel in ("services/_calendar_outlook.py",
                "services/_calendar_eventkit.py",
                "services/extension_calendar_service.py"):
        source = (backend / rel).read_text(encoding="utf-8")
        assert '"organizer"' in source, f"{rel} must emit an organizer key"
