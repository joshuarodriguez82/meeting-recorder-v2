"""
A broken recording must be reported while it can still be fixed.

THE FIELD REPORT (2026-09-15)
-----------------------------
A 25-minute meeting was recorded mic-only: system audio — everyone else
on the call — failed to open at second zero::

    [FAIL] Loopback buffer=… failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    System audio capture unavailable: [Errno -9996] Invalid device info.

What the user was told, and when:

  * nothing for 45 seconds, although the failure was known immediately;
  * then "No system audio for 45 seconds — capture may have stopped.
    Consider stopping and restarting the recording" — but a restart
    re-opens the same device in the same format and is refused again;
  * only on the Record tab, which nobody is looking at during an
    auto-recorded meeting;
  * and afterwards, nothing: the session's sync check printed "lb=n/a"
    and raised no warning, so a one-sided transcript looked like a
    meeting in which one person talked.

These tests pin the replacement: immediate, cause-specific, coded for a
one-time notification, and recorded on the session.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.capture_health import (
    MIC_DEAD,
    REASON_DEVICE_IN_USE,
    REASON_DEVICE_UNAVAILABLE,
    REASON_UNKNOWN,
    REASON_UNSUPPORTED_FORMAT,
    SYSTEM_AUDIO_DEAD,
    SYSTEM_AUDIO_UNAVAILABLE,
    classify_open_error,
    live_capture_issue,
    missing_system_audio_warning,
)

# The exact strings from the field log.
FIELD_FORMAT_ERROR = ("Error starting stream: Unanticipated host error "
                      "[PaErrorCode -9999]: 'AUDCLNT_E_UNSUPPORTED_FORMAT' "
                      "[Windows WASAPI error -2004287480]")
FIELD_DEVICE_ERROR = "[Errno -9996] Invalid device info"


def _issue(**kw):
    base = dict(elapsed_s=1.0, mic_silent_for_s=0.1,
                system_configured=True, system_open_error=None,
                system_silent_for_s=0.1, platform="win32",
                grace_s=5.0, dead_after_s=45.0)
    base.update(kw)
    return live_capture_issue(**base)


# ── Classifying the field errors ────────────────────────────────────

@pytest.mark.parametrize("error,reason", [
    (FIELD_FORMAT_ERROR, REASON_UNSUPPORTED_FORMAT),
    ("Error opening InputStream: Invalid sample rate [PaErrorCode -9997]",
     REASON_UNSUPPORTED_FORMAT),
    ("Invalid number of channels [PaErrorCode -9998]",
     REASON_UNSUPPORTED_FORMAT),
    (FIELD_DEVICE_ERROR, REASON_DEVICE_UNAVAILABLE),
    ("'AUDCLNT_E_DEVICE_INVALIDATED'", REASON_DEVICE_UNAVAILABLE),
    ("'AUDCLNT_E_DEVICE_IN_USE'", REASON_DEVICE_IN_USE),
    ("something nobody has seen before", REASON_UNKNOWN),
    ("", REASON_UNKNOWN),
    (None, REASON_UNKNOWN),
])
def test_open_errors_are_classified_from_the_real_strings(error, reason):
    assert classify_open_error(error) == reason


# ── The live warning ────────────────────────────────────────────────

def test_a_refused_system_audio_open_is_reported_at_once():
    """The field defect: known at second zero, reported at second 45.
    One second in — inside the start-up grace — it must already say so."""
    issue = _issue(elapsed_s=1.0, system_open_error=FIELD_FORMAT_ERROR)
    assert issue is not None
    assert issue.code == SYSTEM_AUDIO_UNAVAILABLE


def test_it_says_the_other_participants_are_missing():
    """Name the consequence, not the plumbing. 'System audio' means
    nothing to someone in a meeting; 'the others aren't recorded' does."""
    issue = _issue(system_open_error=FIELD_FORMAT_ERROR)
    assert "Other participants are NOT being recorded" in issue.message


def test_a_refused_format_is_not_told_to_just_restart():
    """Restarting re-opens the same device in the same format. The fix
    that works is changing the format, so that is what it must say."""
    issue = _issue(system_open_error=FIELD_FORMAT_ERROR, platform="win32")
    assert "Default Format" in issue.message
    assert "may have stopped" not in issue.message


def test_windows_advice_is_not_given_on_macos():
    issue = _issue(system_open_error=FIELD_FORMAT_ERROR, platform="darwin")
    assert "Windows" not in issue.message
    assert "Sound settings" not in issue.message


def test_an_unplugged_speaker_is_told_to_pick_the_current_one():
    issue = _issue(system_open_error=FIELD_DEVICE_ERROR)
    assert "unplugged or switched" in issue.message


def test_an_unknown_cause_shows_the_error_rather_than_guessing():
    issue = _issue(system_open_error="PaErrorCode -1234: weird driver")
    assert "weird driver" in issue.message


def test_a_raw_error_is_truncated():
    issue = _issue(system_open_error="x" * 5000)
    assert len(issue.message) < 600


# ── Precedence ──────────────────────────────────────────────────────

def test_a_dead_microphone_outranks_missing_system_audio():
    """No mic means nothing at all is being recorded — strictly worse
    than a one-sided recording."""
    issue = _issue(elapsed_s=120.0, mic_silent_for_s=60.0,
                   system_open_error=FIELD_FORMAT_ERROR)
    assert issue.code == MIC_DEAD


def test_system_audio_that_stopped_mid_meeting_keeps_the_old_message():
    """This is the one case where 'may have stopped, try restarting' is
    honest: it opened, flowed, then went quiet."""
    issue = _issue(elapsed_s=300.0, system_open_error=None,
                   system_silent_for_s=60.0)
    assert issue.code == SYSTEM_AUDIO_DEAD
    assert "may have stopped" in issue.message


def test_stopped_system_audio_still_waits_for_the_threshold():
    """Silence is not failure: a far end that is simply quiet for 20 s
    must not raise a warning that trains people to ignore it."""
    assert _issue(elapsed_s=300.0, system_silent_for_s=20.0) is None


# ── Choices are not failures ────────────────────────────────────────

def test_no_system_audio_configured_is_never_a_warning():
    """In-room meetings are recorded mic-only on purpose."""
    assert _issue(system_configured=False,
                  system_open_error=FIELD_FORMAT_ERROR) is None
    assert _issue(system_configured=False, elapsed_s=600.0,
                  system_silent_for_s=None) is None


def test_a_healthy_recording_has_no_issue():
    assert _issue(elapsed_s=600.0) is None


# ── The session, afterwards ─────────────────────────────────────────

def test_a_session_with_no_system_audio_says_so():
    """'lb=n/a' used to mean no warning at all."""
    w = missing_system_audio_warning(
        system_configured=True, system_samples=0,
        system_open_error=FIELD_FORMAT_ERROR)
    assert w is not None
    assert "Other participants were not recorded" in w
    assert "your side of the conversation only" in w


def test_the_session_warning_names_the_cause():
    w = missing_system_audio_warning(
        system_configured=True, system_samples=0,
        system_open_error=FIELD_FORMAT_ERROR)
    assert "format was refused" in w


def test_a_session_that_captured_system_audio_has_no_such_warning():
    assert missing_system_audio_warning(
        system_configured=True, system_samples=16000 * 60,
        system_open_error=None) is None


def test_a_mic_only_session_by_choice_has_no_such_warning():
    assert missing_system_audio_warning(
        system_configured=False, system_samples=0,
        system_open_error=None) is None


def test_system_audio_that_opened_but_never_delivered_is_still_reported():
    """No open error, zero samples: the stream opened and nothing ever
    arrived. Different mechanism, same result for the meeting."""
    w = missing_system_audio_warning(
        system_configured=True, system_samples=0, system_open_error=None)
    assert w is not None and "never started" in w


# ── Wired into the real service ─────────────────────────────────────

from tests._app_import import _stub_optional_modules  # noqa: E402

_stub_optional_modules()

from services import recording_service as rs  # noqa: E402


class _Settings:
    def __init__(self, recordings_dir):
        self.recordings_dir = str(recordings_dir)
        self.echo_cancellation_enabled = False
        self.channel_attribution_enabled = False


class _Capture:
    mic_start_monotonic = None
    loopback_start_monotonic = None

    def __init__(self, loopback_error=None, loopback_samples=0):
        self.loopback_error = loopback_error
        self._lb = loopback_samples

    def stop(self):
        pass

    def get_capture_stats(self):
        return {"mic_sr": 16000, "loopback_sr": 16000,
                "mic_samples": 16000 * 60, "loopback_samples": self._lb,
                "mic_overflows": 0, "loopback_overflows": 0}


def _recording(svc, tmp_path, *, loopback_error=None, configured=True,
               loopback_samples=0, seconds_in=2.0):
    session = rs.Session(session_id="S")
    session.started_at = datetime.now() - timedelta(seconds=seconds_in)
    svc._session = session
    svc._recording = True
    svc._capture = _Capture(loopback_error, loopback_samples)
    svc._chunk_count = 10
    svc._wav_writer = None
    svc._live_transcriber = None
    svc._last_chunk_at = datetime.now()
    svc._wav_temp_path = str(tmp_path / "_recording_S.wav")
    (tmp_path / "_recording_S.wav").write_bytes(b"RIFF" + b"\0" * 64)
    svc._loopback_temp_path = (str(tmp_path / "_loopback_S.wav")
                               if configured else None)
    return session


def test_status_reports_the_refused_open_on_the_first_poll(tmp_path):
    """Two seconds into the recording — the real service, not the pure
    function — already carries the code the UI notifies on."""
    svc = rs.RecordingService(settings=_Settings(tmp_path))
    _recording(svc, tmp_path, loopback_error=FIELD_FORMAT_ERROR)

    levels = svc.get_capture_levels()

    assert levels["capture_warning_code"] == SYSTEM_AUDIO_UNAVAILABLE
    assert "Other participants" in levels["capture_warning"]
    assert levels["system_state"] == "dead"


def test_status_has_no_code_when_nothing_is_wrong(tmp_path):
    svc = rs.RecordingService(settings=_Settings(tmp_path))
    _recording(svc, tmp_path, loopback_error=None, configured=False)
    levels = svc.get_capture_levels()
    assert levels["capture_warning"] is None
    assert levels["capture_warning_code"] is None


def test_the_stopped_session_carries_the_warning(tmp_path, monkeypatch):
    """Drives the real stop path: configured system audio, zero samples
    delivered — the session must say the others were not recorded."""
    svc = rs.RecordingService(settings=_Settings(tmp_path))
    _recording(svc, tmp_path, loopback_error=FIELD_FORMAT_ERROR,
               seconds_in=60.0)

    import contextlib

    @contextlib.contextmanager
    def _slot(on_queued=None):
        yield

    def _finalize(**kwargs):
        from pathlib import Path
        Path(kwargs["output_wav_path"]).parent.mkdir(parents=True,
                                                     exist_ok=True)
        Path(kwargs["output_wav_path"]).write_bytes(b"RIFF" + b"\0" * 64)
        return (60.0, False, None)

    monkeypatch.setattr(rs, "finalize_slot", _slot)
    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        staticmethod(_finalize))

    session = svc.stop_recording()

    assert session.capture_warning is not None
    assert session.capture_warning.startswith(
        "Other participants were not recorded")
    # Not folded into the informational drift chip, which the Sessions
    # list renders as "no audio was altered".
    assert "Other participants" not in (session.sync_warning or "")


def _stop_with(svc, monkeypatch):
    import contextlib

    @contextlib.contextmanager
    def _slot(on_queued=None):
        yield

    def _finalize(**kwargs):
        from pathlib import Path
        out = Path(kwargs["output_wav_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF" + b"\0" * 64)
        return (10.0, False, None)

    monkeypatch.setattr(rs, "finalize_slot", _slot)
    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        staticmethod(_finalize))
    return svc.stop_recording()


def test_a_short_call_missing_the_other_side_is_still_flagged(tmp_path,
                                                             monkeypatch):
    """The drift check only runs past 30 s. A ten-second call with no
    far end is missing the far end all the same."""
    svc = rs.RecordingService(settings=_Settings(tmp_path))
    _recording(svc, tmp_path, loopback_error=FIELD_FORMAT_ERROR,
               seconds_in=10.0)
    session = _stop_with(svc, monkeypatch)
    assert session.capture_warning is not None


def test_unreadable_stats_with_a_known_open_failure_is_flagged(tmp_path,
                                                               monkeypatch):
    svc = rs.RecordingService(settings=_Settings(tmp_path))
    _recording(svc, tmp_path, loopback_error=FIELD_FORMAT_ERROR,
               seconds_in=60.0)

    def _boom():
        raise RuntimeError("stats unavailable")
    svc._capture.get_capture_stats = _boom
    session = _stop_with(svc, monkeypatch)
    assert session.capture_warning is not None


def test_unreadable_stats_alone_is_not_evidence_of_missing_audio(
        tmp_path, monkeypatch):
    """No stats means we do not know — not that nothing arrived. A
    warning that 'the others were not recorded' on a meeting where they
    were would be worse than silence."""
    svc = rs.RecordingService(settings=_Settings(tmp_path))
    _recording(svc, tmp_path, loopback_error=None, seconds_in=60.0)

    def _boom():
        raise RuntimeError("stats unavailable")
    svc._capture.get_capture_stats = _boom
    session = _stop_with(svc, monkeypatch)
    assert session.capture_warning is None


def test_a_session_that_captured_the_far_end_is_not_flagged(tmp_path,
                                                            monkeypatch):
    svc = rs.RecordingService(settings=_Settings(tmp_path))
    _recording(svc, tmp_path, loopback_error=None,
               loopback_samples=16000 * 60, seconds_in=60.0)
    session = _stop_with(svc, monkeypatch)
    assert session.capture_warning is None


def test_the_warning_survives_a_save_and_reload(tmp_path):
    """It is read the next day, from disk, by the Sessions list."""
    s1 = rs.Session(session_id="S")
    s1.capture_warning = "Other participants were not recorded — x."
    s2 = rs.Session.from_dict(s1.to_dict())
    assert s2.capture_warning == s1.capture_warning


def _recorder(into):
    """Same signature as utils.events.emit — session_id is positional in
    some real call sites, so a narrower fake breaks stop_recording."""
    def emit(event, session_id=None, **fields):
        if session_id is not None:
            fields = {"session_id": session_id, **fields}
        into.append((event, fields))
    return emit


class _RefusingCapture:
    """Stands in for core.audio_capture.AudioCapture at the one boundary
    that matters here: start() succeeds for the mic and records WHY
    system audio would not open, exactly as _start_loopback_windows
    does after its ladder is exhausted."""

    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.loopback_error = None
        self.actual_sr = 16000
        self.mic_start_monotonic = None
        self.loopback_start_monotonic = None
        _RefusingCapture.instances.append(self)

    def start(self):
        if self.kwargs.get("output_device_index") is not None:
            self.loopback_error = FIELD_FORMAT_ERROR

    def stop(self):
        pass

    def get_capture_stats(self):
        return {"mic_sr": 16000, "loopback_sr": 16000,
                "mic_samples": 16000 * 60, "loopback_samples": 0,
                "mic_overflows": 0, "loopback_overflows": 0}


def test_the_field_failure_end_to_end(tmp_path, monkeypatch):
    """The real start_recording → status poll → stop_recording, with only
    the sound card faked. Each stage is where the field meeting went
    quiet: no event at start, nothing for 45 s, nothing on the session."""
    from utils import events as ev

    emitted = []
    monkeypatch.setattr(rs.events, "emit", _recorder(emitted))
    monkeypatch.setattr(rs, "AudioCapture", _RefusingCapture)
    monkeypatch.setattr(rs, "_local_capture_dir", lambda: tmp_path)

    settings = _Settings(tmp_path / "rec")
    settings.live_transcription_enabled = False
    svc = rs.RecordingService(settings=settings)
    try:
        session = svc.start_recording(mic_device_index=0,
                                      output_device_index=5)

        # 1. Counted at start, by code.
        degraded = [f for n, f in emitted if n == ev.CAPTURE_DEGRADED]
        assert degraded == [{"session_id": session.session_id,
                             "stream": "system",
                             "reason": REASON_UNSUPPORTED_FORMAT}]

        # 2. Reported on the first poll — well inside the 5 s grace.
        levels = svc.get_capture_levels()
        assert levels["capture_warning_code"] == SYSTEM_AUDIO_UNAVAILABLE

        # 3. Carried by the session afterwards.
        session.started_at = datetime.now() - timedelta(seconds=60)
        svc._chunk_count = 10

        import contextlib

        @contextlib.contextmanager
        def _slot(on_queued=None):
            yield

        def _finalize(**kwargs):
            from pathlib import Path
            out = Path(kwargs["output_wav_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"RIFF" + b"\0" * 64)
            return (60.0, False, None)

        monkeypatch.setattr(rs, "finalize_slot", _slot)
        monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                            staticmethod(_finalize))
        stopped = svc.stop_recording()
        assert stopped.capture_warning.startswith(
            "Other participants were not recorded — the speaker's audio "
            "format was refused.")
    finally:
        svc._stop_session_log()


def test_no_system_device_means_no_degraded_event(tmp_path, monkeypatch):
    """Mic-only by choice (in-room meeting, or no speaker picked) is not
    a degraded start and must not be counted as one."""
    from utils import events as ev

    emitted = []
    monkeypatch.setattr(rs.events, "emit", _recorder(emitted))
    monkeypatch.setattr(rs, "AudioCapture", _RefusingCapture)
    monkeypatch.setattr(rs, "_local_capture_dir", lambda: tmp_path)
    settings = _Settings(tmp_path / "rec")
    settings.live_transcription_enabled = False
    svc = rs.RecordingService(settings=settings)
    try:
        svc.start_recording(mic_device_index=0, output_device_index=None)
        assert not [n for n, _ in emitted if n == ev.CAPTURE_DEGRADED]
        assert svc.get_capture_levels()["capture_warning_code"] is None
    finally:
        svc._recording = False
        svc._stop_session_log()


def test_the_degraded_event_is_registered():
    from utils import events as ev
    assert ev.CAPTURE_DEGRADED in ev.ALL_EVENTS
    record = ev.build_record(ev.CAPTURE_DEGRADED, session_id="S",
                             stream="system",
                             reason=REASON_UNSUPPORTED_FORMAT)
    assert record.get("reason") == REASON_UNSUPPORTED_FORMAT


# ── Through the HTTP status the UI actually polls ───────────────────

import asyncio  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

FRONTEND_FIXTURE = (Path(__file__).resolve().parents[2] / "src" / "lib"
                    / "__fixtures__"
                    / "recording-status-system-audio-unavailable.json")


def _field_status(tmp_path, monkeypatch, platform=None):
    """GET /recording/status for a recording in the field state — the
    real handler, the real RecordingService, only the sound card faked."""
    from tests._app_import import import_app
    import server

    import_app()
    monkeypatch.setattr(rs, "AudioCapture", _RefusingCapture)
    monkeypatch.setattr(rs, "_local_capture_dir", lambda: tmp_path)
    monkeypatch.setattr(rs.events, "emit", _recorder([]))
    settings = _Settings(tmp_path / "rec")
    settings.live_transcription_enabled = False
    rec = rs.RecordingService(settings=settings)
    rec.start_recording(mic_device_index=0, output_device_index=5)
    if platform:
        # Only the wording of the remedy reads it; set after start so
        # nothing platform-specific in start_recording is exercised.
        monkeypatch.setattr(rs.sys, "platform", platform)
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "recording_svc", rec)
    monkeypatch.setattr(server.svc, "record_started_at", datetime.now())
    try:
        result = asyncio.run(server.recording_status())
        return json.loads(result.model_dump_json())
    finally:
        rec._recording = False
        rec._stop_session_log()


def test_the_status_endpoint_carries_the_code(tmp_path, monkeypatch):
    """The field defect's last hop: a code computed in the service but
    dropped by the response model never reaches the UI."""
    body = _field_status(tmp_path, monkeypatch)
    assert body["is_recording"] is True
    assert body["capture_warning_code"] == SYSTEM_AUDIO_UNAVAILABLE
    assert body["capture_warning"].startswith(
        "Other participants are NOT being recorded")


def test_the_frontend_fixture_is_what_the_endpoint_sends(tmp_path,
                                                        monkeypatch):
    """src/lib/__fixtures__/recording-status-system-audio-unavailable.json
    was captured from this endpoint (with sys.platform = win32, the
    field platform). If the response gains, loses or renames a field,
    the frontend tests would keep passing against a payload the backend
    no longer sends — so the fixture is held to the producer here."""
    fixture = json.loads(FRONTEND_FIXTURE.read_text(encoding="utf-8"))
    body = _field_status(tmp_path, monkeypatch)
    assert sorted(fixture) == sorted(body)
    assert fixture["capture_warning_code"] == body["capture_warning_code"]
    assert fixture["is_recording"] == body["is_recording"]
