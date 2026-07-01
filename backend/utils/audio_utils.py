"""Audio file helpers: WAV writing, resampling, mixing."""

import tempfile
from math import gcd
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from utils.logger import get_logger

logger = get_logger(__name__)

TARGET_SAMPLE_RATE = 16000

# Streaming block size. Large enough that resample_poly edge artifacts are
# negligible for speech audio, small enough that peak RAM stays bounded
# regardless of recording length.
#   10 s @ 192 kHz float32 ≈ 7.7 MB per block.
STREAM_BLOCK_SECONDS = 10.0


def _processing_temp_dir() -> Path:
    """Local-only scratch dir for the WAV the ML pipeline reads from.

    READ-SIDE CLOUD-CONTENTION FIX (v2.12.0, field repro 2026-06-26):
    the recorded WAV lives in ``recordings_dir`` which is often on
    Google Drive Stream / OneDrive / iCloud. Even AFTER the
    ``_ensure_audio_available`` hydrate-by-read pass, the cloud sync
    filter driver can intermittently return ENOENT or partial reads
    while it negotiates the file's state — enough to crash libsndfile
    / faster-whisper at the native level on a long recording. Two
    backend segfaults today (rust.log 20:01:39 + 20:33:45) hit exactly
    this; same shape as the v2.11.1 finalize fix, just on the read
    side now.

    Copying the WAV ONCE to a local-only path and then reading from
    THAT for transcribe + diarize + speaker embeddings closes the
    contention class. The local copy is a working scratch file — its
    presence does not affect the canonical session WAV at
    ``audio_path``. ``_purge_processing_temp`` deletes the scratch on
    every exit so disk doesn't grow over time."""
    d = Path(tempfile.gettempdir()) / "meeting_recorder_processing"
    d.mkdir(parents=True, exist_ok=True)
    return d


def copy_audio_to_local_for_processing(audio_path: str, session_id: str) -> str:
    """Stream-copy the session WAV to ``%TEMP%/meeting_recorder_
    processing/<session_id>.wav``. Returns the local path.

    Idempotent: if ``audio_path`` is ALREADY in
    ``_processing_temp_dir()``, returns it unchanged.

    Uses 8 MB chunks with per-chunk retries (3 attempts, 0.5s
    backoff) so a transient sync-filter stall on one read doesn't
    fail the whole copy. A genuine missing-file / corrupt-WAV
    condition raises RuntimeError with a user-facing message."""
    import time as _t
    src = Path(audio_path)
    if not src.exists():
        raise RuntimeError(
            "The audio file for this recording is missing — it may have "
            "been moved, deleted, or not yet synced down from the cloud.")
    try:
        size = src.stat().st_size
    except OSError as e:
        raise RuntimeError(
            f"The audio file couldn't be accessed: {e}") from e
    if size < 1024:
        raise RuntimeError(
            "The audio file is empty or truncated — this recording didn't "
            "capture usable audio and can't be processed.")

    # Idempotency check: if the source is already under the processing
    # temp dir, return as-is. is_relative_to is 3.9+; on older Pythons
    # or non-relative paths the comparison just falls through.
    try:
        if src.resolve().is_relative_to(_processing_temp_dir().resolve()):
            return str(src.resolve())
    except (AttributeError, ValueError):
        pass

    dst = _processing_temp_dir() / f"{session_id}.wav"
    if dst.exists():
        try:
            dst.unlink()
        except OSError as e:
            logger.warning(f"could not remove stale {dst}: {e}")

    logger.info(
        f"Copying {src.name} ({size/1024/1024:.1f} MB) to local processing "
        f"scratch at {dst}")
    chunk = 8 * 1024 * 1024
    copied = 0
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                last_err: Optional[OSError] = None
                for attempt in range(3):
                    try:
                        buf = fsrc.read(chunk)
                        break
                    except OSError as e:
                        last_err = e
                        if attempt == 2:
                            raise
                        _t.sleep(0.5 * (attempt + 1))
                else:  # pragma: no cover
                    raise last_err  # type: ignore[misc]
                if not buf:
                    break
                fdst.write(buf)
                copied += len(buf)
    except OSError as e:
        try:
            if dst.exists():
                dst.unlink()
        except OSError:
            pass
        raise RuntimeError(
            "The audio file couldn't be copied to local scratch — it may "
            "still be syncing from the cloud. Wait a moment and process "
            "the session again."
        ) from e

    if copied != size:
        try:
            dst.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"Copy was truncated ({copied}/{size} bytes); cloud sync may "
            f"be still mid-flight. Wait a moment and process again.")

    logger.info(
        f"Working copy ready: {dst} ({copied/1024/1024:.1f} MB)")
    return str(dst)


def purge_processing_temp(session_id: str) -> None:
    """Delete the session's local working WAV. Called from
    process_session's finally so a re-process starts from a fresh
    copy and %TEMP% doesn't accumulate scratch files."""
    p = _processing_temp_dir() / f"{session_id}.wav"
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            logger.info(f"could not purge {p}: {e}")


def _scratch_temp_dir() -> Path:
    """Local-only scratch dir for finalize intermediates (`_lb16k_*.tmp.wav`).

    DATA-LOSS FIX (field repro 2026-06-15): the 58-min 8B88C1C3 session
    segfaulted at native level during finalize. Backend exited with
    STATUS_ACCESS_VIOLATION (0xC0000005) and never wrote a session JSON,
    so the meeting just vanished from the Sessions list. Root cause: this
    function's intermediate loopback-resample file used to land at
    ``out_path.parent / '_lb16k_<id>.tmp.wav'`` — which on a user with
    ``recordings_dir`` on Google Drive Stream put the 100+ MB temp write
    on the cloud-synced volume. The cloud sync filter driver contends
    with libsndfile's write path; on a long recording the contention
    pushes scipy/sf into a state that triggers a native crash.

    Mirrors the v2.10.5/v2.10.6 fix that moved the capture-time WAV
    streams to ``%TEMP%\\meeting_recorder_capture\\``. Same dir name so
    recovery sees both kinds of temps in one place. Created on demand,
    never deleted by anything other than the finalize cleanup."""
    d = Path(tempfile.gettempdir()) / "meeting_recorder_capture"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_wav(path: str, audio: np.ndarray, samplerate: int) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    audio_clipped = np.clip(audio, -1.0, 1.0)
    sf.write(path, audio_clipped, samplerate, subtype="PCM_16")
    logger.info(f"Saved WAV: {path} ({len(audio_clipped)/samplerate:.1f}s @ {samplerate}Hz)")


def resample_to_16k(audio: np.ndarray, orig_sr: int) -> np.ndarray:
    """
    High quality resample to 16kHz using polyphase filtering.
    Much better than scipy.signal.resample for audio — no aliasing artifacts.
    """
    if orig_sr == TARGET_SAMPLE_RATE:
        return audio.astype(np.float32)
    divisor = gcd(TARGET_SAMPLE_RATE, orig_sr)
    up = TARGET_SAMPLE_RATE // divisor
    down = orig_sr // divisor
    resampled = resample_poly(audio, up, down)
    return np.clip(resampled, -1.0, 1.0).astype(np.float32)


def mix_stereo_to_mono(audio: np.ndarray) -> np.ndarray:
    if audio.ndim == 2:
        return audio.mean(axis=1).astype(np.float32)
    return audio.astype(np.float32)


def _resample_block(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample a single (in-memory) mono block. Edge artifacts are inaudible
    for speech at STREAM_BLOCK_SECONDS granularity."""
    if orig_sr == target_sr:
        return audio.astype(np.float32, copy=False)
    divisor = gcd(target_sr, orig_sr)
    up = target_sr // divisor
    down = orig_sr // divisor
    resampled = resample_poly(audio.astype(np.float64, copy=False), up, down)
    return resampled.astype(np.float32)


def _stream_resample_to_file(
    in_path: str,
    out_path: str,
    orig_sr: int,
    target_sr: int,
    block_seconds: float = STREAM_BLOCK_SECONDS,
) -> int:
    """
    Read `in_path` block-by-block, resample each block to target_sr, write to
    `out_path` (mono float32 WAV). Returns total frames written.

    Uses bounded memory (~block_seconds × orig_sr × 4 bytes per pass).
    """
    block_frames = max(int(orig_sr * block_seconds), 1024)
    total_out = 0
    with sf.SoundFile(in_path, mode="r") as reader:
        with sf.SoundFile(
            out_path, mode="w",
            samplerate=target_sr, channels=1, subtype="FLOAT",
        ) as writer:
            while True:
                block = reader.read(
                    block_frames, dtype="float32", always_2d=False,
                )
                if block is None or len(block) == 0:
                    break
                if block.ndim == 2:
                    block = block.mean(axis=1)
                out_block = _resample_block(block, orig_sr, target_sr)
                writer.write(out_block)
                total_out += len(out_block)
    return total_out


def _wav_layout(path: str) -> Optional[dict]:
    """Parse a WAV's chunk layout WITHOUT trusting the declared data
    size. Returns fmt params + the data chunk's content offset and
    declared size + the real file size — or None if it isn't a
    RIFF/WAVE we can read.

    Stops at the first ``data`` chunk (always the last chunk in the
    recorder's streaming captures) so a stale/truncated data-size field
    can't send us seeking into raw PCM hunting for phantom chunks."""
    import struct
    p = Path(path)
    try:
        file_size = p.stat().st_size
    except OSError:
        return None
    if file_size < 44:
        return None
    try:
        with open(p, "rb") as f:
            riff = f.read(12)
            if len(riff) < 12 or riff[0:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return None
            channels = samplerate = block_align = bits = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, csize = struct.unpack("<4sI", hdr)
                content = f.tell()
                if cid == b"fmt ":
                    fmt = f.read(min(csize, 16))
                    if len(fmt) >= 16:
                        (_afmt, channels, samplerate, _br,
                         block_align, bits) = struct.unpack("<HHIIHH", fmt[:16])
                    f.seek(content + csize + (csize & 1))
                elif cid == b"data":
                    return {
                        "file_size": file_size,
                        "data_offset": content,
                        "declared_data_size": csize,
                        "channels": channels,
                        "samplerate": samplerate,
                        "block_align": block_align,
                        "bits": bits,
                    }
                else:
                    f.seek(content + csize + (csize & 1))
    except (OSError, struct.error):
        return None
    return None


def repair_truncated_wav_header(path: str) -> dict:
    """Rewrite a WAV's RIFF + data chunk sizes to cover the PCM actually
    on disk, when the declared data size is short of what the file's
    byte count implies. Returns {"repaired": bool, "reason": str,
    "declared_frames": int, "actual_frames": int}.

    This is the fingerprint of a process killed mid-recording: the
    streaming writer flushes audio frames continuously but only patches
    the RIFF/data length fields on a clean close. A hard kill (crash,
    installer launch, power loss) leaves those fields stale, so every
    reader — sf.info, the merge, media players — sees only the handful
    of frames the header advertises even though the full recording is
    physically present. Left unrepaired, recovery merges the short
    length and then deletes the temp that held everything (field repro:
    session 191D826D, 2026-06-30 — 20+ min captured, 1 min survived).

    Idempotent and safe: on a cleanly-closed file the declared size
    already matches the bytes, so this is a no-op. Assumes ``data`` is
    the final chunk (true for the recorder's streaming captures, the
    only files this is ever called on). Never raises — returns
    ``repaired=False`` on any malformed input."""
    import struct
    layout = _wav_layout(path)
    if not layout:
        return {"repaired": False, "reason": "unreadable"}
    ba = layout["block_align"]
    if not ba:
        return {"repaired": False, "reason": "no_fmt"}
    actual = layout["file_size"] - layout["data_offset"]
    actual -= actual % ba                       # align down to whole frames
    declared = layout["declared_data_size"]
    declared_frames = declared // ba
    actual_frames = max(actual, 0) // ba
    # Only touch the file when the real PCM is meaningfully longer than
    # the header claims (one block of slack avoids churn on good files).
    if actual <= declared + ba:
        return {"repaired": False, "reason": "header_ok",
                "declared_frames": declared_frames,
                "actual_frames": actual_frames}
    try:
        with open(path, "r+b") as f:
            f.seek(layout["data_offset"] - 4)   # data chunk size field
            f.write(struct.pack("<I", actual))
            f.seek(4)                           # RIFF chunk size field
            f.write(struct.pack("<I", layout["file_size"] - 8))
            f.flush()
    except (OSError, struct.error) as e:
        return {"repaired": False, "reason": f"write_failed: {e}",
                "declared_frames": declared_frames,
                "actual_frames": actual_frames}
    return {"repaired": True, "reason": "rewrote_sizes",
            "declared_frames": declared_frames,
            "actual_frames": actual_frames}


def wav_byte_implied_duration(path: str) -> float:
    """Seconds of audio the file's actual byte count implies, ignoring
    the header's declared length. 0.0 if unreadable. Used as a
    truncation tripwire: if a merge produced far fewer seconds than the
    source bytes imply, the source must be preserved, not deleted."""
    layout = _wav_layout(path)
    if not layout:
        return 0.0
    ba = layout["block_align"]
    sr = layout["samplerate"]
    if not ba or not sr:
        return 0.0
    actual = layout["file_size"] - layout["data_offset"]
    frames = max(actual, 0) // ba
    return frames / sr


def finalize_recording_streaming(
    mic_wav_path: str,
    loopback_wav_path: Optional[str],
    output_wav_path: str,
    target_sr: int = TARGET_SAMPLE_RATE,
    block_seconds: float = STREAM_BLOCK_SECONDS,
    loopback_start_offset_s: Optional[float] = None,
) -> Tuple[float, bool]:
    """
    Stream-merge mic + (optional) loopback into a single mono PCM_16 WAV at
    target_sr. Uses bounded memory regardless of recording length.

    Cross-stream alignment:
      - When loopback_start_offset_s is provided (live recording path),
        the loopback track is positioned at exactly that offset from t=0,
        using wallclock anchors captured the first time each stream
        delivered audio. This is the correct alignment.
      - When None (legacy callers, recovery path with no clock info), we
        fall back to right-aligning loopback against mic length. This
        assumes both streams stopped at the same wallclock instant — close
        enough at stop, but the start offset is approximate.

    Drift detection: at end of merge, log a warning if the produced
    duration differs from sample-count-derived expectations by >1%. Useful
    diagnostic for long recordings on devices with non-nominal sample
    rates (USB audio, Bluetooth, virtualized inputs).

    Args:
        mic_wav_path: path to the mic-only temp WAV (any SR, mono float).
        loopback_wav_path: optional loopback WAV. Missing/empty → mic-only.
        output_wav_path: destination session WAV.
        target_sr: output sample rate (default 16 kHz — matches whisper/pyannote).
        block_seconds: streaming block size in seconds of input audio.
        loopback_start_offset_s: known cross-stream offset (loopback start
            minus mic start, in seconds, ≥0). Pass None to fall back to
            the right-aligned heuristic.

    Returns:
        (duration_seconds_written, loopback_mixed_in)

    Raises:
        RuntimeError: if mic recording is missing or empty.
    """
    mic_path = Path(mic_wav_path)
    if not mic_path.exists():
        raise RuntimeError(f"Mic recording not found: {mic_wav_path}")
    # Repair a header left truncated by a mid-recording kill BEFORE we
    # trust sf.info's frame count. Without this, a WAV whose length
    # field was never finalized reports only the frames the stale
    # header advertises, and we merge (and later delete) a fraction of
    # the real capture. No-op on cleanly-closed files.
    mic_rep = repair_truncated_wav_header(str(mic_path))
    if mic_rep.get("repaired"):
        logger.warning(
            f"Repaired truncated mic header {mic_path.name}: "
            f"{mic_rep.get('declared_frames')} → "
            f"{mic_rep.get('actual_frames')} frames — recording was "
            f"interrupted before the WAV was finalized"
        )
    mic_info = sf.info(str(mic_path))
    if mic_info.frames == 0:
        raise RuntimeError(f"Mic recording is empty: {mic_wav_path}")
    mic_sr = mic_info.samplerate
    mic_total_frames = mic_info.frames

    have_lb = False
    lb_sr = 0
    lb_total_frames = 0
    if loopback_wav_path and Path(loopback_wav_path).exists():
        # Defensive size check before handing the file to libsndfile.
        # A WAV header alone is ~44 bytes; anything smaller than 1 KB
        # has no meaningful audio data and may even be a malformed
        # header-only file (e.g. when the loopback reader thread opens
        # the writer but the WASAPI device never delivers a buffer
        # because nothing is rendering to that endpoint). libsndfile
        # has been observed to crash the Python process at native
        # level on such files — `try/except` won't catch a segfault.
        # Skip the soundfile call entirely for files this small and
        # fall through to the mic-only merge path. Mirrors the same
        # 1024-byte threshold used in recovery_service for orphans.
        try:
            lb_size = Path(loopback_wav_path).stat().st_size
        except OSError:
            lb_size = 0
        if lb_size < 1024:
            logger.warning(
                f"Loopback WAV is {lb_size} bytes (no audio frames) — "
                f"merging mic-only"
            )
        else:
            # Same header repair as the mic side — a loopback stream
            # killed mid-write under-reports its length too.
            lb_rep = repair_truncated_wav_header(str(loopback_wav_path))
            if lb_rep.get("repaired"):
                logger.warning(
                    f"Repaired truncated loopback header "
                    f"{Path(loopback_wav_path).name}: "
                    f"{lb_rep.get('declared_frames')} → "
                    f"{lb_rep.get('actual_frames')} frames"
                )
            try:
                lb_info = sf.info(str(loopback_wav_path))
                if lb_info.frames > 0:
                    have_lb = True
                    lb_sr = lb_info.samplerate
                    lb_total_frames = lb_info.frames
            except Exception as e:
                logger.warning(
                    f"Could not read loopback info ({e}) — "
                    f"merging mic-only"
                )

    out_path = Path(output_wav_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Pass 1: pre-resample loopback to target_sr into a temp file. This lets
    # pass 2 do simple frame-aligned mixing without bundling a streaming
    # resampler for two sources at once.
    #
    # CRITICAL: the temp file lands in `_scratch_temp_dir()` (a local-only
    # %TEMP% subdir), NOT in `out_path.parent`. When the user's
    # `recordings_dir` is on Google Drive Stream / OneDrive / iCloud, a
    # 100+ MB temp write on the cloud-synced mount races with the sync
    # filter driver and can crash libsndfile at native level during the
    # subsequent merge — see _scratch_temp_dir() for the full
    # 2026-06-15 8B88C1C3 field-repro write-up.
    lb_path_16k: Optional[str] = None
    lb_total_out = 0
    if have_lb:
        lb_path_16k = str(
            _scratch_temp_dir() / f"_lb16k_{out_path.stem}.tmp.wav"
        )
        try:
            lb_total_out = _stream_resample_to_file(
                str(loopback_wav_path), lb_path_16k, lb_sr, target_sr,
                block_seconds=block_seconds,
            )
        except Exception as e:
            logger.warning(
                f"Loopback pre-resample failed ({e}) — merging mic-only"
            )
            _safe_unlink(lb_path_16k)
            lb_path_16k = None
            lb_total_out = 0
            have_lb = False

    mic_total_out = int(round(mic_total_frames * target_sr / mic_sr))
    if have_lb:
        if loopback_start_offset_s is not None and loopback_start_offset_s >= 0:
            # Wallclock-anchored alignment. The first loopback frame sits at
            # exactly loopback_start_offset_s from t=0 in the merged output.
            lb_offset_out = int(round(loopback_start_offset_s * target_sr))
            logger.info(
                f"Loopback aligned by wallclock: offset {loopback_start_offset_s:.3f}s"
            )
        else:
            # Legacy heuristic: assume both streams stopped at the same
            # wallclock instant and right-align by length difference.
            lb_offset_out = max(0, mic_total_out - lb_total_out)
            logger.info(
                f"Loopback aligned by right-justification (no clock anchor)"
            )
    else:
        lb_offset_out = None

    written_out = 0
    loopback_mixed = False
    lb_reader: Optional[sf.SoundFile] = None
    try:
        with sf.SoundFile(
            str(out_path), mode="w",
            samplerate=target_sr, channels=1, subtype="PCM_16",
        ) as writer:
            with sf.SoundFile(str(mic_path), mode="r") as mic_reader:
                if have_lb and lb_path_16k:
                    lb_reader = sf.SoundFile(lb_path_16k, mode="r")

                mic_block_frames = max(
                    int(mic_sr * block_seconds), 1024)

                while True:
                    mic_block = mic_reader.read(
                        mic_block_frames, dtype="float32",
                        always_2d=False,
                    )
                    if mic_block is None or len(mic_block) == 0:
                        break
                    if mic_block.ndim == 2:
                        mic_block = mic_block.mean(axis=1)

                    mic_out = _resample_block(mic_block, mic_sr, target_sr)
                    # mic_out is float32 and writable (resample_poly allocates)

                    if (have_lb and lb_reader is not None
                            and lb_offset_out is not None):
                        out_block_start = written_out
                        out_block_end = written_out + len(mic_out)
                        lb_region_start = lb_offset_out
                        lb_region_end = lb_offset_out + lb_total_out

                        overlap_start = max(
                            out_block_start, lb_region_start)
                        overlap_end = min(
                            out_block_end, lb_region_end)
                        if overlap_end > overlap_start:
                            pre = overlap_start - out_block_start
                            need = overlap_end - overlap_start
                            lb_block = lb_reader.read(
                                need, dtype="float32",
                                always_2d=False,
                            )
                            if lb_block is None:
                                lb_block = np.zeros(0, dtype=np.float32)
                            if lb_block.ndim == 2:
                                lb_block = lb_block.mean(axis=1)
                            if len(lb_block) < need:
                                pad = np.zeros(need, dtype=np.float32)
                                pad[:len(lb_block)] = lb_block
                                lb_block = pad
                            mic_out[pre:pre + need] = (
                                mic_out[pre:pre + need] + lb_block
                            )
                            loopback_mixed = True

                    np.clip(mic_out, -1.0, 1.0, out=mic_out)
                    writer.write(mic_out)
                    written_out += len(mic_out)
    finally:
        if lb_reader is not None:
            try:
                lb_reader.close()
            except Exception:
                pass
        if lb_path_16k:
            _safe_unlink(lb_path_16k)

    duration_s = written_out / target_sr
    if have_lb and loopback_mixed:
        # Drift sanity check. mic_total_out is what we expect from the mic
        # samplecount alone; written_out should match (mix doesn't add or
        # drop frames, only overlays). A divergence >1% means something is
        # off — most likely a write error during recording or an SR
        # mismatch we didn't catch. Worth a warning but not a failure.
        expected = mic_total_out
        if expected > 0:
            drift_pct = abs(written_out - expected) / expected * 100.0
            if drift_pct > 1.0:
                logger.warning(
                    f"Audio drift detected: written {written_out} vs "
                    f"expected {expected} samples ({drift_pct:.2f}%). "
                    f"Long recordings on USB/Bluetooth devices may need "
                    f"resampling at capture time."
                )
        logger.info(
            f"Merged mic + loopback streaming: {duration_s:.1f}s "
            f"(mic {mic_sr}Hz, lb {lb_sr}Hz → {target_sr}Hz)"
        )
    else:
        logger.info(
            f"Merged mic-only streaming: {duration_s:.1f}s "
            f"(mic {mic_sr}Hz → {target_sr}Hz)"
        )
    return duration_s, loopback_mixed


def _safe_unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass
