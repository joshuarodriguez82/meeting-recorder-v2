"""
v2.12.0 ghost-session cleanup. Stubs accumulate when the backend
crashes mid-recording or mid-finalize — v2.11.1's JSON-first write
left them behind by design (so the session row never vanished
post-crash), but over time they pile up. Field repro 2026-06-26: 69
ghosts on one machine.

These tests pin the contract of ``_scan_ghost_sessions``:
  - A session JSON whose audio_path file is missing on disk = ghost.
  - A session JSON whose audio_path file exists = NOT a ghost.
  - An empty audio_path = ghost (the recovery flow handles these
    separately, but they're still in the scan output).
  - Sidecar files (commitments.json, item_status.json) don't get
    scanned as sessions.
  - Output sorted oldest-first so a UI picker can lead with worst.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Import via direct path manipulation — server.py imports a lot of
# heavy ML and we only want the one helper. Re-import via importlib
# from a stripped namespace to avoid the heavy imports cascade.
import importlib.util
import sys


def _load_scan_helper():
    """Load `_scan_ghost_sessions` from server.py without booting the
    full FastAPI app. We monkey-patch a couple of import names that
    server.py touches at module level."""
    # Pre-create stubs for the heavy modules server.py imports at
    # top-level so the module loads. We don't call any of them.
    src = Path(__file__).resolve().parents[1] / "server.py"
    code = src.read_text(encoding="utf-8")
    # Extract just _scan_ghost_sessions via AST so the test stays
    # independent of server.py's import surface.
    import ast
    tree = ast.parse(code)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_scan_ghost_sessions":
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {
                "Path": Path,
                "datetime": datetime,
                "json": json,
            }
            exec(compile(mod, str(src), "exec"), ns)
            return ns["_scan_ghost_sessions"]
    raise RuntimeError("Could not find _scan_ghost_sessions in server.py")


_scan_ghost_sessions = _load_scan_helper()


def _write_session_json(path: Path, session_id: str, audio_path: str, display_name: str = "") -> None:
    path.write_text(json.dumps({
        "session_id": session_id,
        "display_name": display_name,
        "started_at": "2026-06-26T10:00:00",
        "ended_at": "2026-06-26T10:30:00",
        "audio_path": audio_path,
        "speakers": {},
        "segments": [],
        "screenshots": [],
        "client": "",
        "project": "",
        "notes": "",
        "attendees": [],
    }), encoding="utf-8")


def test_real_session_with_audio_is_not_a_ghost(recordings_dir: Path):
    audio = recordings_dir / "session_alive1.wav"
    audio.write_bytes(b"\x00" * 2048)
    _write_session_json(
        recordings_dir / "session_alive1.json",
        "alive1", str(audio), display_name="Real meeting",
    )

    ghosts = _scan_ghost_sessions(str(recordings_dir))
    assert ghosts == []


def test_session_with_missing_audio_is_a_ghost(recordings_dir: Path):
    _write_session_json(
        recordings_dir / "session_ghost1.json",
        "ghost1", str(recordings_dir / "session_ghost1.wav"),  # does not exist
        display_name="Crashed mid-recording",
    )

    ghosts = _scan_ghost_sessions(str(recordings_dir))
    assert len(ghosts) == 1
    g = ghosts[0]
    assert g["session_id"] == "ghost1"
    assert g["display_name"] == "Crashed mid-recording"
    assert g["audio_path"].endswith("session_ghost1.wav")
    assert g["age_days"] >= 0


def test_scan_skips_sidecar_files(recordings_dir: Path):
    """session_<id>.commitments.json and similar dotted-stem sidecars
    must NOT be enumerated as sessions — they fail json parsing as
    Session shapes AND lack audio_path. A previous regression made
    them show as ghosts and the UI tried to delete real commitment
    sidecars."""
    audio = recordings_dir / "session_alive2.wav"
    audio.write_bytes(b"\x00" * 2048)
    _write_session_json(
        recordings_dir / "session_alive2.json",
        "alive2", str(audio),
    )
    # Sidecars share the session_*.json glob
    (recordings_dir / "session_alive2.commitments.json").write_text("[]")
    (recordings_dir / "session_alive2.item_status.json").write_text("{}")

    ghosts = _scan_ghost_sessions(str(recordings_dir))
    assert ghosts == []  # alive2 has audio; sidecars are skipped


def test_scan_sorted_oldest_first(recordings_dir: Path, tmp_path: Path):
    """When the UI lists ghost sessions for cleanup, leading with the
    oldest is the right default — those are the ones least likely to
    have a recovery path. The scan must sort by age_days desc."""
    import os, time
    young = recordings_dir / "session_young.json"
    old = recordings_dir / "session_old.json"
    _write_session_json(young, "young", str(recordings_dir / "session_young.wav"))
    _write_session_json(old,   "old",   str(recordings_dir / "session_old.wav"))

    # Back-date the "old" stub's mtime by 30 days.
    thirty_days_ago = time.time() - 30 * 86400
    os.utime(old, (thirty_days_ago, thirty_days_ago))

    ghosts = _scan_ghost_sessions(str(recordings_dir))
    ids = [g["session_id"] for g in ghosts]
    # Old appears first.
    assert ids == ["old", "young"]
    assert ghosts[0]["age_days"] >= 29  # ~30 days; allow for rounding


def test_unreadable_json_is_skipped(recordings_dir: Path):
    """A genuinely-corrupt session JSON (truncated mid-write, BOM-
    issues, etc.) should not crash the scan. Retention's cleanup
    pass handles those; we just want to not blow up here."""
    (recordings_dir / "session_corrupt.json").write_text("{ not valid json")

    ghosts = _scan_ghost_sessions(str(recordings_dir))
    assert ghosts == []
