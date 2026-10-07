"""
An AI-guessed speaker name labels the meeting; it doesn't rewrite the
saved voices.

After processing, the summarizer names speakers from what was said
("I went to Sam Poe" names someone, not necessarily the next voice) and
the names were saved as voiceprints. Two paths let one wrong guess
spread to every later meeting:

  * a speaker voice-matched (unconfirmed) to one saved person and named
    another by the transcript RENAMED that saved person and folded this
    voice into them;
  * a name that matched a saved person's name folded this voice into
    them however different it sounded.

Field data 2026-10-06/07: saved people of the wrong gender suggested as
matches, and the user's own mic voice-matched to another person.

Runs against the real SpeakerProfileService on a temp dir. Every name
is fictional (AGENTS.md).
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from _app_import import import_app  # noqa: E402
from models.segment import Segment  # noqa: E402
from models.session import Session  # noqa: E402
from services.speaker_profile_service import SpeakerProfileService  # noqa: E402

import_app()
import server  # noqa: E402

DIM = 192


def _voice(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _near(v: np.ndarray, seed: int, noise: float = 0.3) -> np.ndarray:
    w = v + noise * _voice(seed)
    return (w / np.linalg.norm(w)).astype(np.float32)


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    store = SpeakerProfileService(tmp_path)
    monkeypatch.setattr(server.svc, "speaker_profile_svc", store)
    return store


def _run(monkeypatch, session, mapping):
    async def _identify(transcript, attendees=None, organizer=""):
        return mapping

    monkeypatch.setattr(server.svc, "summarizer",
                        SimpleNamespace(identify_speakers=_identify))
    return asyncio.run(server._auto_identify_and_save_speakers(session))


def _session(voice: np.ndarray, profile_id=None) -> Session:
    s = Session(session_id="s-auto")
    s.segments = [Segment(speaker_id="SPEAKER_05", start=0.0, end=4.0,
                          text="I went to Sam Poe about this.")]
    sp = s.get_or_create_speaker("SPEAKER_05")
    sp.embedding = [float(x) for x in voice]
    if profile_id:
        sp.profile_id = profile_id
        sp.match_confirmed = False
        sp.match_confidence = 0.85
    return s


def test_a_guessed_name_never_renames_the_voice_matched_person(
        profiles, monkeypatch):
    jane = profiles.create("Jane Doe", _voice(1), "s-old")
    before = list(jane.embedding)
    session = _session(_near(_voice(1), 9), profile_id=jane.profile_id)

    assert _run(monkeypatch, session, {"SPEAKER_05": "Sam Roe"}) == 1

    kept = profiles.get(jane.profile_id)
    assert kept.display_name == "Jane Doe"
    assert kept.embedding == before
    assert "s-auto" not in kept.sessions_seen_in
    sp = session.speakers["SPEAKER_05"]
    assert sp.display_name == "Sam Roe"
    assert sp.profile_id != jane.profile_id


def test_a_name_never_absorbs_a_voice_that_sounds_like_someone_else(
        profiles, monkeypatch):
    jane = profiles.create("Jane Doe", _voice(1), "s-old")
    before = list(jane.embedding)
    session = _session(_voice(2))   # an unrelated voice

    assert _run(monkeypatch, session, {"SPEAKER_05": "Jane Doe"}) == 1

    assert profiles.get(jane.profile_id).embedding == before
    assert [p.display_name for p in profiles.list_all()] == ["Jane Doe"]
    sp = session.speakers["SPEAKER_05"]
    assert sp.display_name == "Jane Doe" and sp.profile_id is None


def test_the_same_name_and_a_similar_voice_still_refine_the_profile(
        profiles, monkeypatch):
    jane = profiles.create("Jane Doe", _voice(1), "s-old")
    session = _session(_near(_voice(1), 9))
    assert float(np.dot(session.speakers["SPEAKER_05"].embedding,
                        _voice(1))) >= server.AUTO_NAME_SAME_VOICE_FLOOR

    _run(monkeypatch, session, {"SPEAKER_05": "jane doe"})

    assert session.speakers["SPEAKER_05"].profile_id == jane.profile_id
    assert "s-auto" in profiles.get(jane.profile_id).sessions_seen_in


def test_a_matched_name_that_agrees_refines_the_match(profiles, monkeypatch):
    jane = profiles.create("Jane Doe", _voice(1), "s-old")
    session = _session(_near(_voice(1), 9), profile_id=jane.profile_id)

    _run(monkeypatch, session, {"SPEAKER_05": "Jane Doe"})

    assert "s-auto" in profiles.get(jane.profile_id).sessions_seen_in
    assert session.speakers["SPEAKER_05"].profile_id == jane.profile_id
