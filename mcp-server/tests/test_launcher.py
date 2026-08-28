"""
The zero-install launcher.

An installed copy of Meeting Recorder ships this directory inside its
runtime bundle; nothing pip-installs it there. ``run_mcp_server.py``
exists so an AI client can be pointed at

    <app python>  <runtime>/mcp-server/run_mcp_server.py

and have it work with no venv of its own, no editable install, and no
PYTHONPATH set by the user — none of which a client's minimal launch
environment would carry anyway.

The launcher is three lines of path arithmetic, which is exactly the
kind of code that is never exercised until it is on someone else's
machine. So it is tested the only way that proves anything: run it as a
subprocess, from an unrelated working directory, and look at what comes
out.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MCP_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = MCP_ROOT / "run_mcp_server.py"


def test_launcher_ships_next_to_the_package():
    """The backend resolves the launcher as a sibling of
    meeting_recorder_mcp/ (services/mcp_bundle_service.py's
    find_bundled_mcp_dir requires both). Moving either breaks the
    in-app config the Settings card hands out."""
    assert LAUNCHER.is_file()
    assert (MCP_ROOT / "meeting_recorder_mcp" / "server.py").is_file()


def test_launcher_runs_the_doctor_from_an_unrelated_cwd(tmp_path: Path):
    """--doctor is the one entry point that reaches main() without
    speaking MCP, so it is the smoke test: if the launcher can import
    the package and dispatch, this prints its banner. Run from tmp_path
    so a working directory that happens to contain the package can't be
    what makes it pass.

    Exit code 2 is the expected outcome, not a failure: MEETING_RECORDER_
    DATA_DIR points at an empty directory, so no token is found and the
    doctor reports that. What is under test is that it got far enough to
    report anything at all.
    """
    env = dict(os.environ)
    env["MEETING_RECORDER_DATA_DIR"] = str(tmp_path / "empty")
    env.pop("MEETING_RECORDER_TOKEN", None)
    env.pop("PYTHONPATH", None)
    (tmp_path / "empty").mkdir()

    proc = subprocess.run(
        [sys.executable, str(LAUNCHER), "--doctor"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert "Meeting Recorder MCP" in proc.stdout, proc.stdout + proc.stderr
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
