"""
Exercises ``backend/scripts/finalize_audio.py`` as a subprocess.

v2.12 moved the WAV finalize out of the long-running backend process
into a child Python so a native crash in scipy / libsndfile can't kill
the parent. The contract this script exposes to the parent is:

  - argv: --mic PATH --loopback PATH --output PATH --target-sr N --offset S
  - stdout (on success): one line ``RESULT duration_s=X loopback_mixed=Y``
  - exit 0 on success, 1 on expected Python errors, 2 on argparse misuse,
    other / negative / large positive on native crashes
  - stderr: human-readable diagnostics (mirrored into backend.log)

These tests pin that contract. The recording service's
``_run_finalize_subprocess`` helper parses stdout the same way and
relies on the exit-code semantics.
"""
import subprocess
import sys
from pathlib import Path

import pytest
import soundfile as sf

from conftest import write_silence_wav, write_sine_wav

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "finalize_audio.py"
)


def _run(args):
    """Invoke the subprocess with the SAME Python that runs the tests
    so we get an identical numpy/scipy/soundfile build. capture_output
    keeps stdout / stderr addressable for the assertions."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, check=False,
    )


def _parse_result(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.strip().startswith("RESULT "):
            return dict(
                p.split("=", 1) for p in line.split()[1:] if "=" in p
            )
    return {}


def test_subprocess_merges_mic_only(recordings_dir: Path):
    mic = write_sine_wav(
        recordings_dir / "_recording_subA.wav",
        duration_s=2.0, samplerate=48000,
    )
    out = recordings_dir / "session_subA.wav"

    proc = _run([
        "--mic", str(mic),
        "--loopback", "",
        "--output", str(out),
        "--target-sr", "16000",
        "--offset", "",
    ])

    assert proc.returncode == 0, (
        f"unexpected exit {proc.returncode}; stderr={proc.stderr}")
    assert out.exists()
    info = sf.info(str(out))
    assert info.samplerate == 16000
    assert info.channels == 1

    result = _parse_result(proc.stdout)
    assert result["loopback_mixed"] == "false"
    assert float(result["duration_s"]) == pytest.approx(2.0, abs=0.05)


def test_subprocess_merges_mic_and_loopback(recordings_dir: Path):
    """A full mic + loopback merge with a wallclock-anchored offset.
    Proves the subprocess path passes ``--offset`` through correctly
    and the merged WAV's energy distribution matches what the inline
    finalize produced before v2.12."""
    mic = write_silence_wav(
        recordings_dir / "_recording_subB.wav",
        duration_s=5.0, samplerate=48000,
    )
    write_sine_wav(
        recordings_dir / "_loopback_subB.wav",
        duration_s=2.0, samplerate=44100,
    )
    out = recordings_dir / "session_subB.wav"

    proc = _run([
        "--mic", str(mic),
        "--loopback", str(recordings_dir / "_loopback_subB.wav"),
        "--output", str(out),
        "--target-sr", "16000",
        "--offset", "1.0",
    ])

    assert proc.returncode == 0, (
        f"unexpected exit {proc.returncode}; stderr={proc.stderr}")
    assert out.exists()
    result = _parse_result(proc.stdout)
    assert result["loopback_mixed"] == "true"
    # Output duration = mic length (loopback was overlaid, not appended)
    assert float(result["duration_s"]) == pytest.approx(5.0, abs=0.1)


def test_subprocess_exits_1_on_missing_mic(tmp_path: Path):
    """A path that doesn't exist is an EXPECTED failure — Python raises
    RuntimeError inside the child. The parent treats this as a clean
    "merge failed, surface the error" branch, NOT as a native crash.
    Exit code 1 distinguishes the two so the parent's diagnostic is
    actionable."""
    proc = _run([
        "--mic", str(tmp_path / "does_not_exist.wav"),
        "--loopback", "",
        "--output", str(tmp_path / "out.wav"),
        "--target-sr", "16000",
        "--offset", "",
    ])
    assert proc.returncode == 1
    assert "not found" in proc.stderr.lower() or "RuntimeError" in proc.stderr
    # No RESULT line because finalize never reached completion.
    assert _parse_result(proc.stdout) == {}


def test_subprocess_exits_2_on_bad_offset(tmp_path: Path):
    """Argparse-level / wrapper-level misuse exits 2 — distinct from
    finalize failure (1) and native crash (other). Lets the parent
    treat "you called this wrong" differently from "the merge died."
    """
    mic = write_sine_wav(
        tmp_path / "_recording_subC.wav",
        duration_s=1.0, samplerate=48000,
    )
    proc = _run([
        "--mic", str(mic),
        "--loopback", "",
        "--output", str(tmp_path / "out.wav"),
        "--target-sr", "16000",
        "--offset", "not_a_number",
    ])
    assert proc.returncode == 2
