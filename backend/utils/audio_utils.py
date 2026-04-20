"""Audio file helpers: WAV writing, resampling, mixing."""

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


def finalize_recording_streaming(
    mic_wav_path: str,
    loopback_wav_path: Optional[str],
    output_wav_path: str,
    target_sr: int = TARGET_SAMPLE_RATE,
    block_seconds: float = STREAM_BLOCK_SECONDS,
) -> Tuple[float, bool]:
    """
    Stream-merge mic + (optional) loopback into a single mono PCM_16 WAV at
    target_sr. Uses bounded memory regardless of recording length.

    Loopback is right-aligned against mic (it starts later because WASAPI
    blocks until audio actually plays). This matches the previous in-memory
    behaviour at recording_service.py:190-193.

    Args:
        mic_wav_path: path to the mic-only temp WAV (any SR, mono float).
        loopback_wav_path: optional loopback WAV. Missing/empty → mic-only.
        output_wav_path: destination session WAV.
        target_sr: output sample rate (default 16 kHz — matches whisper/pyannote).
        block_seconds: streaming block size in seconds of input audio.

    Returns:
        (duration_seconds_written, loopback_mixed_in)

    Raises:
        RuntimeError: if mic recording is missing or empty.
    """
    mic_path = Path(mic_wav_path)
    if not mic_path.exists():
        raise RuntimeError(f"Mic recording not found: {mic_wav_path}")
    mic_info = sf.info(str(mic_path))
    if mic_info.frames == 0:
        raise RuntimeError(f"Mic recording is empty: {mic_wav_path}")
    mic_sr = mic_info.samplerate
    mic_total_frames = mic_info.frames

    have_lb = False
    lb_sr = 0
    lb_total_frames = 0
    if loopback_wav_path and Path(loopback_wav_path).exists():
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
    lb_path_16k: Optional[str] = None
    lb_total_out = 0
    if have_lb:
        lb_path_16k = str(out_path.parent / f"_lb16k_{out_path.stem}.tmp.wav")
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
    lb_offset_out = max(0, mic_total_out - lb_total_out) if have_lb else None

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
