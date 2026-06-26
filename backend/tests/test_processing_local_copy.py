"""
``_copy_audio_to_local_for_processing`` is the v2.12.0 read-side fix
for cloud-sync filter driver contention crashes during transcribe /
diarize. These tests pin the helper's contract:

  - Copy succeeds for a real-on-disk WAV and returns the local path.
  - Subsequent processing reads from %TEMP%, not the original path.
  - A path that's already in the processing temp dir is returned
    unchanged (idempotency — re-process doesn't re-copy needlessly).
  - Missing / empty / truncated source raises RuntimeError with a
    user-facing message — same surface as the older _ensure_audio_
    available so downstream callers don't have to special-case.

The crash class these defend against (libsndfile / scipy native crash
mid-read on a Google-Drive-Stream-backed path) can't be reproduced in
a CI sandbox, so the tests assert the LOGICAL guarantee — bytes come
from a local-only path — not the absence of a hypothetical native
crash. That logical guarantee is what makes the crash class impossible.
"""
import os
import shutil
from pathlib import Path

import pytest
import soundfile as sf

from utils.audio_utils import (
    copy_audio_to_local_for_processing as _copy_audio_to_local_for_processing,
    purge_processing_temp as _purge_processing_temp,
    _processing_temp_dir,
)
from conftest import write_sine_wav


def test_copy_returns_a_local_temp_path(recordings_dir: Path):
    """Happy path: source WAV in recordings_dir, copy lands in the
    local processing scratch."""
    src = write_sine_wav(
        recordings_dir / "session_localcopy1.wav",
        duration_s=1.0, samplerate=48000,
    )
    out = _copy_audio_to_local_for_processing(str(src), "localcopy1")
    out_path = Path(out)
    assert out_path.exists()
    # MUST be under %TEMP%/meeting_recorder_processing — the whole
    # point of this helper. If the path ever lands back under
    # recordings_dir the cloud-sync-contention immunity is lost.
    assert out_path.parent == _processing_temp_dir()
    # Same audio content
    src_info = sf.info(str(src))
    out_info = sf.info(out)
    assert src_info.frames == out_info.frames
    _purge_processing_temp("localcopy1")


def test_copy_is_idempotent_when_source_is_already_in_processing_temp(recordings_dir: Path):
    """If the caller passes a path that's already under the processing
    temp dir, the helper returns it unchanged. Prevents a no-op copy
    (and a potential rare race where a re-call mid-process overwrites
    a copy the previous call is still using)."""
    # Plant a WAV directly in the processing temp dir.
    proc_dir = _processing_temp_dir()
    local_src = proc_dir / "already_local.wav"
    write_sine_wav(local_src, duration_s=0.5, samplerate=48000)

    out = _copy_audio_to_local_for_processing(str(local_src), "already_local")
    assert Path(out).resolve() == local_src.resolve()
    # Cleanup
    try:
        local_src.unlink()
    except OSError:
        pass


def test_copy_raises_on_missing_source(tmp_path: Path):
    """User-facing error, NOT a raw FileNotFoundError. The whole point
    of wrapping this is so the UI shows an actionable message."""
    missing = tmp_path / "does_not_exist.wav"
    with pytest.raises(RuntimeError, match="missing|moved|synced"):
        _copy_audio_to_local_for_processing(str(missing), "missing1")


def test_copy_raises_on_empty_source(tmp_path: Path):
    """A < 1 KB source is the "header-only WAV from a recording that
    captured no audio" failure mode. Reject it with a clear message
    BEFORE the ML pipeline opens it (libsndfile crashes on these)."""
    empty = tmp_path / "_recording_empty.wav"
    empty.write_bytes(b"RIFF" + b"\x00" * 40)  # 44 byte fake header
    with pytest.raises(RuntimeError, match="empty|truncated"):
        _copy_audio_to_local_for_processing(str(empty), "empty1")


def test_purge_removes_the_working_copy(recordings_dir: Path):
    """The finally branch in process_session calls _purge_processing_
    temp on every exit (success or failure). Verify it actually deletes
    so disk doesn't grow over time."""
    src = write_sine_wav(
        recordings_dir / "session_purge1.wav",
        duration_s=0.5, samplerate=48000,
    )
    out = _copy_audio_to_local_for_processing(str(src), "purge1")
    assert Path(out).exists()

    _purge_processing_temp("purge1")
    assert not Path(out).exists()

    # Idempotent — calling purge again on an already-deleted path is
    # safe and silent (no exception).
    _purge_processing_temp("purge1")
