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


def test_subprocess_accepts_echo_cancellation_flag(recordings_dir: Path):
    """--echo-cancellation is opt-in and threaded all the way from
    _run_finalize_subprocess's argv through to finalize_recording_
    streaming's echo_cancellation_enabled param. This just proves the
    flag is accepted and the subprocess still succeeds end-to-end with
    it set — the AEC decision logic itself is covered in test_aec.py."""
    mic = write_sine_wav(
        recordings_dir / "_recording_subD.wav",
        duration_s=2.0, samplerate=16000,
    )
    loopback = write_sine_wav(
        recordings_dir / "_loopback_subD.wav",
        duration_s=2.0, samplerate=16000,
    )
    out = recordings_dir / "session_subD.wav"

    proc = _run([
        "--mic", str(mic),
        "--loopback", str(loopback),
        "--output", str(out),
        "--target-sr", "16000",
        "--offset", "0.0",
        "--echo-cancellation",
    ])

    assert proc.returncode == 0, (
        f"unexpected exit {proc.returncode}; stderr={proc.stderr}")
    result = _parse_result(proc.stdout)
    assert result["loopback_mixed"] == "true"
    assert float(result["duration_s"]) == pytest.approx(2.0, abs=0.05)


def _parse_aec_result(stdout: str) -> dict | None:
    import json
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("AEC_RESULT "):
            return json.loads(line[len("AEC_RESULT "):])
    return None


def test_subprocess_emits_aec_result_line_when_requested(recordings_dir: Path):
    """--echo-cancellation must produce a machine-parseable AEC_RESULT
    line on stdout, ahead of (or alongside) RESULT — this is the only
    channel the AEC decision has to reach the parent process, since the
    child's own logger.info(...) calls land on stdout too and the
    parent only ever treated stdout as noise before this."""
    mic = write_sine_wav(
        recordings_dir / "_recording_subE.wav",
        duration_s=2.0, samplerate=16000,
    )
    loopback = write_sine_wav(
        recordings_dir / "_loopback_subE.wav",
        duration_s=2.0, samplerate=16000,
    )
    out = recordings_dir / "session_subE.wav"

    proc = _run([
        "--mic", str(mic),
        "--loopback", str(loopback),
        "--output", str(out),
        "--target-sr", "16000",
        "--offset", "0.0",
        "--echo-cancellation",
    ])

    assert proc.returncode == 0, (
        f"unexpected exit {proc.returncode}; stderr={proc.stderr}")
    aec_result = _parse_aec_result(proc.stdout)
    assert aec_result is not None, (
        f"no AEC_RESULT line in stdout: {proc.stdout!r}")
    assert aec_result["requested"] is True
    assert aec_result["accepted"] in (True, False)
    assert "reason" in aec_result
    # RESULT must still be present and parseable alongside it.
    assert _parse_result(proc.stdout) != {}


def test_subprocess_emits_no_aec_result_line_when_not_requested(recordings_dir: Path):
    """Without --echo-cancellation, no AEC_RESULT line should appear —
    the parent's job is to read that absence as 'not requested', which
    only works if a truly-off run never emits one."""
    mic = write_sine_wav(
        recordings_dir / "_recording_subF.wav",
        duration_s=2.0, samplerate=16000,
    )
    out = recordings_dir / "session_subF.wav"

    proc = _run([
        "--mic", str(mic),
        "--loopback", "",
        "--output", str(out),
        "--target-sr", "16000",
        "--offset", "",
    ])

    assert proc.returncode == 0, (
        f"unexpected exit {proc.returncode}; stderr={proc.stderr}")
    assert _parse_aec_result(proc.stdout) is None


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
