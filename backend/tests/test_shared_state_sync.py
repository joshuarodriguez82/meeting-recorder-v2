"""Tests for shared_state_sync (client_configs.json / summary_templates.json
roaming through SESSION_ARCHIVE_DIR).

Guards the 2026-08-07 field report: session JSONs roamed via the Session
Archive but client_configs.json / summary_templates.json never did, so a
client that exists only in client_configs.json (no tagged meeting) was
missing on the second machine, along with per-client folder settings and
custom summary templates.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from services.shared_state_sync import SHARED_FILES, pull, push, status


def _write(path: Path, payload: dict, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_raw(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ── push ─────────────────────────────────────────────────────────────

def test_push_copies_when_local_newer(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()
    _write(local / "client_configs.json", {"acme": {"export_folder": "x"}}, mtime=2000)
    copied = push(str(local), str(archive))
    assert copied == ["client_configs.json"]
    assert (archive / "client_configs.json").is_file()
    assert json.loads((archive / "client_configs.json").read_text()) == {
        "acme": {"export_folder": "x"}
    }


def test_push_copies_when_archive_absent(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()
    _write(local / "summary_templates.json", {"General": {"prompt": "p"}})
    copied = push(str(local), str(archive))
    assert copied == ["summary_templates.json"]


def test_push_noop_when_archive_newer(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {}}, mtime=1000)
    _write(archive / "client_configs.json", {"a": {}, "b": {}}, mtime=5000)
    copied = push(str(local), str(archive))
    assert copied == []
    # archive copy untouched
    assert json.loads((archive / "client_configs.json").read_text()) == {
        "a": {}, "b": {}
    }


def test_push_noop_when_archive_dir_unset(tmp_path):
    local = tmp_path / "recordings"
    _write(local / "client_configs.json", {"a": {}})
    assert push(str(local), "") == []
    assert push(str(local), None) == []


def test_push_noop_when_archive_dir_missing(tmp_path):
    local = tmp_path / "recordings"
    _write(local / "client_configs.json", {"a": {}})
    assert push(str(local), str(tmp_path / "does-not-exist")) == []


def test_push_noop_when_local_file_absent(tmp_path):
    local = tmp_path / "recordings"
    local.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    assert push(str(local), str(archive)) == []


# ── pull ─────────────────────────────────────────────────────────────

def test_pull_copies_when_archive_newer(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {}}, mtime=1000)
    _write(archive / "client_configs.json", {"a": {}, "b": {}}, mtime=5000)
    copied = pull(str(local), str(archive))
    assert copied == ["client_configs.json"]
    assert json.loads((local / "client_configs.json").read_text()) == {
        "a": {}, "b": {}
    }


def test_pull_noop_when_local_newer(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {}, "b": {}}, mtime=5000)
    _write(archive / "client_configs.json", {"a": {}}, mtime=1000)
    copied = pull(str(local), str(archive))
    assert copied == []
    assert json.loads((local / "client_configs.json").read_text()) == {
        "a": {}, "b": {}
    }


def test_pull_noop_when_equal_mtime(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {}}, mtime=3000)
    _write(archive / "client_configs.json", {"z": {}}, mtime=3000)
    copied = pull(str(local), str(archive))
    assert copied == []
    assert json.loads((local / "client_configs.json").read_text()) == {"a": {}}


def test_pull_noop_when_archive_dir_unset_or_missing(tmp_path):
    local = tmp_path / "recordings"
    _write(local / "client_configs.json", {"a": {}})
    assert pull(str(local), "") == []
    assert pull(str(local), str(tmp_path / "nope")) == []


def test_pull_creates_local_file_when_absent(tmp_path):
    local = tmp_path / "recordings"
    local.mkdir()
    archive = tmp_path / "archive"
    _write(archive / "summary_templates.json", {"General": {"prompt": "p"}})
    copied = pull(str(local), str(archive))
    assert copied == ["summary_templates.json"]
    assert (local / "summary_templates.json").is_file()


# ── round trip ───────────────────────────────────────────────────────

def test_round_trip_push_then_pull_identical_bytes(tmp_path):
    """Machine A pushes its client_configs.json to the shared archive;
    machine B (a separate local dir) pulls from that same archive and
    ends up byte-identical."""
    archive = tmp_path / "archive"
    archive.mkdir()
    machine_a = tmp_path / "machine_a"
    machine_b = tmp_path / "machine_b"
    machine_b.mkdir()

    payload = {"acme": {"export_folder": "/A", "knowledge_folder": "/K",
                         "display_name": "Acme"}}
    _write(machine_a / "client_configs.json", payload, mtime=9999)

    pushed = push(str(machine_a), str(archive))
    assert pushed == ["client_configs.json"]

    pulled = pull(str(machine_b), str(archive))
    assert pulled == ["client_configs.json"]

    a_bytes = (machine_a / "client_configs.json").read_bytes()
    b_bytes = (machine_b / "client_configs.json").read_bytes()
    assert a_bytes == b_bytes


# ── safety: malformed / non-dict archive copies are never pulled ──────

def test_pull_refuses_truncated_json(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {"export_folder": "good"}}, mtime=1000)
    # Truncated mid-write — invalid JSON, but newer mtime than local.
    _write_raw(archive / "client_configs.json", '{"a": {"export_fol', mtime=9000)

    copied = pull(str(local), str(archive))
    assert copied == []
    # local file must be untouched
    assert json.loads((local / "client_configs.json").read_text()) == {
        "a": {"export_folder": "good"}
    }
    st = status(str(local), str(archive))
    assert st["client_configs.json"]["direction"] == "pull"
    assert st["client_configs.json"]["reason"]
    assert "malformed" in st["client_configs.json"]["reason"]


def test_pull_refuses_non_dict_json_root(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {}}, mtime=1000)
    _write_raw(archive / "client_configs.json", "[1, 2, 3]", mtime=9000)

    copied = pull(str(local), str(archive))
    assert copied == []
    assert json.loads((local / "client_configs.json").read_text()) == {"a": {}}


# ── status() ────────────────────────────────────────────────────────

def test_status_local_only(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()
    _write(local / "client_configs.json", {"a": {}})
    st = status(str(local), str(archive))
    row = st["client_configs.json"]
    assert row["local_present"] is True
    assert row["archive_present"] is False
    assert row["direction"] == "push"


def test_status_archive_only(tmp_path):
    local = tmp_path / "recordings"
    local.mkdir()
    archive = tmp_path / "archive"
    _write(archive / "client_configs.json", {"a": {}})
    st = status(str(local), str(archive))
    row = st["client_configs.json"]
    assert row["local_present"] is False
    assert row["archive_present"] is True
    assert row["direction"] == "pull"


def test_status_local_newer(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {}}, mtime=5000)
    _write(archive / "client_configs.json", {"a": {}}, mtime=1000)
    row = status(str(local), str(archive))["client_configs.json"]
    assert row["direction"] == "push"


def test_status_archive_newer(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "client_configs.json", {"a": {}}, mtime=1000)
    _write(archive / "client_configs.json", {"a": {}, "b": {}}, mtime=5000)
    row = status(str(local), str(archive))["client_configs.json"]
    assert row["direction"] == "pull"


def test_status_both_absent(tmp_path):
    local = tmp_path / "recordings"
    local.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    row = status(str(local), str(archive))["client_configs.json"]
    assert row["local_present"] is False
    assert row["archive_present"] is False
    assert row["direction"] == "absent"


def test_status_both_absent_covers_all_shared_files(tmp_path):
    local = tmp_path / "recordings"
    local.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    st = status(str(local), str(archive))
    assert set(st.keys()) == set(SHARED_FILES)


def test_status_no_archive_configured(tmp_path):
    local = tmp_path / "recordings"
    _write(local / "client_configs.json", {"a": {}})
    st = status(str(local), "")
    row = st["client_configs.json"]
    assert row["archive_present"] is False
    assert row["direction"] == "in-sync"
