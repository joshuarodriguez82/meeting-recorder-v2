"""
Shared fixtures for the backend test suite.

The backend is laid out as a flat package rooted at backend/ (server.py
does `from utils.logger import ...`), so the suite injects that root
onto sys.path — tests run from anywhere: `pytest backend/tests` at the
repo root, or `pytest tests` inside backend/.

Scope note: this suite deliberately covers ONLY the data-loss surface —
audio finalization, crash recovery, and session persistence. No FastAPI
endpoints, no ML models, no network. The only third-party deps are
numpy / scipy / soundfile, which install in seconds in CI.
"""

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def write_sine_wav(
    path: Path,
    duration_s: float,
    samplerate: int,
    freq_hz: float = 440.0,
    amplitude: float = 0.5,
    channels: int = 1,
) -> Path:
    """Synthesize a sine-tone WAV — the test stand-in for captured audio."""
    t = np.arange(int(duration_s * samplerate)) / samplerate
    tone = (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    if channels == 2:
        tone = np.column_stack([tone, tone])
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), tone, samplerate, subtype="FLOAT")
    return path


def write_silence_wav(path: Path, duration_s: float, samplerate: int) -> Path:
    frames = np.zeros(int(duration_s * samplerate), dtype=np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), frames, samplerate, subtype="FLOAT")
    return path


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    d = tmp_path / "recordings"
    d.mkdir()
    return d
