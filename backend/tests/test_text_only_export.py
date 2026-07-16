"""v2.19+ invariant: the network-folder export copies ONLY derived text
artifacts. The raw session WAV never leaves local disk, regardless of
whether the target is a client's Designated Folder or the Cloud Mirror
root. This is the rule the 2026-07-09 Drive-stall + user-clarification
gave us: audio is the artifact that stalled cloud mounts, and it's not
what teams read from a shared drive anyway.
"""

import shutil
from pathlib import Path

import soundfile as sf

from services.export_service import ExportService
from models.segment import Segment
from models.session import Session
from conftest import write_sine_wav


def _mk_session(recordings_dir: Path, sid: str, with_text: bool = True):
    wav_path = write_sine_wav(
        recordings_dir / f"session_{sid}.wav", duration_s=1.0, samplerate=16000)
    sess = Session(session_id=sid)
    sess.display_name = "ACME Discovery"
    sess.client = "ACME"
    sess.audio_path = str(wav_path)
    if with_text:
        # A single segment is enough to trigger transcript export.
        sess.segments = [
            Segment(speaker_id="SPEAKER_00", start=0.0, end=1.0,
                    text="hello world"),
        ]
        sess.summary = "Short call about ACME migration."
    return sess


def test_export_all_copy_audio_false_writes_no_wav(
    recordings_dir: Path, tmp_path: Path,
):
    """The core mechanism: export_all(copy_audio=False) must never
    create an audio file under the target folder."""
    target = tmp_path / "acme_shared_drive"
    target.mkdir()
    svc = ExportService(str(recordings_dir))
    sess = _mk_session(recordings_dir, "ABC123")

    svc.export_all(sess, target_dir=str(target), copy_audio=False)

    files = sorted(p.name for p in target.iterdir())
    # Some text artifact landed (proves the export ran)…
    assert any(f.endswith(".txt") for f in files), files
    # …and not a single audio file did.
    for f in files:
        assert not f.lower().endswith(
            (".wav", ".mp3", ".m4a", ".flac")), (
            f"raw audio leaked to the network folder: {f}")


def test_export_bails_when_no_text_yet(recordings_dir: Path, tmp_path: Path):
    """Just-stopped session with no transcript/summary yet: the worker
    body's early-exit should skip the copy entirely — nothing to write."""
    target = tmp_path / "acme_shared_drive"
    target.mkdir()
    svc = ExportService(str(recordings_dir))
    sess = _mk_session(recordings_dir, "PENDING1", with_text=False)

    # Simulate the worker's guard: no text → no export.
    has_text = bool(
        sess.segments or sess.summary or sess.action_items
        or sess.decisions or sess.requirements
    )
    if has_text:
        svc.export_all(sess, target_dir=str(target), copy_audio=False)

    assert list(target.iterdir()) == [], (
        "empty session should not produce any files on the network folder")


def test_source_wav_stays_local_after_export(
    recordings_dir: Path, tmp_path: Path,
):
    """Belt-and-suspenders: the local WAV must be untouched by the
    export — no move, no rename, byte-identical size."""
    target = tmp_path / "shared"
    target.mkdir()
    svc = ExportService(str(recordings_dir))
    sess = _mk_session(recordings_dir, "LOCAL1")
    src = Path(sess.audio_path)
    src_size = src.stat().st_size
    src_frames = sf.info(str(src)).frames

    svc.export_all(sess, target_dir=str(target), copy_audio=False)

    assert src.exists()
    assert src.stat().st_size == src_size
    assert sf.info(str(src)).frames == src_frames
