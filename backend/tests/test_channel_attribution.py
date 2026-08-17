"""
Channel-dominance speaker attribution (core/channel_attribution.py).

The claim under test, in one line: WHICH DEVICE captured a sound is
ground truth about who said it, and using it must never be able to make
the transcript worse than voice-only diarization was.

That splits into two properties, and every test here is one of them:

  1. When the channel signal is clean, the user's speech is attributed
     to the user with certainty and the far end's speech is NEVER
     attributed to the user — not by dominance, not by a turn that
     straddles a handover, not by a label collision.
  2. When the channel signal is NOT trustworthy — a mic-only session,
     conference-room mode, a speakerphone recording where the far end
     bleeds into the mic, a marginal/low-confidence timeline, or a
     session recorded before the sidecar existed — the pipeline falls
     back to EXACTLY today's pure-voice behaviour, silently and without
     error.

Fixtures are synthetic amplitude-modulated noise rather than real
speech: this module measures energy ratios between two channels, and
AM noise exercises the envelope logic identically to speech while
staying deterministic and dependency-free (numpy only — see
core/vad.py's note on why the CI venv has no ML VAD).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.channel_attribution import (
    FAR_END_FALLBACK_LABEL,
    LABEL_BOTH,
    LABEL_LOOPBACK,
    LABEL_MIC,
    OWNER_SPEAKER_LABEL,
    SIDECAR_SUFFIX,
    compute_attribution,
    compute_attribution_from_files,
    confident_intervals,
    constrain_turns_to_owner,
    evaluate_trust,
    load_sidecar_for_audio,
    sidecar_path_for_audio,
    stood_down_document,
    write_sidecar,
)
from core.live_transcriber import SPEAKER_YOU
from utils.audio_utils import finalize_recording_streaming

SR = 16000


# ── Fixtures ─────────────────────────────────────────────────────────


def _speechish(n: int, amplitude: float, rng: np.random.Generator,
               rate_hz: float = 3.0) -> np.ndarray:
    """Amplitude-modulated noise — a syllable-rate energy envelope over
    a broadband carrier. Stands in for speech for anything that only
    looks at short-term energy."""
    t = np.arange(n) / SR
    env = 0.5 * (1.0 + np.sin(2 * np.pi * rate_hz * t))
    return (rng.standard_normal(n) * amplitude * env).astype(np.float32)


def two_stream_fixture(
    duration_s: float = 20.0,
    mic_turns=((1.0, 5.0), (11.0, 14.0)),
    far_turns=((6.0, 10.0), (15.0, 19.0)),
    bleed_gain: float = 0.0,
    mic_amp: float = 0.2,
    far_amp: float = 0.15,
    seed: int = 7,
):
    """Build (mic, loopback) for a two-party call.

    `bleed_gain` re-injects the far end into the mic — 0.0 is a headset
    (the far end physically cannot reach the mic), larger values are a
    laptop-speakers setup. Each stream carries its own independent
    noise floor at a DIFFERENT level, so any rule that only works when
    the two devices happen to be gain-matched will fail these tests.
    """
    rng = np.random.default_rng(seed)
    n = int(SR * duration_s)
    mic = (rng.standard_normal(n) * 0.001).astype(np.float32)
    lb = (rng.standard_normal(n) * 0.0005).astype(np.float32)
    for a, b in mic_turns:
        seg = _speechish(int((b - a) * SR), mic_amp, rng)
        mic[int(a * SR):int(a * SR) + len(seg)] += seg
    for a, b in far_turns:
        seg = _speechish(int((b - a) * SR), far_amp, rng, rate_hz=2.3)
        lb[int(a * SR):int(a * SR) + len(seg)] += seg
        if bleed_gain:
            mic[int(a * SR):int(a * SR) + len(seg)] += seg * bleed_gain
    return mic, lb


def _label_at(doc: dict, t: float):
    for span in doc["spans"]:
        if span["start"] <= t < span["end"]:
            return span["label"]
    return None


# ══ 1. Clean two-stream recording ════════════════════════════════════


def test_speech_only_in_mic_is_attributed_to_the_user():
    """The headline claim. Audio present only on the mic is the user,
    with high confidence, because the mic is definitionally the user."""
    mic, lb = two_stream_fixture()
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)

    assert doc["summary"]["usable"] is True, doc["summary"]
    assert _label_at(doc, 3.0) == LABEL_MIC
    assert _label_at(doc, 12.5) == LABEL_MIC
    assert doc["summary"]["mean_mic_confidence"] > 0.9

    turns = [{"start": 0.5, "end": 5.5, "speaker": "SPEAKER_00"}]
    out, stats = constrain_turns_to_owner(turns, doc)
    assert stats["applied"] is True
    owner = [t for t in out if t["speaker"] == OWNER_SPEAKER_LABEL]
    assert owner, out
    assert owner[0]["start"] == pytest.approx(1.0, abs=0.1)
    assert owner[0]["end"] == pytest.approx(5.0, abs=0.1)


def test_speech_only_in_loopback_is_never_attributed_to_the_user():
    """The claim that actually protects the user: far-end speech is
    system audio, so no amount of voice similarity may put it in the
    user's turns. Every far-end second must survive with a non-owner
    label — including when pyannote lumped the far end into the SAME
    cluster it used for the user."""
    mic, lb = two_stream_fixture()
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)

    assert _label_at(doc, 8.0) == LABEL_LOOPBACK
    assert _label_at(doc, 17.0) == LABEL_LOOPBACK

    # One cluster for everybody — the exact mis-attribution this
    # feature exists to undo.
    turns = [
        {"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 6.0, "end": 10.0, "speaker": "SPEAKER_00"},
        {"start": 11.0, "end": 14.0, "speaker": "SPEAKER_00"},
        {"start": 15.0, "end": 19.0, "speaker": "SPEAKER_00"},
    ]
    out, stats = constrain_turns_to_owner(turns, doc)
    assert stats["applied"] is True

    owner_time = sum(
        t["end"] - t["start"] for t in out
        if t["speaker"] == OWNER_SPEAKER_LABEL)
    # The user's two turns (4s + 3s), not the far end's.
    assert owner_time == pytest.approx(7.0, abs=0.6)
    for turn in out:
        if turn["speaker"] != OWNER_SPEAKER_LABEL:
            continue
        mid = (turn["start"] + turn["end"]) / 2
        assert not (6.0 < mid < 10.0), f"far-end time given to user: {turn}"
        assert not (15.0 < mid < 19.0), f"far-end time given to user: {turn}"


def test_turn_straddling_a_handover_is_split_not_voted():
    """A single pyannote turn routinely spans a handover. Voting the
    whole turn one way would either hand the far end's tail to the user
    or throw the user's head away; cutting it at the channel boundary
    keeps both halves right."""
    mic, lb = two_stream_fixture(
        mic_turns=((1.0, 5.0),), far_turns=((5.5, 10.0),))
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)

    turns = [{"start": 1.0, "end": 10.0, "speaker": "SPEAKER_00"}]
    out, stats = constrain_turns_to_owner(turns, doc)

    assert stats["split_turns"] == 1
    assert len(out) >= 2
    assert out[0]["speaker"] == OWNER_SPEAKER_LABEL
    assert out[0]["end"] == pytest.approx(5.0, abs=0.15)
    assert out[-1]["speaker"] == "SPEAKER_00"
    assert out[-1]["end"] == pytest.approx(10.0, abs=0.01)
    # No time is invented or lost by the split.
    assert sum(t["end"] - t["start"] for t in out) == pytest.approx(9.0, abs=0.01)


def test_far_end_can_never_inherit_the_owner_label():
    """Structural guard, not a probabilistic one. pyannote emits
    SPEAKER_xx and cannot collide with the owner label — but the
    invariant "far-end words are never attributed to the user" must not
    depend on that coincidence, so a turn that arrives already claiming
    the owner identity is stripped of it outside confident mic spans."""
    mic, lb = two_stream_fixture()
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)

    turns = [{"start": 6.0, "end": 10.0, "speaker": OWNER_SPEAKER_LABEL}]
    out, _stats = constrain_turns_to_owner(turns, doc)
    assert all(t["speaker"] == FAR_END_FALLBACK_LABEL for t in out), out


def test_overlapping_speech_is_left_to_pyannote():
    """Documented rule: `both` spans are where the channel evidence is
    weakest, so they are never used to override. Simultaneous speech
    keeps whatever pyannote decided, and the low confidence is
    reported rather than hidden."""
    rng = np.random.default_rng(3)
    n = int(SR * 20)
    mic = (rng.standard_normal(n) * 0.001).astype(np.float32)
    lb = (rng.standard_normal(n) * 0.001).astype(np.float32)
    # A clean user turn, then a stretch where both talk at once at
    # matched levels — neither channel is convincingly louder, which is
    # what "inside the dominance margin" means. (Note that lopsided
    # double-talk, where one party IS clearly louder frame by frame,
    # deliberately resolves to that party rather than to `both`.)
    mic[int(1 * SR):int(6 * SR)] += _speechish(int(5 * SR), 0.2, rng)
    mic[int(8 * SR):int(12 * SR)] += _speechish(int(4 * SR), 0.2, rng)
    lb[int(8 * SR):int(12 * SR)] += _speechish(int(4 * SR), 0.2, rng)
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)

    assert _label_at(doc, 10.0) == LABEL_BOTH
    assert doc["summary"]["both_seconds"] > 1.0
    # `both` spans never become override intervals.
    for start, end in confident_intervals(doc, LABEL_MIC):
        assert not (start >= 8.5 and end <= 11.5), (start, end)

    turns = [{"start": 8.0, "end": 12.0, "speaker": "SPEAKER_03"}]
    out, _stats = constrain_turns_to_owner(turns, doc)
    assert [t["speaker"] for t in out] == ["SPEAKER_03"]


def test_gain_difference_between_devices_does_not_decide_the_answer():
    """Mic gain varies by 30+ dB across devices. The rule is a ratio
    against each channel's OWN noise floor, so scaling one channel
    wholesale must not change a single label."""
    mic, lb = two_stream_fixture()
    quiet = compute_attribution(mic * 0.05, lb, SR, loopback_offset_s=0.0)
    loud = compute_attribution(mic * 8.0, lb, SR, loopback_offset_s=0.0)
    labels_quiet = [(s["label"], round(s["start"], 1)) for s in quiet["spans"]]
    labels_loud = [(s["label"], round(s["start"], 1)) for s in loud["spans"]]
    assert labels_quiet == labels_loud


def test_offset_is_applied_before_comparing():
    """Loopback is positioned at the wallclock offset finalize already
    computed. Comparing unaligned streams would misclassify exactly the
    boundaries that matter, so a shifted loopback with a matching
    offset must produce the same answer as an unshifted one."""
    mic, lb = two_stream_fixture()
    aligned = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)
    # Loopback started 2s after the mic did: drop its first 2s and
    # declare the offset. Same physical timeline, different arrays.
    shifted = compute_attribution(
        mic, lb[int(2 * SR):], SR, loopback_offset_s=2.0)
    assert _label_at(shifted, 8.0) == LABEL_LOOPBACK
    assert _label_at(shifted, 3.0) == LABEL_MIC
    assert (shifted["summary"]["loopback_seconds"]
            == pytest.approx(aligned["summary"]["loopback_seconds"], abs=0.3))


def test_spans_are_merged_not_raw_frames():
    """Hysteresis, observable: a 20s two-party call is a handful of
    spans, not 625 frames. Without smoothing + minimum dwell, the
    per-frame decision flaps on every plosive and pause."""
    mic, lb = two_stream_fixture()
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)
    assert doc["summary"]["span_count"] < 15
    shortest = min(s["end"] - s["start"] for s in doc["spans"])
    assert shortest >= 0.15, doc["spans"]


# ══ 2. Standing down ═════════════════════════════════════════════════


def test_mic_only_session_stands_down():
    """A real field case (`lb=n/a`): no system audio was captured, so
    there is no second channel to compare against and no opinion to
    have. Must behave exactly as today."""
    mic, _lb = two_stream_fixture()
    doc = compute_attribution(mic, None, SR, loopback_offset_s=0.0)
    usable, reason = evaluate_trust(doc)
    assert usable is False
    assert reason == "mic_only_recording"

    turns = [{"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    out, stats = constrain_turns_to_owner(turns, doc)
    assert out == turns
    assert stats["applied"] is False
    assert stats["reason"] == "mic_only_recording"


def test_conference_room_mode_stands_down():
    """The mic is capturing the whole ROOM, so "the mic means the user"
    is simply false. Even a perfectly clean timeline must not override
    anything."""
    doc = stood_down_document(
        "conference_room_mode", conference_room_mode=True,
        loopback_present=False)
    usable, reason = evaluate_trust(doc)
    assert usable is False
    assert reason == "conference_room_mode"

    # …and a document that somehow carries real spans is still refused
    # on the mode flag alone.
    mic, lb = two_stream_fixture()
    real = compute_attribution(
        mic, lb, SR, loopback_offset_s=0.0, conference_room_mode=True)
    assert evaluate_trust(real) == (False, "conference_room_mode")


def test_speaker_bleed_recording_stands_down():
    """The non-headset case, and the one where attribution is least
    reliable: far-end audio comes back in through the mic loudly enough
    to invert dominance. Both halves of the detector see it, and the
    result is a refusal — never a confident wrong answer."""
    mic, lb = two_stream_fixture(bleed_gain=10.0)
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)
    usable, reason = evaluate_trust(doc)
    assert usable is False
    assert reason in ("mic_hears_far_end", "overlap_dominant"), doc["summary"]

    turns = [
        {"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 6.0, "end": 10.0, "speaker": "SPEAKER_01"},
    ]
    out, stats = constrain_turns_to_owner(turns, doc)
    assert out == turns
    assert stats["applied"] is False


def test_harmless_quiet_bleed_does_not_stand_down():
    """The other side of that gate. Faint bleed correlates almost
    perfectly with the loopback envelope but changes no label, because
    the far end is still far louder on its own channel. Standing down
    there would throw the feature away on every real recording — the
    detector needs actual damage, not just the presence of bleed."""
    mic, lb = two_stream_fixture(bleed_gain=0.05)
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)
    assert doc["summary"]["bleed_correlation"] > 0.5, doc["summary"]
    assert doc["summary"]["contested_mic_fraction"] < 0.25
    assert evaluate_trust(doc) == (True, None), doc["summary"]
    assert _label_at(doc, 8.0) == LABEL_LOOPBACK


def test_unaligned_streams_stand_down():
    """Without the wallclock anchor the two streams were only
    right-aligned by length (the legacy recovery heuristic). They may
    be seconds apart, which is garbage at exactly the boundaries that
    matter, so we decline rather than guess."""
    mic, lb = two_stream_fixture()
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=None)
    assert doc["alignment"] == "none"
    assert evaluate_trust(doc) == (False, "no_wallclock_alignment")


def test_near_silent_recording_stands_down():
    """Percentile-based noise floors need something to anchor on. A
    recording with essentially no speech gets no opinion rather than a
    fabricated one."""
    rng = np.random.default_rng(5)
    n = int(SR * 20)
    mic = (rng.standard_normal(n) * 0.0008).astype(np.float32)
    lb = (rng.standard_normal(n) * 0.0004).astype(np.float32)
    doc = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)
    usable, reason = evaluate_trust(doc)
    assert usable is False
    assert reason in ("insufficient_speech", "no_confident_user_spans"), (
        doc["summary"])


def test_missing_sidecar_falls_back_cleanly():
    """Every session recorded before this shipped. No file, no error,
    no override — pure-voice diarization exactly as before."""
    turns = [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    out, stats = constrain_turns_to_owner(turns, None)
    assert out == turns
    assert stats["applied"] is False
    assert stats["reason"] == "no_sidecar"
    assert evaluate_trust(None) == (False, "no_sidecar")
    assert evaluate_trust({}) == (False, "no_sidecar")


def test_corrupt_and_future_sidecars_fall_back_cleanly(tmp_path: Path):
    """A half-written or newer-schema sidecar must degrade, never
    raise — the same degrade-don't-raise contract OwnerAliasStore and
    SpeakerProfileService hold."""
    audio = tmp_path / "session_DEAD.wav"
    bad = sidecar_path_for_audio(str(audio))
    bad.write_text("{not json", encoding="utf-8")
    assert load_sidecar_for_audio(str(audio)) is None

    from_future = {"version": 999, "loopback_present": True,
                   "alignment": "wallclock", "summary": {}, "spans": []}
    assert evaluate_trust(from_future) == (False, "unsupported_version")

    turns = [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00"}]
    assert constrain_turns_to_owner(turns, from_future)[0] == turns


def test_low_confidence_spans_never_override():
    """Confidence is not decoration. A hand-built timeline whose mic
    spans sit under the override bar produces no override at all."""
    doc = {
        "version": 1,
        "loopback_present": True,
        "conference_room_mode": False,
        "alignment": "wallclock",
        "summary": {
            "speech_seconds": 30.0,
            "overlap_fraction": 0.0,
            "bleed_correlation": 0.0,
            "contested_mic_fraction": 0.0,
            "mean_mic_confidence": 0.44,
        },
        "spans": [{"start": 0.0, "end": 10.0, "label": LABEL_MIC,
                   "confidence": 0.44}],
    }
    assert evaluate_trust(doc) == (False, "low_confidence")
    assert confident_intervals(doc, LABEL_MIC) == []


# ══ 3. Sidecar round-trip + the finalize integration ═════════════════


def test_sidecar_round_trips_and_carries_a_confidence_summary(tmp_path: Path):
    """A consumer must be able to tell a clean headset recording from a
    muddy speakerphone one WITHOUT re-reading the audio — which is gone
    by then."""
    mic, lb = two_stream_fixture()
    doc = compute_attribution(
        mic, lb, SR, loopback_offset_s=0.0, session_id="ABCD1234")
    audio = tmp_path / "session_ABCD1234.wav"
    path = sidecar_path_for_audio(str(audio))
    assert path.name == f"session_ABCD1234{SIDECAR_SUFFIX}"
    assert write_sidecar(path, doc) is True

    loaded = load_sidecar_for_audio(str(audio))
    assert loaded is not None
    summary = loaded["summary"]
    for key in ("overall_confidence", "mean_mic_confidence",
                "overlap_fraction", "bleed_correlation",
                "contested_mic_fraction", "usable", "stand_down_reason"):
        assert key in summary, summary
    assert summary["overall_confidence"] > 0.9
    assert json.loads(path.read_text(encoding="utf-8"))["spans"] == doc["spans"]


def _write_wav(path: Path, data: np.ndarray, samplerate: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), data.astype(np.float32), samplerate, subtype="FLOAT")
    return path


def test_finalize_writes_the_sidecar_without_changing_the_audio(
    recordings_dir: Path,
):
    """The load-bearing integration property: turning attribution on
    adds a sidecar and changes the merged WAV by exactly zero bytes.
    The user's only copy of the recording is not something this feature
    is allowed to touch."""
    mic, lb = two_stream_fixture()
    mic_path = _write_wav(recordings_dir / "_recording_CH1.wav", mic, SR)
    lb_path = _write_wav(recordings_dir / "_loopback_CH1.wav", lb, SR)

    plain = recordings_dir / "session_PLAIN.wav"
    finalize_recording_streaming(
        mic_wav_path=str(mic_path), loopback_wav_path=str(lb_path),
        output_wav_path=str(plain), loopback_start_offset_s=0.0,
    )
    attributed = recordings_dir / "session_CH1.wav"
    finalize_recording_streaming(
        mic_wav_path=str(mic_path), loopback_wav_path=str(lb_path),
        output_wav_path=str(attributed), loopback_start_offset_s=0.0,
        channel_attribution_enabled=True,
    )

    assert plain.read_bytes() == attributed.read_bytes()

    doc = load_sidecar_for_audio(str(attributed))
    assert doc is not None
    assert doc["session_id"] == "CH1"
    assert doc["alignment"] == "wallclock"
    assert doc["summary"]["usable"] is True, doc["summary"]
    assert _label_at(doc, 3.0) == LABEL_MIC
    assert _label_at(doc, 8.0) == LABEL_LOOPBACK
    # …and the default (flag absent) writes nothing at all.
    assert load_sidecar_for_audio(str(plain)) is None


def test_finalize_records_mic_only_and_conference_room_stand_downs(
    recordings_dir: Path,
):
    """Standing down is a RECORDED fact, not a silence. "no sidecar"
    and "a sidecar that says conference-room mode" are different
    things, and a field pull has to be able to tell them apart — the
    same lesson as Session.aec_outcome's "no decision came back"."""
    mic, lb = two_stream_fixture()
    mic_path = _write_wav(recordings_dir / "_recording_CH2.wav", mic, SR)
    lb_path = _write_wav(recordings_dir / "_loopback_CH2.wav", lb, SR)

    mic_only = recordings_dir / "session_MICONLY.wav"
    finalize_recording_streaming(
        mic_wav_path=str(mic_path), loopback_wav_path=None,
        output_wav_path=str(mic_only), channel_attribution_enabled=True,
    )
    doc = load_sidecar_for_audio(str(mic_only))
    assert doc["summary"]["stand_down_reason"] == "mic_only_recording"
    assert evaluate_trust(doc)[0] is False

    room = recordings_dir / "session_ROOM.wav"
    finalize_recording_streaming(
        mic_wav_path=str(mic_path), loopback_wav_path=str(lb_path),
        output_wav_path=str(room), loopback_start_offset_s=0.0,
        channel_attribution_enabled=True, conference_room_mode=True,
    )
    doc = load_sidecar_for_audio(str(room))
    assert doc["conference_room_mode"] is True
    assert doc["summary"]["stand_down_reason"] == "conference_room_mode"
    assert evaluate_trust(doc)[0] is False


def test_failed_merge_leaves_no_sidecar(recordings_dir: Path, monkeypatch):
    """A sidecar describing a WAV that was never produced is a trap: on
    the next launch recovery_service re-merges the preserved temps with
    the LEGACY right-alignment heuristic, putting the audio on a
    slightly different timeline that a stale sidecar would then be
    silently mapped onto. So the sidecar is computed before the merge
    (it needs the loopback temp) but committed only after it."""
    mic, lb = two_stream_fixture()
    mic_path = _write_wav(recordings_dir / "_recording_CH4.wav", mic, SR)
    lb_path = _write_wav(recordings_dir / "_loopback_CH4.wav", lb, SR)
    out = recordings_dir / "session_BOOM.wav"

    import utils.audio_utils as au
    real_soundfile = au.sf.SoundFile

    def exploding_soundfile(path, mode="r", **kwargs):
        if mode == "w" and str(path) == str(out):
            raise OSError("simulated write failure during merge")
        return real_soundfile(path, mode=mode, **kwargs)

    monkeypatch.setattr(au.sf, "SoundFile", exploding_soundfile)
    with pytest.raises(OSError):
        au.finalize_recording_streaming(
            mic_wav_path=str(mic_path), loopback_wav_path=str(lb_path),
            output_wav_path=str(out), loopback_start_offset_s=0.0,
            channel_attribution_enabled=True,
        )
    assert not sidecar_path_for_audio(str(out)).exists()


def test_streaming_and_in_memory_analysis_agree(recordings_dir: Path):
    """The finalize path streams (bounded memory on a 3-hour meeting);
    the reference path works in memory. They must produce the same
    timeline, or the tests above are testing something the product
    doesn't run."""
    mic, lb = two_stream_fixture()
    mic_path = _write_wav(recordings_dir / "_recording_CH3.wav", mic, SR)
    lb_path = _write_wav(recordings_dir / "_loopback_CH3.wav", lb, SR)

    streamed = compute_attribution_from_files(
        mic_wav_path=str(mic_path), mic_samplerate=SR,
        loopback_16k_path=str(lb_path), target_sr=SR,
        loopback_offset_frames=0,
    )
    in_memory = compute_attribution(mic, lb, SR, loopback_offset_s=0.0)
    assert ([(s["label"], round(s["start"], 2), round(s["end"], 2))
             for s in streamed["spans"]]
            == [(s["label"], round(s["start"], 2), round(s["end"], 2))
                for s in in_memory["spans"]])


def test_owner_label_is_the_existing_user_identity():
    """No second identity. The label these turns carry is the very
    string the live transcript already publishes for mic audio, so the
    user's speaker flows through the normal pipeline into the existing
    known-speakers store rather than into a parallel notion of "the
    user"."""
    assert OWNER_SPEAKER_LABEL == SPEAKER_YOU
