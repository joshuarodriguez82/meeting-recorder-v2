"""
Back-to-back meetings must not share one recording's files.

THE FIELD LOG (2026-09-15, Windows, auto-record on)
---------------------------------------------------
Three meetings, back to back. From backend.log::

    13:30:03  [stop] meeting A → finalize starts (it takes 82.7 s)
    13:30:08  meeting B starts recording
    13:30:28  [stop] meeting B → "queued behind another in-flight finalize"
    13:30:42  meeting C starts recording
    13:31:26  A's finalize done → A's cleanup runs
    13:31:26  B's finalize → TypeError: sequence item 2: expected str
              instance, NoneType found → "preserving temps: None + …"
    13:31:26  "Session log copied to …session_<C>.log"   ← 28 min early
    13:59:42  [stop] meeting C → "[stop] complete in 0.1s"  ← no finalize

A kept its audio. B was lost with a crash. C, 29 minutes long, was lost
with NO error at all — the stop reported success in a tenth of a second.

WHY
---
Each recording's temp paths and session log live in fields on the ONE
``RecordingService`` instance. ``stop_recording`` waits in
``finalize_slot`` behind the previous meeting's finalize, and in that
window two other code paths write the same fields:

  * the NEXT meeting's ``start_recording`` sets ``_wav_temp_path`` and
    ``_session_log_handler`` to its own;
  * the PREVIOUS meeting's stop, finishing, deletes ``_wav_temp_path``,
    sets it to ``None``, and closes ``_session_log_handler``.

The stop path copied its loopback path into a local before the wait but
read its mic path from ``self`` after it — which is why B's log line
reads "None + _loopback_B.wav": the half it had copied was right, the
half it re-read was gone. And A's cleanup, running while C recorded,
deleted C's mic path from the field, so C's stop later found nothing to
finalize and returned silently.

The 2026-06-15 data-loss fix already solved exactly this for
``self._session`` by copying it into a local at the top of stop. This
extends the same rule to every per-recording field: copy once at the
top, use only the copy, and only clear a shared field if it is still
this recording's.

HOW THESE TESTS REPRODUCE IT
----------------------------
Deterministically, with no threads: ``finalize_slot`` and the finalize
subprocess are replaced with stand-ins that perform the other meeting's
writes at exactly the moment they happened in the field. The real
``stop_recording`` runs end to end.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests._app_import import _stub_optional_modules

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

    def stop(self):
        pass

    def get_capture_stats(self):
        return {"mic_sr": 16000, "loopback_sr": 16000,
                "mic_samples": 16000 * 60, "loopback_samples": 0,
                "mic_overflows": 0, "loopback_overflows": 0}


def _wav(path: Path) -> str:
    """A real file on disk. Content is irrelevant — finalize is faked —
    but existence is what the cleanup and recovery paths key on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + b"\0" * 2048)
    return str(path)


def _log_handler(tmp_path: Path, sid: str):
    temp = tmp_path / "capture" / f"session_{sid}.log"
    temp.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(temp), encoding="utf-8")
    logging.getLogger().addHandler(handler)
    final = tmp_path / "recordings" / f"session_{sid}.log"
    return handler, str(temp), str(final)


def _arm(svc, tmp_path, sid, *, conference_room_mode=False):
    """Put ``svc`` in the state a live recording of ``sid`` leaves it
    in: its own session, mic temp, loopback temp and session log."""
    session = rs.Session(session_id=sid)
    session.started_at = datetime.now() - timedelta(minutes=1)
    svc._session = session
    svc._recording = True
    svc._capture = _Capture()
    svc._chunk_count = 10
    svc._wav_writer = None
    svc._live_transcriber = None
    svc._conference_room_mode = conference_room_mode
    svc._wav_temp_path = _wav(tmp_path / "capture" / f"_recording_{sid}.wav")
    svc._loopback_temp_path = _wav(
        tmp_path / "capture" / f"_loopback_{sid}.wav")
    handler, temp, final = _log_handler(tmp_path, sid)
    svc._session_log_handler = handler
    svc._session_log_temp = temp
    svc._session_log_final = final
    return session


def _next_meeting_starts(svc, tmp_path, sid):
    """What ``start_recording`` does to the shared fields — the writes
    that land while an earlier stop is still in flight."""
    svc._wav_temp_path = _wav(tmp_path / "capture" / f"_recording_{sid}.wav")
    svc._loopback_temp_path = _wav(
        tmp_path / "capture" / f"_loopback_{sid}.wav")
    handler, temp, final = _log_handler(tmp_path, sid)
    svc._session_log_handler = handler
    svc._session_log_temp = temp
    svc._session_log_final = final
    svc._conference_room_mode = True
    return handler


@pytest.fixture
def svc(tmp_path):
    service = rs.RecordingService(settings=_Settings(tmp_path / "recordings"))
    yield service
    # Never leave a test's FileHandlers on the root logger.
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and str(tmp_path) in str(
                getattr(h, "baseFilename", "")):
            root.removeHandler(h)
            h.close()


def _succeeding_finalize(calls, *, during=None):
    """A finalize that records what it was asked to merge, optionally
    lets another meeting act mid-run, and produces the output file so
    the stop's cleanup branch runs."""
    def _fake(**kwargs):
        calls.append(kwargs)
        if during is not None:
            during()
        Path(kwargs["output_wav_path"]).parent.mkdir(
            parents=True, exist_ok=True)
        Path(kwargs["output_wav_path"]).write_bytes(b"RIFF" + b"\0" * 64)
        return (60.0, False, None)
    return staticmethod(_fake)


@contextlib.contextmanager
def _slot_free(on_queued=None):
    """``finalize_slot`` when nothing else is finalizing: granted at
    once, ``on_queued`` never called. Same signature as the real gate —
    ``contextlib.nullcontext`` does not accept ``on_queued`` and would
    fail every stop for a reason that has nothing to do with the race."""
    yield


def _slot_that_waits(*, while_waiting):
    """``finalize_slot`` for a stop that is queued: report the queue,
    let the other meetings do what they did in the field while this
    stop waits, then grant the slot."""
    @contextlib.contextmanager
    def _slot(on_queued=None):
        if on_queued is not None:
            on_queued()
        while_waiting()
        yield
    return _slot


# ── Meeting B: the queued stop that crashed ─────────────────────────

@pytest.mark.parametrize("what_happened_while_waiting", [
    "previous_meeting_cleaned_up",   # A's stop nulled the field (field log)
    "next_meeting_started",          # C's start overwrote it
])
def test_a_queued_finalize_merges_its_own_mic_audio(
        svc, tmp_path, monkeypatch, what_happened_while_waiting):
    """B waited ~58 s for A's finalize. While it waited the shared mic
    path became None (A's cleanup) or C's path (C's start). B must still
    merge B's microphone — never nothing, and never another meeting's."""
    _arm(svc, tmp_path, "B")
    b_mic = svc._wav_temp_path

    def _meanwhile():
        if what_happened_while_waiting == "previous_meeting_cleaned_up":
            svc._wav_temp_path = None
        else:
            _next_meeting_starts(svc, tmp_path, "C")

    calls = []
    monkeypatch.setattr(rs, "finalize_slot",
                        _slot_that_waits(while_waiting=_meanwhile))
    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        _succeeding_finalize(calls))

    session = svc.stop_recording()

    assert calls, "finalize never ran"
    assert calls[0]["mic_wav_path"] == b_mic, (
        f"meeting B was finalized with {calls[0]['mic_wav_path']!r} instead "
        f"of its own microphone file")
    assert session.session_id == "B"
    assert Path(session.audio_path).exists()


def test_a_queued_finalize_keeps_its_own_conference_room_setting(
        svc, tmp_path, monkeypatch):
    """Conference-room mode changes how channel attribution treats the
    mic. It is a property of the meeting being finalized, not of
    whichever meeting happens to be recording now."""
    _arm(svc, tmp_path, "B", conference_room_mode=False)
    calls = []
    monkeypatch.setattr(
        rs, "finalize_slot",
        _slot_that_waits(
            while_waiting=lambda: _next_meeting_starts(svc, tmp_path, "C")))
    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        _succeeding_finalize(calls))

    svc.stop_recording()

    assert calls[0]["conference_room_mode"] is False


# ── Meeting C: silently lost by A's cleanup ─────────────────────────

def _stop_while_next_meeting_starts(svc, tmp_path, monkeypatch):
    """Stop A; while A's finalize runs, meeting C starts. Returns C's
    mic path and C's session-log handler."""
    _arm(svc, tmp_path, "A")
    started = {}

    def _c_starts():
        started["handler"] = _next_meeting_starts(svc, tmp_path, "C")
        started["mic"] = svc._wav_temp_path

    calls = []
    monkeypatch.setattr(rs, "finalize_slot", _slot_free)
    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        _succeeding_finalize(calls, during=_c_starts))
    svc.stop_recording()
    return started["mic"], started["handler"]


def test_a_finished_stop_does_not_delete_the_next_meetings_audio(
        svc, tmp_path, monkeypatch):
    """A's merge succeeded, so its cleanup deletes temps. It deleted
    ``self._wav_temp_path`` — which by then was C's file, open and still
    being written. On macOS that unlink succeeds and C's audio is gone
    the moment its writer closes."""
    c_mic, _ = _stop_while_next_meeting_starts(svc, tmp_path, monkeypatch)
    assert Path(c_mic).exists(), "A's cleanup deleted C's live recording"


def test_a_finished_stop_still_cleans_up_its_own_temps(
        svc, tmp_path, monkeypatch):
    """The other half: fixing the overlap must not leave A's own temps
    behind to be 'recovered' as a duplicate on next launch."""
    a_mic = str(tmp_path / "capture" / "_recording_A.wav")
    a_lb = str(tmp_path / "capture" / "_loopback_A.wav")
    _stop_while_next_meeting_starts(svc, tmp_path, monkeypatch)
    assert not Path(a_mic).exists()
    assert not Path(a_lb).exists()


def test_a_finished_stop_leaves_the_next_meeting_recordable(
        svc, tmp_path, monkeypatch):
    """The silent loss. A's stop ended with ``self._wav_temp_path =
    None`` while C was recording, so C's stop found no mic file, skipped
    finalize entirely, and reported "[stop] complete in 0.1s"."""
    c_mic, _ = _stop_while_next_meeting_starts(svc, tmp_path, monkeypatch)
    assert svc._wav_temp_path == c_mic, (
        "A's stop cleared the mic path of a meeting that is still recording")


def test_a_finished_stop_does_not_close_the_next_meetings_log(
        svc, tmp_path, monkeypatch):
    """Field log: 'Session log copied to …session_<C>.log' at 13:31:26,
    28 minutes before C ended — A's stop closed C's log."""
    _, c_handler = _stop_while_next_meeting_starts(svc, tmp_path, monkeypatch)
    assert svc._session_log_handler is c_handler
    assert c_handler in logging.getLogger().handlers
    assert not Path(tmp_path / "recordings" / "session_C.log").exists(), (
        "C's log was copied out while C was still recording")


def test_a_finished_stop_closes_and_keeps_its_own_log(
        svc, tmp_path, monkeypatch):
    """A's own handler must not be orphaned on the root logger — left
    there it keeps writing every line of every later meeting into A's
    file — and A's log must still reach the recordings folder."""
    _arm(svc, tmp_path, "A")
    a_handler = svc._session_log_handler
    monkeypatch.setattr(rs, "finalize_slot", _slot_free)
    monkeypatch.setattr(
        rs.RecordingService, "_run_finalize_subprocess",
        _succeeding_finalize(
            [], during=lambda: _next_meeting_starts(svc, tmp_path, "C")))

    svc.stop_recording()

    assert a_handler not in logging.getLogger().handlers
    assert Path(tmp_path / "recordings" / "session_A.log").exists()


# ── No overlap: nothing changes ─────────────────────────────────────

def test_a_lone_stop_still_clears_every_field(svc, tmp_path, monkeypatch):
    """With no other meeting in flight, a stop leaves the service idle,
    exactly as before."""
    _arm(svc, tmp_path, "A")
    monkeypatch.setattr(rs, "finalize_slot", _slot_free)
    monkeypatch.setattr(rs.RecordingService, "_run_finalize_subprocess",
                        _succeeding_finalize([]))

    session = svc.stop_recording()

    assert Path(session.audio_path).exists()
    assert svc._wav_temp_path is None
    assert svc._session_log_handler is None


# ── A missing file is reported in words, not as a TypeError ─────────

def test_finalize_without_a_mic_file_says_so(svc, tmp_path):
    """The user-facing message on 2026-09-15 was 'sequence item 2:
    expected str instance, NoneType found' — raised by a LOG LINE. It
    must name what is missing instead."""
    with pytest.raises(RuntimeError) as err:
        rs.RecordingService._run_finalize_subprocess(
            mic_wav_path=None, loopback_wav_path=None,
            output_wav_path=str(tmp_path / "out.wav"), target_sr=16000,
            loopback_start_offset_s=None, echo_cancellation_enabled=False,
            channel_attribution_enabled=False, conference_room_mode=False)
    assert "microphone recording" in str(err.value)
    assert "sequence item" not in str(err.value)
