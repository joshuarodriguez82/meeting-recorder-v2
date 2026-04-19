"""Audio file helpers: WAV writing, resampling, mixing."""

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from math import gcd
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

TARGET_SAMPLE_RATE = 16000


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