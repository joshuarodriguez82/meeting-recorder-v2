"""
events.jsonl — the structured outcome log.

WHY IT EXISTS
-------------
Five real field bugs, none caught by a test, all diagnosed by
regex-scraping prose out of a 231 MB backend.log. These tests cover the
two things that make the replacement trustworthy:

1. the events actually fire on a simulated session, with the numbers a
   diagnosis needs, and
2. nothing that could identify a person, a meeting or a machine can get
   into the file — enforced mechanically, not by reviewer discipline,
   because the file is designed to be handed to a third party.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from utils import events  # noqa: E402
from services import recording_service as rs  # noqa: E402


@pytest.fixture
def event_log(tmp_path: Path):
    """Point the event log at a temp file for the duration of a test."""
    path = tmp_path / "events.jsonl"
    events.configure(path, max_bytes=1024 * 1024, backup_count=2)
    yield path
    events.reset()


def _read(path: Path):
    return events.read_events(path)


def _by_name(records, name):
    return [r for r in records if r.get("event") == name]


# ── envelope ─────────────────────────────────────────────────────────

def test_every_record_is_one_json_object_per_line(event_log):
    events.emit(events.BACKEND_START, app_version="2.32.0")
    events.emit(events.BACKEND_STOP, uptime_s=12.5)

    lines = event_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert isinstance(obj, dict)
        assert obj["ts"] and obj["event"] and obj["v"] == events.SCHEMA_VERSION
        assert isinstance(obj["pid"], int)


def test_session_id_is_carried_when_relevant(event_log):
    events.emit(events.FINALIZE_COMPLETED, "A1B2C3D4", duration_s=191.9)
    rec = _read(event_log)[0]
    assert rec["session_id"] == "A1B2C3D4"
    assert rec["duration_s"] == 191.9


def test_typed_fields_survive_round_trip(event_log):
    events.emit(
        "capture.stopped", "SESS0001",
        mic_seconds=3421.0, mic_overflows=0, stats_available=True,
        drift_s=None, by_reason={"overlap_dominant": 2},
    )
    rec = _read(event_log)[0]
    assert rec["mic_seconds"] == 3421.0
    assert rec["mic_overflows"] == 0
    assert rec["stats_available"] is True
    assert rec["drift_s"] is None
    assert rec["by_reason"] == {"overlap_dominant": 2}


# ── the privacy backstop ─────────────────────────────────────────────

def test_free_prose_cannot_reach_the_file(event_log):
    """A caller passing transcript text, a meeting title or an attendee
    name must not be able to write it here, even by accident.

    This test caught a real hole during development: the token regex
    originally permitted spaces and relied on a 64-character cap to
    stop prose, which let a 48-character sentence — and would have let
    most real meeting titles — straight through. Spaces are banned now;
    that is what this asserts."""
    events.emit(
        "capture.stopped", "SESS0001",
        reason="So then I told the client we would ship on Friday",
        title="Q3 Roadmap sync with Contoso",
        attendee="Jane Doe <jane.doe@contoso.com>",
    )
    raw = event_log.read_text(encoding="utf-8")
    for leaked in ("client", "Contoso", "Jane Doe", "jane.doe@contoso.com"):
        assert leaked not in raw, f"{leaked!r} leaked into events.jsonl"

    rec = _read(event_log)[0]
    for key in ("reason", "title", "attendee"):
        assert rec[key] == "<redacted:not-a-token>"


def test_file_paths_cannot_reach_the_file(event_log):
    """Paths carry the user's account name on every platform."""
    events.emit(
        "finalize.failed", "SESS0001",
        error_type="RuntimeError",
        detail=r"C:\Users\jrodriguez\AppData\Local\MeetingRecorder\x.wav",
        posix_detail="/Users/jrodriguez/Library/Application Support/x.wav",
    )
    raw = event_log.read_text(encoding="utf-8")
    assert "jrodriguez" not in raw
    assert "Users" not in raw
    # The genuinely useful part survives.
    assert _read(event_log)[0]["error_type"] == "RuntimeError"


def test_enum_reason_codes_do_survive(event_log):
    """The redaction must not be so blunt that the reason codes a
    diagnosis actually runs on get thrown away too."""
    for reason in ("erle_non_positive", "overlap_dominant",
                   "no_decision_returned", "briefing-fallback",
                   "finalize_subprocess_failed", "mic_only"):
        events.emit("test.reason", reason=reason)
    got = [r["reason"] for r in _read(event_log)]
    assert got == ["erle_non_positive", "overlap_dominant",
                   "no_decision_returned", "briefing-fallback",
                   "finalize_subprocess_failed", "mic_only"]


def test_unknown_object_types_are_recorded_as_types_not_values(event_log):
    class Secretish:
        def __repr__(self):
            return "sk-ant-api03-REALKEYMATERIAL"

    events.emit("test.obj", thing=Secretish())
    raw = event_log.read_text(encoding="utf-8")
    assert "REALKEYMATERIAL" not in raw
    assert _read(event_log)[0]["thing"] == "<unsupported:Secretish>"


def test_bad_keys_are_dropped(event_log):
    events.emit("test.keys", **{"good_key": 1})
    rec = events.build_record("test.keys", None, **{"Bad-Key": "x"})
    assert "Bad-Key" not in rec
    assert _read(event_log)[0]["good_key"] == 1


def test_emit_never_raises(tmp_path):
    """Observability must not be able to break the thing it observes."""
    events.configure(tmp_path / "nope" / "deep" / "events.jsonl")
    try:
        events.emit("test.x", value=object())
        events.emit("test.x", **{"k": {"nested": {"deep": {"deeper": 1}}}})
    finally:
        events.reset()


# ── rotation ─────────────────────────────────────────────────────────

def test_event_log_rotation_is_bounded(tmp_path):
    path = tmp_path / "events.jsonl"
    events.configure(path, max_bytes=4096, backup_count=2)
    try:
        for i in range(2000):
            events.emit("test.filler", "SESS0001", index=i, padding="p" * 60)
    finally:
        events.reset()

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["events.jsonl", "events.jsonl.1", "events.jsonl.2"]
    total = sum(p.stat().st_size for p in tmp_path.iterdir())
    # 3 slots x (4096 + one oversized final line) — the point is that
    # 2000 events did not become an unbounded file.
    assert total < 4096 * 3 + 4096


def test_default_caps_are_documented_and_bounded():
    assert events.DEFAULT_MAX_BYTES == 4 * 1024 * 1024
    assert events.DEFAULT_BACKUP_COUNT == 3
    ceiling = events.DEFAULT_MAX_BYTES * (events.DEFAULT_BACKUP_COUNT + 1)
    assert ceiling == 16 * 1024 * 1024


# ── a simulated session end-to-end ───────────────────────────────────
#
# Reuses the headless RecordingService harness from test_finalize_timing
# (no audio hardware, no real files, finalize monkeypatched).

class _FakeSettings:
    def __init__(self, recordings_dir, echo_cancellation_enabled=False):
        self.recordings_dir = str(recordings_dir)
        self.echo_cancellation_enabled = echo_cancellation_enabled
        self.channel_attribution_enabled = True


class _FakeCapture:
    def __init__(self, mic_samples, mic_sr=16000, loopback_samples=0,
                 loopback_sr=16000, mic_overflows=0, loopback_overflows=0):
        self.mic_start_monotonic = None
        self.loopback_start_monotonic = None
        self._stats = {
            "mic_sr": mic_sr, "loopback_sr": loopback_sr,
            "mic_samples": mic_samples, "loopback_samples": loopback_samples,
            "mic_overflows": mic_overflows,
            "loopback_overflows": loopback_overflows,
        }

    def stop(self):
        pass

    def get_capture_stats(self):
        return self._stats


def _arm(svc, *, started_at, mic_samples, mic_sr=16000,
         loopback_samples=0, loopback_sr=16000, mic_overflows=0):
    session = rs.Session(session_id="SESS0001")
    session.started_at = started_at
    svc._session = session
    svc._recording = True
    svc._capture = _FakeCapture(
        mic_samples=mic_samples, mic_sr=mic_sr,
        loopback_samples=loopback_samples, loopback_sr=loopback_sr,
        mic_overflows=mic_overflows)
    svc._chunk_count = 1
    svc._wav_temp_path = "/tmp/does-not-exist-mic.wav"
    svc._loopback_temp_path = None
    svc._live_transcriber = None
    svc._wav_writer = None
    return session


def test_simulated_session_stop_emits_the_diagnostic_events(
        tmp_path, monkeypatch, event_log):
    """One stop must produce the three events the field debugging
    actually needed: what capture delivered, what finalize did (with the
    AEC verdict), and whether the audio is as long as it should be."""
    window_s = 1800.0
    mic_sr = 16000
    aec = {"requested": True, "accepted": False,
           "reason": "erle_non_positive", "erle_db": -0.4,
           "residual_delay_ms": 12}

    def fake_finalize(**kwargs):
        return (window_s, False, aec)

    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        staticmethod(fake_finalize))

    svc = rs.RecordingService(
        settings=_FakeSettings(tmp_path, echo_cancellation_enabled=True))
    _arm(svc, started_at=datetime.now() - timedelta(seconds=window_s),
         mic_samples=int(window_s * mic_sr), mic_sr=mic_sr,
         loopback_samples=int((window_s - 3.0) * mic_sr), mic_overflows=2)

    svc.stop_recording()
    records = _read(event_log)

    # capture.stopped — the SYNC_INTEGRITY numbers, typed.
    cap = _by_name(records, events.CAPTURE_STOPPED)
    assert len(cap) == 1
    cap = cap[0]
    assert cap["session_id"] == "SESS0001"
    assert cap["mic_seconds"] == pytest.approx(window_s, abs=1.0)
    assert cap["loopback_seconds"] == pytest.approx(window_s - 3.0, abs=1.0)
    assert cap["mic_sample_rate"] == mic_sr
    assert cap["loopback_sample_rate"] == mic_sr
    assert cap["mic_overflows"] == 2
    assert cap["loopback_overflows"] == 0
    assert cap["capture_window_s"] == pytest.approx(window_s, abs=2.0)
    assert cap["mic_gap_s"] == pytest.approx(0.0, abs=2.0)
    assert cap["drift_s"] == pytest.approx(3.0, abs=1.0)
    assert cap["stats_available"] is True

    # finalize.completed — duration, queued-vs-running, and the AEC
    # decision that used to exist only as prose inside a child process.
    fin = _by_name(records, events.FINALIZE_COMPLETED)
    assert len(fin) == 1
    fin = fin[0]
    assert fin["session_id"] == "SESS0001"
    assert fin["queued"] is False
    assert fin["aec_requested"] is True
    assert fin["aec_accepted"] is False
    assert fin["aec_reason"] == "erle_non_positive"
    assert fin["erle_db"] == -0.4
    assert fin["residual_delay_ms"] == 12
    assert fin["duration_s"] >= 0.0

    # audio.integrity — actual vs expected, with an explicit verdict so
    # "healthy" is a value rather than the absence of a log line.
    integ = _by_name(records, events.AUDIO_INTEGRITY)
    assert len(integ) == 1
    integ = integ[0]
    assert integ["actual_duration_s"] == pytest.approx(window_s, abs=1.0)
    assert integ["expected_duration_s"] == pytest.approx(window_s, abs=2.0)
    assert integ["verdict"] == "ok"


def test_simulated_session_with_lost_audio_is_visible_as_a_deficit(
        tmp_path, monkeypatch, event_log):
    window_s = 1800.0

    def fake_finalize(**kwargs):
        return (600.0, False, None)  # 10 min of a 30-min window

    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        staticmethod(fake_finalize))

    svc = rs.RecordingService(settings=_FakeSettings(tmp_path))
    _arm(svc, started_at=datetime.now() - timedelta(seconds=window_s),
         mic_samples=int(window_s * 16000))
    svc.stop_recording()

    integ = _by_name(_read(event_log), events.AUDIO_INTEGRITY)[0]
    assert integ["verdict"] == "deficit"
    assert integ["deficit_ratio"] == pytest.approx(2.0 / 3.0, abs=0.02)


def test_finalize_failure_records_the_type_not_the_message(
        tmp_path, monkeypatch, event_log):
    """Finalize errors embed the WAV's absolute path. The event must
    carry the exception TYPE and nothing else."""
    def fake_finalize(**kwargs):
        raise RuntimeError(
            r"could not write C:\Users\jrodriguez\...\session_X.wav")

    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        staticmethod(fake_finalize))

    svc = rs.RecordingService(settings=_FakeSettings(tmp_path))
    _arm(svc, started_at=datetime.now() - timedelta(seconds=120),
         mic_samples=120 * 16000)
    svc.stop_recording()

    raw = event_log.read_text(encoding="utf-8")
    assert "jrodriguez" not in raw
    failed = _by_name(_read(event_log), events.FINALIZE_FAILED)
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"
    # capture.stopped still fires — a failed finalize must not cost us
    # the capture telemetry, which is where the cause usually is.
    assert _by_name(_read(event_log), events.CAPTURE_STOPPED)


def test_channel_attribution_verdict_is_emitted(tmp_path, event_log,
                                                monkeypatch):
    """usable / stand-down reason / confidence / overlap — the v2.32.0
    feature whose verdict is otherwise invisible after the fact."""
    doc = {
        "loopback_present": True,
        "conference_room_mode": False,
        "aec_applied": False,
        "alignment": "wallclock",
        "summary": {
            "usable": False,
            "stand_down_reason": "overlap_dominant",
            "overall_confidence": 0.41,
            "overlap_fraction": 0.62,
            "mic_fraction": 0.2,
            "loopback_fraction": 0.18,
            "silence_fraction": 0.4,
            "mean_mic_confidence": 0.55,
            "span_count": 88,
        },
    }
    import core.channel_attribution as ca
    monkeypatch.setattr(ca, "load_sidecar_for_audio", lambda p: doc)

    session = rs.Session(session_id="SESS0001")
    session.audio_path = str(tmp_path / "session_SESS0001.wav")
    assert rs.RecordingService._load_channel_attribution(session) is doc

    rec = _by_name(_read(event_log), events.CHANNEL_ATTRIBUTION)[0]
    assert rec["session_id"] == "SESS0001"
    assert rec["state"] == "loaded"
    assert rec["usable"] is False
    assert rec["stand_down_reason"] == "overlap_dominant"
    assert rec["overall_confidence"] == 0.41
    assert rec["overlap_fraction"] == 0.62
    assert rec["span_count"] == 88


def test_channel_attribution_absence_is_a_state_not_a_silence(
        tmp_path, event_log, monkeypatch):
    """"No sidecar" and "sidecar says stand down" must not look the
    same — that ambiguity is the failure mode this file exists for."""
    import core.channel_attribution as ca
    monkeypatch.setattr(ca, "load_sidecar_for_audio", lambda p: None)

    session = rs.Session(session_id="SESS0002")
    session.audio_path = str(tmp_path / "session_SESS0002.wav")
    assert rs.RecordingService._load_channel_attribution(session) is None

    rec = _by_name(_read(event_log), events.CHANNEL_ATTRIBUTION)[0]
    assert rec["state"] == "absent"
    assert rec["usable"] is None
    assert rec["stand_down_reason"] is None


def test_document_indexing_counts_are_emitted_without_paths(
        tmp_path, event_log):
    from services import document_service as ds

    report = {
        "indexed": 12, "unchanged": 3, "total_chunks": 480,
        "skipped": [
            {"file": r"C:\Users\jrodriguez\Docs\logo.png",
             "reason": "not a text document — images aren't indexed",
             "expected": True},
            {"file": r"C:\Users\jrodriguez\Docs\model.csv",
             "reason": "unsupported file type: .csv", "expected": False},
            {"file": r"C:\Users\jrodriguez\Docs\deck.pptx",
             "reason": "corrupt or unreadable pptx: bad zip",
             "expected": False},
        ],
    }
    ds._emit_index_event(report)

    raw = event_log.read_text(encoding="utf-8")
    assert "jrodriguez" not in raw
    assert "logo" not in raw and "model" not in raw

    rec = _by_name(_read(event_log), events.DOCUMENTS_INDEXED)[0]
    assert rec["indexed"] == 12
    assert rec["unchanged"] == 3
    assert rec["skipped"] == 3
    assert rec["expected_skips"] == 1
    assert rec["total_chunks"] == 480
    assert rec["skipped_by_reason"] == {
        "not_a_text_document": 1,
        "unsupported_file_type": 1,
        "corrupt_or_unreadable": 1,
    }
    assert rec["skipped_by_extension"] == {"png": 1, "csv": 1, "pptx": 1}
