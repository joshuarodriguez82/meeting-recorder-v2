"""
Learning voices from an imported transcript — and refusing to when the
transcript's timing doesn't match the recording.

The policy (core/voice_learning.check_alignment) is tested on embedding
geometry: each speaker is a direction in a 192-dim space (ECAPA's), and
each line is that direction plus noise scaled so a line sits about 0.7
cosine from its speaker's voice and about 0.5 from the speaker's other
lines (ECAPA's range for short utterances of one person). An offset
transcript is modelled the way it happens — each line's label belongs
to the person speaking a few lines earlier.
"""

from __future__ import annotations

import numpy as np
import pytest

from core import voice_learning as vl

DIM = 192


def _voice(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(DIM)
    return v / np.linalg.norm(v)


NOISE = 1.0     # noise norm vs a unit voice: cos(line, voice) ~ 0.7


def _noise(rng) -> np.ndarray:
    return NOISE * rng.standard_normal(DIM) / np.sqrt(DIM)


def _lines(voice: np.ndarray, n: int, seed: int):
    rng = np.random.default_rng(seed)
    return [voice + _noise(rng) for _ in range(n)]


def _meeting(n_speakers=3, lines_each=12):
    voices = [_voice(100 + i) for i in range(n_speakers)]
    order = []                       # who spoke each line, in time order
    for k in range(lines_each):
        for i in range(n_speakers):
            order.append(i)
    rng = np.random.default_rng(7)
    rng.shuffle(order)
    spoken = [voices[i] + _noise(rng) for i in order]
    return voices, order, spoken


def _labelled(order, spoken, names, shift=0):
    """The transcript's view: line k carries the label of line k-shift."""
    out = {n: [] for n in names}
    for k, emb in enumerate(spoken):
        label = names[order[(k - shift) % len(order)]]
        out[label].append(emb)
    return out


NAMES = ["Doe, Jane [NA]", "Roe, Richard", "Poe, Rob"]


def test_an_aligned_transcript_is_learned_for_every_speaker():
    _, order, spoken = _meeting()
    check = vl.check_alignment(_labelled(order, spoken, NAMES))
    assert check.ok
    assert check.agreement > 0.9
    assert set(check.learnable) == set(NAMES)
    for c in check.learnable.values():
        assert abs(np.linalg.norm(c) - 1.0) < 1e-5


def test_an_offset_transcript_is_refused():
    _, order, spoken = _meeting()
    check = vl.check_alignment(_labelled(order, spoken, NAMES, shift=3))
    assert not check.ok
    assert check.learnable == {}
    assert "doesn't line up" in check.reason


def test_one_named_speaker_is_not_enough():
    _, order, spoken = _meeting(n_speakers=1)
    check = vl.check_alignment(_labelled(order, spoken, NAMES[:1]))
    assert not check.ok and check.learnable == {}


def test_a_speaker_with_too_few_lines_is_left_out():
    _, order, spoken = _meeting()
    labelled = _labelled(order, spoken, NAMES)
    labelled["Noh, Kim"] = labelled["Poe, Rob"][:2]
    check = vl.check_alignment(labelled)
    assert check.ok
    assert "Noh, Kim" not in check.learnable


def test_one_mislabelled_speaker_is_not_saved_while_the_rest_are():
    """Two people sharing one account in the roster: the label's lines
    are two voices, so that label agrees with nobody and isn't saved."""
    voices, order, spoken = _meeting(n_speakers=4)
    names = NAMES + ["Shared room"]
    labelled = _labelled(order, spoken, names)
    # Half of "Shared room"'s lines are really Jane's.
    jane = labelled["Doe, Jane [NA]"]
    labelled["Shared room"] = labelled["Shared room"][:6] + jane[:6]
    labelled["Doe, Jane [NA]"] = jane[6:]
    check = vl.check_alignment(labelled)
    assert check.ok
    assert "Shared room" not in check.learnable
    assert {"Roe, Richard", "Poe, Rob"} <= set(check.learnable)


def test_placeholder_names_are_never_learned():
    assert vl.named_speakers({
        "SPEAKER_00": "Doe, Jane [NA]",
        "SPEAKER_01": "Unknown speaker",
        "SPEAKER_02": "Speaker 1",
        "SPEAKER_03": "SPEAKER_03",
        "SPEAKER_04": "",
    }) == ["SPEAKER_00"]


# ── the server step, with the encoder stubbed and a real profile store ─


VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Jane Doe>One.</v>

00:00:05.000 --> 00:00:08.000
<v Rob Poe>Two.</v>

00:00:09.000 --> 00:00:12.000
<v Jane Doe>Three.</v>

00:00:13.000 --> 00:00:16.000
<v Rob Poe>Four.</v>

00:00:17.000 --> 00:00:20.000
<v Jane Doe>Five.</v>

00:00:21.000 --> 00:00:24.000
<v Rob Poe>Six.</v>
"""


def _setup(tmp_path, monkeypatch, per_turn):
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    sf = pytest.importorskip("soundfile")
    sys.modules.setdefault("dotenv", MagicMock())
    from _app_import import import_app
    import_app()
    import server
    import core.speaker_embeddings as se
    from services.session_service import SessionService
    from services.speaker_profile_service import SpeakerProfileService

    wav = tmp_path / "call.wav"
    sf.write(str(wav), np.zeros(16000 * 25, dtype=np.float32), 16000)
    vtt = tmp_path / "call.vtt"
    vtt.write_text(VTT, encoding="utf-8")
    svc = SessionService(str(tmp_path / "recordings"), index_enabled=False)
    session = svc.import_from_file(str(wav), transcript_path=str(vtt))
    profiles = SpeakerProfileService(tmp_path / "profiles")

    monkeypatch.setattr(server.svc, "settings", SimpleNamespace())
    monkeypatch.setattr(server.svc, "_services_ready", True)
    monkeypatch.setattr(server.svc, "session_svc", svc)
    monkeypatch.setattr(server.svc, "speaker_profile_svc", profiles)
    monkeypatch.setattr(se, "is_available", lambda: True)
    monkeypatch.setattr(se, "extract_turn_embeddings",
                        lambda path, turns, **k: per_turn(session, turns))
    # Check 1 (timing) passes unless a test says otherwise; it has its
    # own tests below.
    monkeypatch.setattr(server, "_transcript_timing_matches",
                        lambda s: (True, 0.9, ""))
    return server, svc, profiles, session


def _by_name(session, turns, voices, swap=False):
    out = {}
    for sid, spans in turns.items():
        name = session.speakers[sid].display_name
        if swap:
            name = "Rob Poe" if name == "Jane Doe" else "Jane Doe"
        out[sid] = _lines(voices[name], len(spans), seed=len(out))
    return out


def test_an_import_saves_each_named_voice(tmp_path, monkeypatch):
    voices = {"Jane Doe": _voice(1), "Rob Poe": _voice(2)}
    server, svc, profiles, session = _setup(
        tmp_path, monkeypatch,
        lambda s, t: _by_name(s, t, voices))
    outcome = server._learn_voices_from_transcript(session.session_id)

    assert outcome["state"] == "learned"
    assert sorted(x["name"] for x in outcome["learned"]) == ["Jane Doe", "Rob Poe"]
    assert sorted(p.display_name for p in profiles.list_all()) == [
        "Jane Doe", "Rob Poe"]
    saved = svc.load_full(session.session_id)
    assert saved.voice_learning["state"] == "learned"
    for sp in saved.speakers.values():
        assert sp.profile_id and sp.embedding and sp.match_confirmed
    # A saved voice is recognised: Jane's real voice matches Jane.
    match = profiles.find_match(voices["Jane Doe"].astype(np.float32))
    assert match and match[0].display_name == "Jane Doe"


def test_an_existing_voice_is_refined_not_duplicated(tmp_path, monkeypatch):
    """The roster spells her "Doe, Jane"; the user saved "Jane". Same
    voice → the saved one is refined, keeping the user's name."""
    voices = {"Jane Doe": _voice(1), "Rob Poe": _voice(2)}
    server, svc, profiles, session = _setup(
        tmp_path, monkeypatch, lambda s, t: _by_name(s, t, voices))
    profiles.create("Jane", voices["Jane Doe"].astype(np.float32), "OLD00001")
    outcome = server._learn_voices_from_transcript(session.session_id)
    actions = {x["name"]: x["action"] for x in outcome["learned"]}
    assert actions["Jane Doe"] == "refined"
    assert sorted(p.display_name for p in profiles.list_all()) == [
        "Jane", "Rob Poe"]


def test_a_transcript_that_does_not_match_the_audio_saves_nothing(
        tmp_path, monkeypatch):
    """Every line labelled Jane is really a random one of the two — the
    offset-transcript case."""
    voices = {"Jane Doe": _voice(1), "Rob Poe": _voice(2)}
    rng = np.random.default_rng(3)

    def _mixed(s, turns):
        both = list(voices.values())
        return {sid: [both[int(rng.integers(0, 2))] + 0.05 * rng.standard_normal(DIM)
                      for _ in spans] for sid, spans in turns.items()}
    server, svc, profiles, session = _setup(tmp_path, monkeypatch, _mixed)
    outcome = server._learn_voices_from_transcript(session.session_id)
    assert outcome["state"] == "skipped"
    assert "doesn't line up" in outcome["reason"]
    assert profiles.list_all() == []
    assert svc.load_full(session.session_id).voice_learning["state"] == "skipped"



def test_a_swapped_transcript_is_caught_by_the_timing_check(
        tmp_path, monkeypatch):
    """One line out in a two-person back-and-forth: every 'Jane' line is
    Rob's voice, consistently — the voice check passes it (observed with
    the real model). Only the timing check can refuse it."""
    voices = {"Jane Doe": _voice(1), "Rob Poe": _voice(2)}
    server, svc, profiles, session = _setup(
        tmp_path, monkeypatch, lambda s, t: _by_name(s, t, voices, swap=True))
    monkeypatch.setattr(
        server, "_transcript_timing_matches",
        lambda s: (False, 0.14, "the transcript's timing doesn't line up "
                                "with the recording"))
    outcome = server._learn_voices_from_transcript(session.session_id)
    assert outcome["state"] == "skipped"
    assert outcome["word_recall"] == 0.14
    assert profiles.list_all() == []


# ── check 1: timing, from the words ──────────────────────────────────
#
# Measured with the real Whisper model on a two-voice meeting
# (2026-10-03): aligned median recall 0.93; one line out 0.14; 2 s late
# 0.44; 1 s late 0.85; 1 s early 1.00. The bar (0.6) passes ±1 s, which
# leaves each line mostly its own speaker, and refuses 2 s or a line.

def test_word_recall_ignores_case_punctuation_and_short_words():
    assert vl.word_recall("Who owns the holiday routing rules?",
                          "who owns the holiday routing rules") == 1.0
    assert vl.word_recall("Who owns the holiday routing rules?",
                          "the pilot queue goes live on Monday") < 0.2
    assert vl.word_recall("", "anything") == 0.0


def test_timing_needs_enough_lines_and_enough_words_heard():
    assert vl.timing_matches([0.93, 0.8, 1.0])[0] is True
    assert vl.timing_matches([0.9, 0.9])[0] is False          # too few
    ok, m, why = vl.timing_matches([0.38, 0.5, 0.2, 0.57, 0.14])
    assert not ok and m == 0.38 and "doesn't line up" in why


def test_spot_checks_are_spread_and_skip_short_lines():
    lines = [(i * 5.0, i * 5.0 + (3.0 if i % 2 else 1.0),
              "one two three four five six" if i % 2 else "yes ok")
             for i in range(40)]
    spots = vl.pick_spot_checks(lines)
    assert len(spots) == vl.SPOT_CHECKS
    assert all(b - a >= vl.SPOT_MIN_SECONDS for a, b, _ in spots)
    assert spots[0][0] < 20 and spots[-1][0] > 150      # spread out


def test_the_spot_check_reads_each_line_at_its_own_timestamp(
        tmp_path, monkeypatch):
    """The real slicing code, against a recording where each line's span
    holds a distinct level; the stand-in transcriber reports which line
    it was handed. Aligned timestamps hear the right words; timestamps
    one line out hear the neighbour's."""
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    sf = pytest.importorskip("soundfile")
    sys.modules.setdefault("dotenv", MagicMock())
    from _app_import import import_app
    import_app()
    import server
    from models.segment import Segment
    from models.session import Session

    texts = ["the pilot queue goes live monday",
             "holiday routing rules still missing",
             "operations team sends calendar friday",
             "recording uploaded after hours message",
             "testing session booked tuesday morning",
             "porting delay escalated this afternoon"]
    rate, span = 16000, 3.0
    audio = np.zeros(int(rate * span * len(texts)), dtype=np.float32)
    for i in range(len(texts)):
        audio[int(i * span * rate):int((i + 1) * span * rate)] = (i + 1) / 10
    wav = tmp_path / "rec.wav"
    sf.write(str(wav), audio, rate)

    class _Engine:
        def transcribe_clip(self, clip, language="en"):
            level = float(np.median(clip))       # padding is a minority
            return texts[int(round(level * 10)) - 1]

    monkeypatch.setattr(server.svc, "settings", SimpleNamespace())
    monkeypatch.setattr(server.svc, "transcription", _Engine())
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)

    def _session(shift):
        s = Session(session_id="T1")
        s.audio_path = str(wav)
        s.segments = [Segment("SPEAKER_00", i * span + 0.2, (i + 1) * span - 0.2,
                              texts[(i - shift) % len(texts)])
                      for i in range(len(texts))]
        return s

    ok, recall, _ = server._transcript_timing_matches(_session(0))
    assert ok and recall == 1.0
    ok, recall, why = server._transcript_timing_matches(_session(1))
    assert not ok and recall < 0.6 and "doesn't line up" in why
