"""
Groups that are only interjections are not people.

Field data 2026-10-09, a 2-hour call with 12 people on it: 13 voice
groups plus the unattributed row: 14 rows in the speaker list. Eight groups
spoke for a minute or more. Of the small ones:

  * one, 38 s, 15 of 21 lines three words or fewer, matched an
    established speaker at 0.795 — the same person's "so" and "I love
    it" clustered apart because a one-word clip gives ECAPA little;
  * one, 23 s, 12 of 15 lines that short ("right", "thank you", "make
    sense"), resembled half the room at 0.4-0.6 — crosstalk, not a
    voice;
  * two spoke in sentences (median 15 and 7 words) and may be real
    people who spoke briefly — they must stay.

The shapes below keep those properties with invented content and names
(AGENTS.md). Embeddings are 4-dim stand-ins chosen for the cosines
that matter.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest

from _app_import import import_app

import_app()
import server  # noqa: E402

from core.speaker_merge import (  # noqa: E402
    UNATTRIBUTED_LABEL, UNATTRIBUTED_NAME, SpeakerFacts, is_low_evidence,
    plan_low_evidence_merges)
from models.segment import Segment  # noqa: E402
from models.session import Session  # noqa: E402


def _unit(*v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


ESTABLISHED = _unit(1.0, 0.0, 0.0, 0.0)
OTHER = _unit(0.0, 1.0, 0.0, 0.0)
SAME_AS_ESTABLISHED = _unit(0.8, 0.0, 0.6, 0.0)       # cosine 0.80
CROSSTALK = _unit(0.5, 0.5, 0.4, 0.58)                # ≤0.58 to each
BRIEF_SPEAKER = _unit(0.0, 0.0, 1.0, 0.0)


def _lines(n, words):
    return [" ".join(["word"] * words)] * n


def _session():
    s = Session("S-interject")
    s.audio_path = "(no such recording)"
    t = 0.0
    plan = [
        ("SPEAKER_12", _lines(40, 20), 6.0, ESTABLISHED),        # named below
        ("SPEAKER_03", _lines(30, 18), 5.0, ESTABLISHED),
        ("SPEAKER_05", _lines(20, 15), 5.0, OTHER),
        ("SPEAKER_02", _lines(15, 1) + _lines(6, 6), 1.8, SAME_AS_ESTABLISHED),
        ("SPEAKER_04", _lines(12, 2) + _lines(3, 5), 1.5, CROSSTALK),
        ("SPEAKER_00", _lines(7, 15), 5.0, BRIEF_SPEAKER),        # sentences
        (UNATTRIBUTED_LABEL, _lines(5, 1), 0.8, None),
    ]
    for label, lines, dur, emb in plan:
        sp = s.get_or_create_speaker(label)
        if emb is not None:
            sp.embedding = list(emb)
        for text in lines:
            s.segments.append(Segment(speaker_id=label, start=t,
                                      end=t + dur, text=text))
            t += dur + 0.5
    s.speakers["SPEAKER_12"].display_name = "Jane Doe"
    s.speakers["SPEAKER_12"].profile_id = "p-jane"
    s.speakers["SPEAKER_12"].embedding = list(_unit(0.0, 0.0, 0.0, 1.0))
    return s


@pytest.fixture
def quiet_profiles(monkeypatch):
    calls = []
    monkeypatch.setattr(server.svc, "speaker_profile_svc", SimpleNamespace(
        confirm_match=lambda *a: calls.append(a)))
    return calls


def _ids_in_transcript(session):
    return {seg.speaker_id for seg in session.segments}


def test_the_field_shape_resolves_to_the_people_who_spoke(quiet_profiles):
    session = _session()
    before = len(session.segments)

    server.auto_merge_split_speakers(session)

    # The interjections that sound like SPEAKER_03 are SPEAKER_03's;
    # the crosstalk is unattributed; everyone with sentences stays.
    assert "SPEAKER_02" not in _ids_in_transcript(session)
    assert "SPEAKER_04" not in _ids_in_transcript(session)
    assert {"SPEAKER_12", "SPEAKER_03", "SPEAKER_05", "SPEAKER_00"} <= set(
        session.speakers)
    assert sum(seg.speaker_id == "SPEAKER_03" for seg in session.segments) == 30 + 21
    assert sum(seg.speaker_id == UNATTRIBUTED_LABEL
               for seg in session.segments) == 5 + 15
    assert len(session.segments) == before          # nothing lost
    # No saved voice learns from a one-word clip.
    assert quiet_profiles == []


def test_unattributed_is_labelled_and_carries_no_voice(quiet_profiles):
    session = _session()
    server.auto_merge_split_speakers(session)
    unk = session.speakers[UNATTRIBUTED_LABEL]
    assert unk.display_name == UNATTRIBUTED_NAME
    assert not unk.embedding and unk.profile_id is None


def test_interjections_are_never_folded_into_the_owner(quiet_profiles):
    """The owner's spans come from the capture device; a voice guess
    must not hand anyone's words to the user."""
    session = _session()
    owner = server._owner_label()
    session.speakers[owner] = session.speakers.pop("SPEAKER_03")
    session.speakers[owner].speaker_id = owner
    for seg in session.segments:
        if seg.speaker_id == "SPEAKER_03":
            seg.speaker_id = owner

    server.auto_merge_split_speakers(session)

    assert sum(seg.speaker_id == owner for seg in session.segments) == 30
    assert "SPEAKER_02" not in _ids_in_transcript(session)


def _facts(sid, seconds, lines, short, emb=(), name="", profile=None):
    return SpeakerFacts(speaker_id=sid, display_name=name or sid,
                        profile_id=profile, embedding=tuple(emb),
                        seconds=seconds, segment_count=lines,
                        short_line_count=short)


def test_a_named_or_linked_group_is_never_absorbed():
    big = _facts("SPEAKER_01", 600, 100, 5, ESTABLISHED)
    named = _facts("SPEAKER_02", 20, 10, 9, SAME_AS_ESTABLISHED, name="Sam Roe")
    linked = _facts("SPEAKER_03", 20, 10, 9, SAME_AS_ESTABLISHED, profile="p")
    assert plan_low_evidence_merges([big, named, linked]) == []


def test_sentences_are_not_interjections():
    assert not is_low_evidence(_facts("S", 36, 7, 0))
    assert not is_low_evidence(_facts("S", 120, 60, 60))      # too much speech
    assert is_low_evidence(_facts("S", 23, 15, 12))


def test_a_close_match_to_another_interjection_group_is_not_a_target():
    a = _facts("SPEAKER_04", 20, 10, 9, ESTABLISHED)
    b = _facts("SPEAKER_08", 12, 4, 4, ESTABLISHED)
    groups = plan_low_evidence_merges([a, b])
    assert [(g.into, g.absorb) for g in groups] == [
        (UNATTRIBUTED_LABEL, ("SPEAKER_04", "SPEAKER_08"))]


def test_the_ai_is_never_asked_to_name_unattributed(monkeypatch):
    """Unattributed is several people; a name for it is a name for none
    of them, and saving it would teach a saved voice a mixture."""
    session = _session()
    session.speakers[UNATTRIBUTED_LABEL].embedding = list(ESTABLISHED)

    async def _identify(transcript, attendees=None, organizer=""):
        return {UNATTRIBUTED_LABEL: "Sam Roe"}

    created = []
    monkeypatch.setattr(server.svc, "summarizer",
                        SimpleNamespace(identify_speakers=_identify))
    monkeypatch.setattr(server.svc, "speaker_profile_svc", SimpleNamespace(
        get=lambda pid: None, list_all=lambda: [],
        create=lambda *a: created.append(a)))

    assert asyncio.run(server._auto_identify_and_save_speakers(session)) == 0
    assert created == []
    assert session.speakers[UNATTRIBUTED_LABEL].display_name != "Sam Roe"
