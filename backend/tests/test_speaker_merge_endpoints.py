"""
Merging split speakers on a session, end to end.

core/speaker_merge decides WHICH speakers merge and what the result is
called; test_speaker_merge.py covers that. This covers the part that
touches the session: rewriting every affected segment, dropping the
label that no longer exists, keeping the known-speakers roster in step,
and re-queueing the export so the Designated Folder copy stops
disagreeing with the app.

The transcript is what a merge rewrites, so these tests assert on
segments — not on a summary of what the code intended to do.
"""

from __future__ import annotations

import asyncio

import pytest

from _app_import import import_app

import_app()
import server  # noqa: E402

from models.segment import Segment  # noqa: E402
from models.session import Session  # noqa: E402


def _session(session_id="S1"):
    """One person split in two, which is the reported shape.

    ``SPEAKER_01`` matched a saved profile and got a name;
    ``SPEAKER_03`` is the same person and stayed a label.
    """
    s = Session(session_id)
    s.display_name = "Weekly sync"
    s.client = "Acme"
    s.audio_path = "(no such recording)"
    for label, start, end, text in [
        ("SPEAKER_01", 0.0, 10.0, "morning all"),
        ("SPEAKER_03", 10.0, 14.0, "and then after that"),
        ("SPEAKER_01", 14.0, 20.0, "so we will take that away"),
        ("SPEAKER_02", 20.0, 30.0, "sounds good"),
    ]:
        s.get_or_create_speaker(label)
        s.segments.append(Segment(speaker_id=label, start=start, end=end,
                                  text=text))
    s.speakers["SPEAKER_01"].display_name = "Jane Doe"
    s.speakers["SPEAKER_01"].profile_id = "p-jane"
    s.speakers["SPEAKER_01"].match_confirmed = True
    s.speakers["SPEAKER_01"].embedding = [1.0, 0.0]
    s.speakers["SPEAKER_03"].embedding = [0.98, 0.2]
    s.speakers["SPEAKER_02"].display_name = "Poe"
    s.speakers["SPEAKER_02"].embedding = [0.0, 1.0]
    return s


class _Sessions:
    def __init__(self, session):
        self._session = session
        self.saved = []

    def load_full(self, session_id):
        return self._session if session_id == self._session.session_id else None

    def save(self, session):
        self.saved.append(session)


class _Profiles:
    def __init__(self):
        self.confirmed = []

    def confirm_match(self, profile_id, embedding, session_id):
        self.confirmed.append((profile_id, list(embedding), session_id))


class _Worker:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, session_id, copy_audio=False):
        self.enqueued.append(session_id)


@pytest.fixture
def wired(monkeypatch):
    session = _session()
    sessions, profiles, worker = _Sessions(session), _Profiles(), _Worker()
    monkeypatch.setattr(server.svc, "load_settings", lambda: None,
                        raising=False)
    monkeypatch.setattr(server.svc, "session_svc", sessions, raising=False)
    monkeypatch.setattr(server.svc, "speaker_profile_svc", profiles,
                        raising=False)
    monkeypatch.setattr(server, "_EXPORT_WORKER", worker, raising=False)
    return session, sessions, profiles, worker


def _speaker_ids_in_transcript(session):
    return [seg.speaker_id for seg in session.segments]


# ── The merge itself ────────────────────────────────────────────────

def test_merging_rewrites_the_transcript(wired):
    """The reported defect is what the READER sees: a named person
    alternating with a stranger who is the same person. A merge that
    updated the speaker list without moving the segments would leave
    that transcript untouched."""
    session, _, _, _ = wired

    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_03"])))

    assert "SPEAKER_03" not in _speaker_ids_in_transcript(session)
    assert _speaker_ids_in_transcript(session) == [
        "SPEAKER_01", "SPEAKER_01", "SPEAKER_01", "SPEAKER_02"]


def test_the_absorbed_speaker_stops_existing(wired):
    """Leaving it in the speakers map would keep an empty row in the
    Speakers tab that the user cannot get rid of."""
    session, _, _, _ = wired
    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_03"])))
    assert set(session.speakers) == {"SPEAKER_01", "SPEAKER_02"}


def test_the_named_half_keeps_its_name(wired):
    """With no target given, core/speaker_merge picks the survivor a
    reader would want kept — here the one the user confirmed."""
    session, _, _, _ = wired
    result = asyncio.run(server.merge_session_speakers(
        "S1", server.SpeakerMergeRequest(
            speaker_ids=["SPEAKER_01", "SPEAKER_03"])))
    assert result["merge"]["into"] == "SPEAKER_01"
    assert session.speakers["SPEAKER_01"].display_name == "Jane Doe"


def test_merging_into_the_unnamed_half_still_keeps_the_name(wired):
    """The user may drag it either way. Coming back with a stranger is
    what would make the feature feel broken."""
    session, _, _, _ = wired
    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_03"], into="SPEAKER_03")))
    assert session.speakers["SPEAKER_03"].display_name == "Jane Doe"
    assert "SPEAKER_01" not in session.speakers


def test_the_profile_link_follows_the_name(wired):
    """The surviving speaker must stay connected to the known-speakers
    roster, or the next meeting stops auto-matching this person."""
    session, _, _, _ = wired
    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_03"], into="SPEAKER_03")))
    assert session.speakers["SPEAKER_03"].profile_id == "p-jane"


def test_an_explicit_name_is_honoured(wired):
    session, _, _, _ = wired
    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_03"], display_name="Jane Roe")))
    assert session.speakers["SPEAKER_01"].display_name == "Jane Roe"


def test_the_roster_learns_the_whole_voice(wired):
    """The point of merging is that the profile was learning half a
    voice. The blended centroid goes back to the roster, or the split
    recurs next meeting for the same reason."""
    session, _, profiles, _ = wired
    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_03"])))
    assert [p for p, _, _ in profiles.confirmed] == ["p-jane"]


def test_the_session_is_saved_and_re_exported(wired):
    """The Designated Folder copy is what the rest of the team reads. A
    merge that only fixed the app would leave the shared transcript
    saying something different."""
    session, sessions, _, worker = wired
    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_03"])))
    assert sessions.saved == [session]
    assert worker.enqueued == ["S1"]


def test_a_three_way_merge_collapses_all_of_them(wired):
    session, _, _, _ = wired
    asyncio.run(server.merge_session_speakers("S1", server.SpeakerMergeRequest(
        speaker_ids=["SPEAKER_01", "SPEAKER_02", "SPEAKER_03"],
        into="SPEAKER_01")))
    assert set(session.speakers) == {"SPEAKER_01"}
    assert set(_speaker_ids_in_transcript(session)) == {"SPEAKER_01"}


# ── Refusals ────────────────────────────────────────────────────────

def test_an_unknown_speaker_is_refused(wired):
    """A stale dialog naming a label that no longer exists must not
    silently merge whatever is left."""
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.merge_session_speakers(
            "S1", server.SpeakerMergeRequest(
                speaker_ids=["SPEAKER_01", "SPEAKER_77"])))
    assert e.value.status_code == 404


def test_one_speaker_is_not_a_merge(wired):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.merge_session_speakers(
            "S1", server.SpeakerMergeRequest(speaker_ids=["SPEAKER_01"])))
    assert e.value.status_code == 400


def test_merging_a_speaker_into_itself_is_refused(wired):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.merge_session_speakers(
            "S1", server.SpeakerMergeRequest(
                speaker_ids=["SPEAKER_01", "SPEAKER_01"], into="SPEAKER_01")))
    assert e.value.status_code == 400


def test_an_unknown_session_is_a_404(wired):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.merge_session_speakers(
            "nope", server.SpeakerMergeRequest(
                speaker_ids=["SPEAKER_01", "SPEAKER_03"])))
    assert e.value.status_code == 404


def test_nothing_is_saved_when_the_request_is_refused(wired):
    """A refused merge must leave the transcript and the export exactly
    as they were."""
    session, sessions, _, worker = wired
    before = _speaker_ids_in_transcript(session)
    with pytest.raises(server.HTTPException):
        asyncio.run(server.merge_session_speakers(
            "S1", server.SpeakerMergeRequest(
                speaker_ids=["SPEAKER_01", "SPEAKER_77"])))
    assert _speaker_ids_in_transcript(session) == before
    assert sessions.saved == [] and worker.enqueued == []


# ── Suggestions ─────────────────────────────────────────────────────

def test_similar_voices_are_offered(wired):
    session, _, _, _ = wired
    out = asyncio.run(server.suggest_speaker_merges("S1"))
    pairs = {frozenset([s["into"], *s["absorb"]]) for s in out["suggestions"]}
    assert frozenset({"SPEAKER_01", "SPEAKER_03"}) in pairs
    assert frozenset({"SPEAKER_01", "SPEAKER_02"}) not in pairs


def test_a_suggestion_carries_the_names_the_user_will_recognise(wired):
    """"Merge SPEAKER_01 and SPEAKER_03" is not a question anyone can
    answer. "Merge Jane Doe and SPEAKER_03" is."""
    session, _, _, _ = wired
    out = asyncio.run(server.suggest_speaker_merges("S1"))
    assert "Jane Doe" in out["suggestions"][0]["names"]


def test_speakers_with_no_fingerprint_are_named_not_silently_dropped(wired):
    """Under 1.5s of speech leaves no centroid, so no suggestion can be
    made about that speaker. An empty list would read as "checked, all
    fine" — a result nobody could compute must not render as a result
    that isn't there."""
    session, _, _, _ = wired
    session.get_or_create_speaker("SPEAKER_09")
    out = asyncio.run(server.suggest_speaker_merges("S1"))
    assert "SPEAKER_09" in out["unfingerprinted"]


# ── The automatic pass ──────────────────────────────────────────────

def test_two_labels_carrying_one_name_are_merged_without_asking(wired):
    """After identification both halves read "Jane Doe" and the
    transcript is about to render two rows with one name on them. That
    is not a judgement call."""
    session, _, _, _ = wired
    session.speakers["SPEAKER_03"].display_name = "Jane Doe"

    applied = server.auto_merge_split_speakers(session)

    assert len(applied) == 1
    assert set(session.speakers) == {"SPEAKER_01", "SPEAKER_02"}
    assert "SPEAKER_03" not in _speaker_ids_in_transcript(session)


def test_two_labels_on_one_profile_are_merged_without_asking(wired):
    session, _, _, _ = wired
    session.speakers["SPEAKER_03"].profile_id = "p-jane"
    server.auto_merge_split_speakers(session)
    assert "SPEAKER_03" not in session.speakers


def test_a_similar_voice_alone_is_left_alone(wired):
    """SPEAKER_01 and SPEAKER_03 sound alike — that is why they are
    suggested. Acting on it unasked is how two people's words get
    merged, silently, into the summary and the commitments."""
    session, _, _, _ = wired
    assert server.auto_merge_split_speakers(session) == []
    assert "SPEAKER_03" in session.speakers


def test_the_automatic_pass_never_fails_processing(wired, monkeypatch):
    """Diarization and extraction already succeeded. A merge that cannot
    be worked out leaves the session as diarization produced it — the
    behaviour every session had before this existed."""
    session, _, _, _ = wired

    def _boom(_):
        raise RuntimeError("no")
    monkeypatch.setattr(server, "_speaker_facts", _boom, raising=False)

    assert server.auto_merge_split_speakers(session) == []
    assert set(session.speakers) == {"SPEAKER_01", "SPEAKER_02", "SPEAKER_03"}
