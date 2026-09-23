"""
The live transcript must not show one sentence as two people.

Field report (2026-09): the live view regularly showed the same words
twice at the same moment — once under "You" and once under "Speaker N"
— on headsets and on speakers alike; and separately, one far-end person
split across two Speaker labels. The first is the same speech reaching
both streams (far end through the speakers into the mic, or the user's
voice routed into system audio) and being transcribed by each. The
second is the live tracker (see test_live_speakers.py).

These drive the real LiveTranscriber's per-chunk path with a scripted
Whisper stand-in, so what is asserted is what a live view receives.
"""

from __future__ import annotations

import json
import queue
from pathlib import Path

import numpy as np
import pytest

from core.live_dedup import CrossStreamDeduper, words
from core.live_speakers import LiveSpeakerTracker
from core.live_transcriber import LiveTranscriber, serialize_segment_sse

SR = 16000
LOUD = 0.3      # a stream's own speech
QUIET = 0.03    # the same speech leaking into the other stream (-20 dB)

FRONTEND_FIXTURE = (Path(__file__).resolve().parents[2] / "src" / "lib"
                    / "__fixtures__" / "live-transcript-stream.json")


# ── The pure decision ───────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _warm(d, source, n=3, level=-10.0):
    for i in range(n):
        d.observe(source, [1000 + i], f"{source} own words number {i} here",
                  level)


def test_the_quieter_later_copy_is_dropped():
    d = CrossStreamDeduper(clock=_Clock())
    _warm(d, "them"), _warm(d, "you")
    assert not d.observe("them", [1], "we should ship the release on friday",
                         -10.0).drop
    leaked = d.observe("you", [2], "we should ship the release on friday",
                       -30.0)
    assert leaked.drop and leaked.retract == []


def test_a_louder_later_copy_retracts_the_one_on_screen():
    """The mic's leaked copy got transcribed first; system audio's real
    copy arrives second and must replace it, not sit beside it."""
    d = CrossStreamDeduper(clock=_Clock())
    _warm(d, "them"), _warm(d, "you")
    assert not d.observe("you", [7, 8], "we should ship the release on "
                         "friday", -30.0).drop
    real = d.observe("them", [9], "we should ship the release on friday",
                     -10.0)
    assert not real.drop and real.retract == [7, 8]


def test_the_users_voice_leaking_into_system_audio_is_dropped():
    """Headset sidetone: the user's own sentence arrives in system audio
    too, quieter there than their mic."""
    d = CrossStreamDeduper(clock=_Clock())
    _warm(d, "them"), _warm(d, "you")
    d.observe("you", [1], "i can send the pricing sheet tomorrow", -10.0)
    assert d.observe("them", [2], "i can send the pricing sheet tomorrow",
                     -28.0).drop


def test_a_slightly_different_hearing_still_counts():
    """The leaked copy is quieter and Whisper hears it a little
    differently — and the two streams cut sentences in different places."""
    d = CrossStreamDeduper(clock=_Clock())
    _warm(d, "them"), _warm(d, "you")
    d.observe("them", [1], "Okay, so we should ship the release on Friday, "
              "and then", -10.0)
    assert d.observe("you", [2], "so we should ship a release on Friday",
                     -30.0).drop


def test_short_backchannels_on_both_sides_are_both_kept():
    """Two people really do say "yeah, okay" at the same time."""
    d = CrossStreamDeduper(clock=_Clock())
    d.observe("them", [1], "yeah okay", -10.0)
    assert not d.observe("you", [2], "yeah okay", -10.0).drop


def test_different_words_are_never_touched():
    d = CrossStreamDeduper(clock=_Clock())
    _warm(d, "them"), _warm(d, "you")
    d.observe("them", [1], "what does the timeline look like for you", -10.0)
    r = d.observe("you", [2], "we can probably start the week after next",
                  -30.0)
    assert not r.drop and r.retract == []


def test_the_same_words_much_later_are_not_a_duplicate():
    clock = _Clock()
    d = CrossStreamDeduper(clock=clock)
    d.observe("them", [1], "can you share your screen please now", -10.0)
    clock.t = 60.0
    assert not d.observe("you", [2], "can you share your screen please now",
                         -10.0).drop


def test_repeating_yourself_on_one_stream_is_not_a_duplicate():
    d = CrossStreamDeduper(clock=_Clock())
    d.observe("you", [1], "sorry can you hear me now okay", -10.0)
    assert not d.observe("you", [2], "sorry can you hear me now okay",
                         -10.0).drop


def test_when_the_levels_cannot_tell_both_stay():
    """A leak loses 15-25 dB. Two copies at the same relative level are
    more likely two people echoing each other — never hide real speech
    on a coin flip."""
    d = CrossStreamDeduper(clock=_Clock())
    _warm(d, "them"), _warm(d, "you")
    d.observe("them", [1], "can you see my screen right now", -10.0)
    r = d.observe("you", [2], "yes i can see your screen right now", -11.0)
    assert not r.drop and r.retract == []


def test_words_normalises_punctuation_and_case():
    assert words("Okay, so — we'll SHIP it.") == ["okay", "so", "we'll",
                                                  "ship", "it"]


# ── Through the real transcriber ────────────────────────────────────

class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _ScriptedModel:
    """faster-whisper stand-in: returns the next scripted text for each
    call, as one segment spanning the clip."""

    def __init__(self):
        self.script = []

    def transcribe(self, audio, **opts):
        text = self.script.pop(0)
        return iter([_Seg(0.0, len(audio) / SR, text)]), None


class _Engine:
    def __init__(self):
        self._model = _ScriptedModel()


def _tone(amp, seconds=3.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _transcriber(tracker=None):
    engine = _Engine()
    lt = LiveTranscriber(engine_provider=lambda: engine, samplerate=SR,
                         speaker_tracker=tracker)
    q = lt.subscribe()
    return lt, engine._model, q


def _say(lt, model, stream, text, amp):
    model.script.append(text)
    source = lt._mic if stream == "you" else lt._loopback
    lt._transcribe_window(source, _tone(amp), 0.0)


def _warm_streams(lt, model):
    for i in range(3):
        _say(lt, model, "you", f"my own point number {i} about this", LOUD)
        _say(lt, model, "them", f"their own point number {i} about that",
             LOUD)


def _events(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_far_end_leaking_into_the_mic_shows_once_as_them():
    lt, model, q = _transcriber()
    _warm_streams(lt, model)
    _events(q)
    _say(lt, model, "them", "we should ship the release on friday", LOUD)
    _say(lt, model, "you", "we should ship the release on friday", QUIET)
    shown = [e for e in _events(q) if "text" in e]
    assert [(e["speaker"], e["text"]) for e in shown] == [
        ("them", "we should ship the release on friday")]
    assert sum("ship the release" in s["text"]
               for s in lt.all_segments()) == 1


def test_a_leaked_copy_already_shown_is_withdrawn():
    lt, model, q = _transcriber()
    _warm_streams(lt, model)
    _events(q)
    _say(lt, model, "you", "we should ship the release on friday", QUIET)
    leaked_id = _events(q)[0]["id"]
    _say(lt, model, "them", "we should ship the release on friday", LOUD)
    events = _events(q)
    assert events[0] == {"type": "retract", "ids": [leaked_id]}
    assert events[1]["speaker"] == "them"
    # Gone from history too, so a reconnecting view never gets it back.
    assert leaked_id not in {s["id"] for s in lt.all_segments()}


def test_every_segment_carries_a_distinct_id():
    lt, model, q = _transcriber()
    _warm_streams(lt, model)
    ids = [s["id"] for s in lt.all_segments()]
    assert len(ids) == 6 and len(set(ids)) == 6


def _merging_tracker():
    """A real LiveSpeakerTracker fed the convergence sequence from
    test_live_speakers: two labels that turn out to be one voice."""
    def unit(v):
        v = np.asarray(v, dtype=np.float32)
        return v / np.linalg.norm(v)

    seq = [unit([0.6, 0.8, 0.0]), unit([0.6, -0.8, 0.0])]
    seq += [unit([0.95, 0.31, 0.0]) if k % 2 == 0
            else unit([0.95, -0.31, 0.0]) for k in range(12)]
    it = iter(seq)
    return LiveSpeakerTracker(embed_fn=lambda pcm, sr: next(it))


def test_merged_speakers_are_relabelled_on_screen_and_in_history():
    lt, model, q = _transcriber(_merging_tracker())
    for k in range(14):
        _say(lt, model, "them", f"far end sentence number {k} goes here",
             LOUD)
    events = _events(q)
    relabels = [e for e in events if e.get("type") == "relabel"]
    assert {"type": "relabel", "from": "Speaker 2",
            "to": "Speaker 1"} in relabels
    assert {s.get("speaker_label") for s in lt.all_segments()} == {
        "Speaker 1"}


# ── The payload the frontend is tested against ──────────────────────

def _stream_payload():
    """Every message a live view receives across both corrections, as
    the real SSE serializer writes them."""
    lt, model, q = _transcriber(_merging_tracker())
    lines = []

    def drain():
        for e in _events(q):
            data = serialize_segment_sse(e)
            assert data.startswith("data: ") and data.endswith("\n\n")
            lines.append(json.loads(data[len("data: "):]))

    for i in range(3):
        _say(lt, model, "you", f"my own point number {i} about this", LOUD)
    drain()
    _say(lt, model, "you", "we should ship the release on friday", QUIET)
    drain()
    for k in range(14):
        text = ("we should ship the release on friday" if k == 3
                else f"far end sentence number {k} goes here")
        _say(lt, model, "them", text, LOUD)
        drain()
    return lines


def test_the_frontend_fixture_is_what_the_stream_sends():
    """src/lib/__fixtures__/live-transcript-stream.json was captured from
    _stream_payload(). Compared by message SHAPE (keys and types), so a
    renamed or dropped field fails here instead of the frontend tests
    passing against a stream nobody sends."""
    def shape(m):
        return {k: type(v).__name__ for k, v in sorted(m.items())}

    fixture = json.loads(FRONTEND_FIXTURE.read_text(encoding="utf-8"))
    live = _stream_payload()
    assert [shape(m) for m in fixture] == [shape(m) for m in live]
    kinds = {m.get("type", "segment") for m in live}
    assert kinds == {"segment", "retract", "relabel"}


if __name__ == "__main__":  # pragma: no cover - fixture regeneration
    FRONTEND_FIXTURE.write_text(
        json.dumps(_stream_payload(), indent=2) + "\n", encoding="utf-8")
