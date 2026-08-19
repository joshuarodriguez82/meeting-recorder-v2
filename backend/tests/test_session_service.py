"""
SessionService owns the only durable record of every meeting. These
tests pin the persistence contract: atomic writes leave no litter,
round-trips are lossless, corruption is surfaced (not swallowed), and
sidecar files never masquerade as sessions in the browser list.
"""

import json
from pathlib import Path

import pytest

from models.session import Session
from services.session_service import SessionService


def _make_session(session_id: str) -> Session:
    session = Session(session_id=session_id)
    session.display_name = "Umbrella ASM Workflow Review"
    session.client = "Umbrella"
    session.project = "Email Automation POC"
    session.notes = "Follow up on ARV multi-region DR question"
    session.audio_path = f"/recordings/session_{session_id}.wav"
    return session


def test_save_then_load_round_trips_core_fields(recordings_dir: Path):
    svc = SessionService(str(recordings_dir))
    saved_path = svc.save(_make_session("rt1"))

    assert Path(saved_path).name == "session_rt1.json"
    loaded = svc.load("rt1")
    assert loaded["display_name"] == "Umbrella ASM Workflow Review"
    assert loaded["client"] == "Umbrella"
    assert loaded["notes"] == "Follow up on ARV multi-region DR question"

    rebuilt = svc.load_full("rt1")
    assert isinstance(rebuilt, Session)
    assert rebuilt.session_id == "rt1"
    assert rebuilt.project == "Email Automation POC"


def test_atomic_save_leaves_no_temp_litter(recordings_dir: Path):
    svc = SessionService(str(recordings_dir))
    svc.save(_make_session("atomic1"))
    assert list(recordings_dir.glob("*.json.tmp")) == []


def test_load_missing_session_returns_none(recordings_dir: Path):
    assert SessionService(str(recordings_dir)).load("ghost") is None


def test_corrupt_session_json_raises_value_error(recordings_dir: Path):
    (recordings_dir / "session_corrupt1.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt session file"):
        SessionService(str(recordings_dir)).load("corrupt1")


def test_list_sessions_skips_sidecar_files(recordings_dir: Path):
    """session_<id>.commitments.json etc. share the glob but are NOT
    sessions — a regression here floods the Session Browser with junk
    rows."""
    svc = SessionService(str(recordings_dir))
    svc.save(_make_session("real1"))
    (recordings_dir / "session_real1.commitments.json").write_text(
        json.dumps([{"id": "c1"}]))
    (recordings_dir / "session_real1.item_status.json").write_text("{}")

    listed_ids = [entry["session_id"] for entry in svc.list_sessions()]

    assert listed_ids == ["real1"]


def test_save_overwrites_existing_session_in_place(recordings_dir: Path):
    svc = SessionService(str(recordings_dir))
    first = _make_session("ow1")
    svc.save(first)
    first.display_name = "Renamed After Edit"
    svc.save(first)

    assert svc.load("ow1")["display_name"] == "Renamed After Edit"
    assert len(list(recordings_dir.glob("session_ow1*.json"))) == 1
