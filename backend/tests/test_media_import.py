"""
Importing a video: its sound track becomes a WAV every stage can read.

The videos here are real files, encoded by the same decoder library the
app uses (PyAV, which faster-whisper depends on): an H.264-free MPEG-4
video stream plus an AAC sound track in an .mp4, the shape of a Teams or
Zoom download. Nothing about the container is invented.

The bug this covers: an imported .mp4 was copied in as-is, and the
processing copy renamed it ``<id>.wav`` without converting it. Whisper
decoded it; libsndfile — what the diarizer and voice fingerprinting read
through — could not, so processing died at the speaker stage.
"""

from __future__ import annotations

import datetime as dt
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

av = pytest.importorskip("av")
sf = pytest.importorskip("soundfile")

from core import media_import  # noqa: E402


def _make_video(path: Path, seconds: float = 3.0, *, audio: bool = True,
                video: bool = True, freq: float = 440.0,
                creation_time: str = "") -> Path:
    """Write a real .mp4: MPEG-4 video and/or a 48 kHz stereo AAC tone."""
    out = av.open(str(path), "w")
    if creation_time:
        out.metadata["creation_time"] = creation_time
    vstream = astream = None
    if video:
        vstream = out.add_stream("mpeg4", rate=10)
        vstream.width, vstream.height = 64, 48
        vstream.pix_fmt = "yuv420p"
    if audio:
        astream = out.add_stream("aac", rate=48000)
        astream.layout = "stereo"
    if vstream is not None:
        for i in range(int(seconds * 10)):
            img = np.full((48, 64, 3), (i * 7) % 255, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = i
            frame.time_base = Fraction(1, 10)
            for pkt in vstream.encode(frame):
                out.mux(pkt)
        for pkt in vstream.encode(None):
            out.mux(pkt)
    if astream is not None:
        rate, block = 48000, 1024
        n = int(seconds * rate)
        t = np.arange(n) / rate
        tone = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        stereo = np.stack([tone, tone])
        for start in range(0, n, block):
            chunk = np.ascontiguousarray(stereo[:, start:start + block])
            frame = av.AudioFrame.from_ndarray(chunk, format="fltp",
                                               layout="stereo")
            frame.sample_rate = rate
            frame.pts = start
            frame.time_base = Fraction(1, rate)
            for pkt in astream.encode(frame):
                out.mux(pkt)
        for pkt in astream.encode(None):
            out.mux(pkt)
    out.close()
    return path


def _dominant_hz(data: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(data))
    return float(np.fft.rfftfreq(len(data), 1 / rate)[int(np.argmax(spectrum))])


# ── extraction ───────────────────────────────────────────────────────


def test_the_bug_libsndfile_cannot_read_an_mp4(tmp_path):
    """What processing tripped over: the diarizer's reader on the raw
    video. If this ever passes, the reason for extracting is gone."""
    mp4 = _make_video(tmp_path / "meeting.mp4")
    with pytest.raises(Exception):
        sf.read(str(mp4))


def test_a_video_becomes_a_16k_mono_wav_the_pipeline_can_read(tmp_path):
    mp4 = _make_video(tmp_path / "meeting.mp4", seconds=3.0, freq=440.0)
    wav = tmp_path / "out.wav"

    result = media_import.extract_audio(mp4, wav)

    data, rate = sf.read(str(wav))
    assert rate == 16000
    assert data.ndim == 1                       # mono
    assert abs(result.duration_s - 3.0) < 0.1
    assert abs(len(data) / rate - result.duration_s) < 0.01
    # It is the sound track, not silence or noise.
    assert abs(_dominant_hz(data, rate) - 440.0) < 5.0
    assert not (tmp_path / "out.wav.part").exists()


def test_progress_is_reported_and_finishes_at_one(tmp_path):
    mp4 = _make_video(tmp_path / "meeting.mp4", seconds=2.0)
    seen = []
    media_import.extract_audio(mp4, tmp_path / "out.wav",
                               progress=seen.append)
    assert seen and seen[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in seen)


def test_a_video_with_no_sound_says_so_and_leaves_nothing_behind(tmp_path):
    mp4 = _make_video(tmp_path / "silent.mp4", audio=False)
    wav = tmp_path / "out.wav"
    with pytest.raises(media_import.NoAudioTrack, match="no sound track"):
        media_import.extract_audio(mp4, wav)
    assert not wav.exists()
    assert not (tmp_path / "out.wav.part").exists()


def test_a_file_that_is_not_media_is_a_clear_error(tmp_path):
    fake = tmp_path / "notes.mp4"
    fake.write_bytes(b"this is a text file with the wrong extension" * 50)
    with pytest.raises(ValueError, match="Couldn't open notes.mp4"):
        media_import.extract_audio(fake, tmp_path / "out.wav")
    assert not (tmp_path / "out.wav").exists()
    assert not (tmp_path / "out.wav.part").exists()


def test_an_audio_only_file_is_converted_too(tmp_path):
    m4a = _make_video(tmp_path / "call.m4a", video=False, freq=300.0)
    result = media_import.extract_audio(m4a, tmp_path / "out.wav")
    data, rate = sf.read(str(tmp_path / "out.wav"))
    assert abs(result.duration_s - 3.0) < 0.1
    assert abs(_dominant_hz(data, rate) - 300.0) < 5.0


def test_the_recording_time_inside_the_file_is_used(tmp_path):
    mp4 = _make_video(tmp_path / "meeting.mp4",
                      creation_time="2026-09-30T14:05:00.000000Z")
    result = media_import.extract_audio(mp4, tmp_path / "out.wav")
    expected = dt.datetime(2026, 9, 30, 14, 5, tzinfo=dt.timezone.utc) \
        .astimezone().replace(tzinfo=None)
    assert result.recorded_at == expected


@pytest.mark.parametrize("value,expected", [
    ("", None),
    (None, None),
    ("not a date", None),
    ("1904-01-01T00:00:00.000000Z", None),   # MP4's unset epoch
    ("1970-01-01T00:00:00Z", None),
])
def test_creation_time_that_is_not_a_meeting_date_is_ignored(value, expected):
    assert media_import.parse_creation_time(value) is expected


def test_which_files_are_converted():
    assert media_import.needs_extraction("a.mp4")
    assert media_import.needs_extraction("a.MOV")
    assert media_import.needs_extraction("a.m4a")
    assert media_import.needs_extraction("a.mp3")
    assert not media_import.needs_extraction("a.wav")
    assert not media_import.needs_extraction("a.WAV")
    assert media_import.is_supported("a.webm")
    assert not media_import.is_supported("a.docx")


# ── the import itself ────────────────────────────────────────────────


def _svc(tmp_path):
    from services.session_service import SessionService
    return SessionService(str(tmp_path / "recordings"), index_enabled=False)


def test_importing_a_video_makes_a_session_with_a_real_wav(tmp_path):
    src = _make_video(tmp_path / "Weekly sync.mp4", seconds=3.0)
    before = src.read_bytes()
    svc = _svc(tmp_path)

    session = svc.import_from_file(
        str(src), display_name="Acme weekly", client="Acme",
        project="Phase 2", template="General")

    audio = Path(session.audio_path)
    assert audio.suffix == ".wav"
    assert audio.parent == tmp_path / "recordings"
    data, rate = sf.read(str(audio))            # what the diarizer does
    assert rate == 16000 and len(data) > 0
    # The list shows the real length, not 0:00.
    length = (session.ended_at - session.started_at).total_seconds()
    assert abs(length - 3.0) < 0.1
    assert session.display_name == "Acme weekly"
    assert (session.client, session.project) == ("Acme", "Phase 2")
    # The original is untouched, and the video itself is not copied.
    assert src.read_bytes() == before
    assert not list((tmp_path / "recordings").glob("*.mp4"))
    # And it round-trips like any other session.
    loaded = svc.load_full(session.session_id)
    assert loaded.audio_path == session.audio_path
    assert loaded.client == "Acme"


def test_the_name_defaults_to_the_filename(tmp_path):
    src = _make_video(tmp_path / "Globex kickoff.mp4", seconds=1.0)
    session = _svc(tmp_path).import_from_file(str(src))
    assert session.display_name == "Globex kickoff"


def test_a_wav_is_copied_and_keeps_its_length(tmp_path):
    src = tmp_path / "call.wav"
    sf.write(str(src), np.zeros(16000 * 2, dtype=np.float32), 16000)
    session = _svc(tmp_path).import_from_file(str(src))
    assert Path(session.audio_path).read_bytes() == src.read_bytes()
    length = (session.ended_at - session.started_at).total_seconds()
    assert abs(length - 2.0) < 0.01


def test_a_failed_extraction_leaves_no_session(tmp_path):
    src = _make_video(tmp_path / "silent.mp4", audio=False)
    svc = _svc(tmp_path)
    with pytest.raises(media_import.NoAudioTrack):
        svc.import_from_file(str(src))
    assert not list((tmp_path / "recordings").glob("session_*"))


def test_unsupported_and_missing_files_are_refused(tmp_path):
    svc = _svc(tmp_path)
    doc = tmp_path / "agenda.docx"
    doc.write_bytes(b"x")
    with pytest.raises(ValueError, match="Unsupported file type"):
        svc.import_from_file(str(doc))
    with pytest.raises(FileNotFoundError):
        svc.import_from_file(str(tmp_path / "nope.mp4"))
    with pytest.raises(FileNotFoundError):
        svc.import_from_file(str(tmp_path))      # a folder is not a file


# ── the endpoint ─────────────────────────────────────────────────────
#
# Called as a coroutine with server.svc pointed at a real SessionService,
# the pattern test_session_file_containment.py uses.


def _server(monkeypatch, session_svc, started):
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    sys.modules.setdefault("dotenv", MagicMock())
    from _app_import import import_app
    import_app()
    import server
    monkeypatch.setattr(server.svc, "settings", SimpleNamespace())
    monkeypatch.setattr(server.svc, "_services_ready", True)
    monkeypatch.setattr(server.svc, "session_svc", session_svc)
    monkeypatch.setattr(server, "_start_background_process",
                        lambda s: started.append(s.session_id) or True)
    return server


def test_import_with_process_starts_the_pipeline(tmp_path, monkeypatch):
    import asyncio
    src = _make_video(tmp_path / "meeting.mp4", seconds=1.0)
    started: list = []
    server = _server(monkeypatch, _svc(tmp_path), started)

    handed_off: list = []

    async def _after(sid, video, process):
        handed_off.append((sid, video, process))
    monkeypatch.setattr(server, "_after_import", _after)

    async def _run():
        resp = await server.import_session(server.ImportSessionRequest(
            file_path=str(src), display_name="Initech review",
            client="Initech", template="General", process=True))
        await asyncio.sleep(0)          # let the scheduled task run
        return resp
    resp = asyncio.run(_run())

    assert resp["ok"] and resp["processing"] is True
    # Slides first, then processing, in the background half.
    assert handed_off == [(resp["session_id"], str(src), True)]
    assert abs(resp["duration_s"] - 1.0) < 0.1


def test_import_without_process_only_imports(tmp_path, monkeypatch):
    import asyncio
    src = _make_video(tmp_path / "meeting.mp4", seconds=1.0)
    started: list = []
    server = _server(monkeypatch, _svc(tmp_path), started)
    resp = asyncio.run(server.import_session(
        server.ImportSessionRequest(file_path=str(src))))
    assert resp["ok"] and resp["processing"] is False
    assert started == []


def test_a_video_with_no_sound_is_a_400_not_a_500(tmp_path, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    src = _make_video(tmp_path / "silent.mp4", audio=False)
    started: list = []
    server = _server(monkeypatch, _svc(tmp_path), started)
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.import_session(server.ImportSessionRequest(
            file_path=str(src), process=True)))
    assert e.value.status_code == 400
    assert "no sound track" in e.value.detail
    assert started == []


def _deck_with_sound(path: Path, slides: int = 3, slide_s: int = 12) -> Path:
    """A shared deck with a sound track: the import that has slides."""
    out = av.open(str(path), "w")
    v = out.add_stream("libx264", rate=5)
    v.width, v.height, v.pix_fmt = 320, 180, "yuv420p"
    v.options = {"g": "10", "keyint_min": "10", "sc_threshold": "0"}
    a = out.add_stream("aac", rate=48000)
    a.layout = "mono"
    for i in range(slides * slide_s * 5):
        k = i // (slide_s * 5)
        img = np.full((180, 320, 3), 250, np.uint8)
        img[10:30, 20:200] = (30, 60, 140)
        img[50 + k * 30:70 + k * 30, 20:300] = 40
        f = av.VideoFrame.from_ndarray(img, format="rgb24")
        f.pts, f.time_base = i, Fraction(1, 5)
        for pkt in v.encode(f):
            out.mux(pkt)
    for pkt in v.encode(None):
        out.mux(pkt)
    n = 48000 * slides * slide_s
    tone = (0.2 * np.sin(np.arange(n) * 2 * np.pi * 200 / 48000)).astype(np.float32)
    for s0 in range(0, n, 1024):
        f = av.AudioFrame.from_ndarray(tone[None, s0:s0 + 1024], format="fltp",
                                       layout="mono")
        f.sample_rate, f.pts, f.time_base = 48000, s0, Fraction(1, 48000)
        for pkt in a.encode(f):
            out.mux(pkt)
    for pkt in a.encode(None):
        out.mux(pkt)
    out.close()
    return path


def test_a_video_import_attaches_its_slides_before_processing(
        tmp_path, monkeypatch):
    import asyncio
    src = _deck_with_sound(tmp_path / "Hooli demo.mp4")
    svc = _svc(tmp_path)
    started: list = []
    seen_at_start: list = []
    server = _server(monkeypatch, svc, started)

    def _start(session):
        # What processing will see when it starts.
        seen_at_start.append(len(svc.load_full(session.session_id).screenshots))
        started.append(session.session_id)
        return True
    monkeypatch.setattr(server, "_start_background_process", _start)

    resp = asyncio.run(server.import_session(server.ImportSessionRequest(
        file_path=str(src), client="Hooli", process=True)))
    sid = resp["session_id"]
    assert resp["slides"] is True
    # The endpoint scheduled the background half; run it to completion.
    asyncio.run(server._after_import(sid, str(src), True))

    session = svc.load_full(sid)
    assert len(session.screenshots) == 3
    for p in session.screenshots:
        assert Path(p).is_file()
        assert Path(p).parent == (tmp_path / "recordings" / "screenshots"
                                  / f"session_{sid}")
    assert started == [sid] and seen_at_start == [3]
    assert session.client == "Hooli"


def test_slides_failing_never_stops_processing(tmp_path, monkeypatch):
    import asyncio
    src = _deck_with_sound(tmp_path / "meeting.mp4", slides=1)
    svc = _svc(tmp_path)
    started: list = []
    server = _server(monkeypatch, svc, started)
    session = svc.import_from_file(str(src))
    import core.video_slides as video_slides

    def _boom(*a, **k):
        raise RuntimeError("decoder fell over")
    monkeypatch.setattr(video_slides, "extract_slides", _boom)
    asyncio.run(server._after_import(session.session_id, str(src), True))
    assert started == [session.session_id]
    assert svc.load_full(session.session_id).screenshots == []
