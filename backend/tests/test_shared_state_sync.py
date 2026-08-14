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

from services.shared_state_sync import (
    SHARED_FILES,
    pull,
    push,
    sanitize_local_paths,
    status,
)


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
    # client_configs.json push is field-aware (see below) so this uses
    # summary_templates.json (no filesystem paths) to test the plain
    # whole-file-copy path in isolation.
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()
    _write(local / "summary_templates.json", {"General": {"prompt": "x"}}, mtime=2000)
    copied = push(str(local), str(archive))
    assert copied == ["summary_templates.json"]
    assert (archive / "summary_templates.json").is_file()
    assert json.loads((archive / "summary_templates.json").read_text()) == {
        "General": {"prompt": "x"}
    }


# ── field-aware merge: client_configs.json push blanks per-machine paths ──

def test_push_blanks_export_and_knowledge_folder_keeps_keys_and_display_name(tmp_path):
    """Field report 2026-08-07: client_configs.json carries absolute,
    machine-specific paths (export_folder, knowledge_folder). Pushing to
    the shared archive must never publish this machine's local paths —
    only the client's identity (key + display_name) roams."""
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()
    _write(
        local / "client_configs.json",
        {
            "acme": {
                "export_folder": "~/Acme",
                "knowledge_folder": "~/Acme/Docs",
                "display_name": "Acme Corp",
            }
        },
        mtime=2000,
    )
    copied = push(str(local), str(archive))
    assert copied == ["client_configs.json"]
    archived = json.loads((archive / "client_configs.json").read_text())
    assert archived == {
        "acme": {
            "export_folder": "",
            "knowledge_folder": "",
            "display_name": "Acme Corp",
        }
    }
    # Local copy is untouched — only the archive's copy is blanked.
    local_data = json.loads((local / "client_configs.json").read_text())
    assert local_data["acme"]["export_folder"] == "~/Acme"


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
    # client_configs.json pull is field-aware (see the merge tests below)
    # so this exercises the plain whole-file-copy path via
    # summary_templates.json, which has no filesystem paths.
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(local / "summary_templates.json", {"a": {}}, mtime=1000)
    _write(archive / "summary_templates.json", {"a": {}, "b": {}}, mtime=5000)
    copied = pull(str(local), str(archive))
    assert copied == ["summary_templates.json"]
    assert json.loads((local / "summary_templates.json").read_text()) == {
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
    """Machine A pushes its summary_templates.json (no filesystem paths,
    so still plain whole-file-copy semantics) to the shared archive;
    machine B (a separate local dir) pulls from that same archive and
    ends up byte-identical."""
    archive = tmp_path / "archive"
    archive.mkdir()
    machine_a = tmp_path / "machine_a"
    machine_b = tmp_path / "machine_b"
    machine_b.mkdir()

    payload = {"General": {"prompt": "Summarize this.", "is_default": True}}
    _write(machine_a / "summary_templates.json", payload, mtime=9999)

    pushed = push(str(machine_a), str(archive))
    assert pushed == ["summary_templates.json"]

    pulled = pull(str(machine_b), str(archive))
    assert pulled == ["summary_templates.json"]

    a_bytes = (machine_a / "summary_templates.json").read_bytes()
    b_bytes = (machine_b / "summary_templates.json").read_bytes()
    assert a_bytes == b_bytes


def test_round_trip_push_then_pull_client_identity_not_paths(tmp_path):
    """Machine A (Windows) pushes client_configs.json to the shared
    archive; machine B (a fresh Mac, no client_configs.json yet) pulls
    from that archive. The client's IDENTITY (key + display_name) roams,
    but machine A's Windows path never lands on machine B — field report
    2026-08-07's `G:\\My Drive\\Zorg` incident."""
    archive = tmp_path / "archive"
    archive.mkdir()
    machine_a = tmp_path / "machine_a"
    machine_b = tmp_path / "machine_b"
    machine_b.mkdir()

    payload = {
        "acme": {
            "export_folder": r"G:\My Drive\Zorg",
            "knowledge_folder": r"G:\My Drive\Zorg\Docs",
            "display_name": "Acme",
        }
    }
    _write(machine_a / "client_configs.json", payload, mtime=9999)

    pushed = push(str(machine_a), str(archive))
    assert pushed == ["client_configs.json"]
    # the archive copy itself must never carry machine A's path
    archived = json.loads((archive / "client_configs.json").read_text())
    assert archived["acme"]["export_folder"] == ""
    assert archived["acme"]["knowledge_folder"] == ""

    pulled = pull(str(machine_b), str(archive))
    assert pulled == ["client_configs.json"]
    b_data = json.loads((machine_b / "client_configs.json").read_text())
    assert b_data == {
        "acme": {
            "export_folder": "",
            "knowledge_folder": "",
            "display_name": "Acme",
        }
    }


# ── field-aware merge: pull preserves local paths, unions keys ─────────

def test_pull_preserves_local_folder_paths_even_when_archive_has_foreign_ones(tmp_path):
    """The core 2026-08-07 fix: even though the archive copy is newer and
    carries a different (foreign) path for a client that also exists
    locally, the LOCAL export_folder/knowledge_folder must win."""
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(
        local / "client_configs.json",
        {
            "[scrubbed]": {
                "export_folder": "~/Documents/Zorg",
                "knowledge_folder": "~/Documents/Zorg/Docs",
                "display_name": "Zorg",
            }
        },
        mtime=1000,
    )
    _write(
        archive / "client_configs.json",
        {
            "[scrubbed]": {
                "export_folder": r"G:\My Drive\Zorg",
                "knowledge_folder": r"G:\My Drive\Zorg\Docs",
                "display_name": "Zorg",
            }
        },
        mtime=5000,
    )
    copied = pull(str(local), str(archive))
    assert copied == ["client_configs.json"]
    local_data = json.loads((local / "client_configs.json").read_text())
    assert local_data["[scrubbed]"]["export_folder"] == "~/Documents/Zorg"
    assert local_data["[scrubbed]"]["knowledge_folder"] == "~/Documents/Zorg/Docs"


def test_pull_adds_archive_only_clients_with_empty_folder_fields(tmp_path):
    """A client that exists only in the archive (created on the OTHER
    machine, no tagged meeting here yet) arrives locally with the folder
    fields empty — the user sets them locally, they never come from the
    archive."""
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(
        local / "client_configs.json",
        {"[scrubbed]": {"export_folder": "/local/Zorg", "display_name": "Zorg"}},
        mtime=1000,
    )
    _write(
        archive / "client_configs.json",
        {
            "[scrubbed]": {"export_folder": "/local/Zorg", "display_name": "Zorg"},
            "[scrubbed]": {
                "export_folder": r"G:\My Drive\Hooli",
                "knowledge_folder": r"G:\My Drive\Hooli\Docs",
                "display_name": "Hooli",
            },
        },
        mtime=5000,
    )
    copied = pull(str(local), str(archive))
    assert copied == ["client_configs.json"]
    local_data = json.loads((local / "client_configs.json").read_text())
    assert local_data["[scrubbed]"]["export_folder"] == "/local/Zorg"
    assert local_data["[scrubbed]"]["export_folder"] == ""
    assert local_data["[scrubbed]"]["knowledge_folder"] == ""
    assert local_data["[scrubbed]"]["display_name"] == "Hooli"


def test_pull_never_deletes_local_client_missing_from_archive(tmp_path):
    """A machine that hasn't synced a given client yet must not wipe the
    other machine's list — the local-only client survives a pull."""
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(
        local / "client_configs.json",
        {
            "[scrubbed]": {"export_folder": "/local/Zorg", "display_name": "Zorg"},
            "only-local": {"export_folder": "/local/OnlyLocal", "display_name": "Only Local"},
        },
        mtime=1000,
    )
    _write(
        archive / "client_configs.json",
        {"[scrubbed]": {"export_folder": "", "display_name": "Zorg"}},
        mtime=5000,
    )
    copied = pull(str(local), str(archive))
    assert copied == ["client_configs.json"]
    local_data = json.loads((local / "client_configs.json").read_text())
    assert "only-local" in local_data
    assert local_data["only-local"]["export_folder"] == "/local/OnlyLocal"


def test_pull_updates_display_name_from_newer_archive(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(
        local / "client_configs.json",
        {"[scrubbed]": {"export_folder": "/local/Zorg", "display_name": "Zorg"}},
        mtime=1000,
    )
    _write(
        archive / "client_configs.json",
        {"[scrubbed]": {"export_folder": "", "display_name": "ZORG (renamed)"}},
        mtime=5000,
    )
    copied = pull(str(local), str(archive))
    assert copied == ["client_configs.json"]
    local_data = json.loads((local / "client_configs.json").read_text())
    assert local_data["[scrubbed]"]["display_name"] == "ZORG (renamed)"
    # folder path is still untouched by the (newer) archive
    assert local_data["[scrubbed]"]["export_folder"] == "/local/Zorg"


def test_pull_does_not_update_display_name_from_older_archive(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    _write(
        local / "client_configs.json",
        {"[scrubbed]": {"export_folder": "/local/Zorg", "display_name": "Zorg"}},
        mtime=5000,
    )
    _write(
        archive / "client_configs.json",
        {"[scrubbed]": {"export_folder": "", "display_name": "Stale Name"}},
        mtime=1000,
    )
    copied = pull(str(local), str(archive))
    assert copied == []
    local_data = json.loads((local / "client_configs.json").read_text())
    assert local_data["[scrubbed]"]["display_name"] == "Zorg"


# ── sanitize_local_paths() ──────────────────────────────────────────────

def test_sanitize_clears_windows_drive_letter_path(tmp_path, monkeypatch):
    monkeypatch.setattr("services.shared_state_sync.os.name", "posix")
    local = tmp_path / "recordings"
    _write(
        local / "client_configs.json",
        {
            "[scrubbed]": {
                "export_folder": r"G:\My Drive\Zorg",
                "knowledge_folder": r"G:\My Drive\Zorg\Docs",
                "display_name": "Zorg",
            }
        },
    )
    cleared = sanitize_local_paths(str(local))
    assert len(cleared) == 2
    fields = {c["field"] for c in cleared}
    assert fields == {"export_folder", "knowledge_folder"}
    for c in cleared:
        assert c["client"] == "Zorg"
        assert c["old_value"] in (r"G:\My Drive\Zorg", r"G:\My Drive\Zorg\Docs")
    data = json.loads((local / "client_configs.json").read_text())
    assert data["[scrubbed]"]["export_folder"] == ""
    assert data["[scrubbed]"]["knowledge_folder"] == ""


def test_sanitize_clears_unc_path_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr("services.shared_state_sync.os.name", "posix")
    local = tmp_path / "recordings"
    _write(
        local / "client_configs.json",
        {"[scrubbed]": {"export_folder": r"\\server\share\Zorg", "display_name": "Zorg"}},
    )
    cleared = sanitize_local_paths(str(local))
    assert len(cleared) == 1
    data = json.loads((local / "client_configs.json").read_text())
    assert data["[scrubbed]"]["export_folder"] == ""


def test_sanitize_leaves_plausible_local_path_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("services.shared_state_sync.os.name", "posix")
    local = tmp_path / "recordings"
    _write(
        local / "client_configs.json",
        {"[scrubbed]": {"export_folder": "~/Documents/Zorg", "display_name": "Zorg"}},
    )
    cleared = sanitize_local_paths(str(local))
    assert cleared == []
    data = json.loads((local / "client_configs.json").read_text())
    assert data["[scrubbed]"]["export_folder"] == "~/Documents/Zorg"


def test_sanitize_leaves_existing_but_currently_missing_local_path_alone(tmp_path, monkeypatch):
    """An unplugged external drive (a plausible local path that merely
    doesn't currently resolve) must survive a sanitize pass — only
    structurally foreign paths are cleared."""
    monkeypatch.setattr("services.shared_state_sync.os.name", "posix")
    local = tmp_path / "recordings"
    _write(
        local / "client_configs.json",
        {"[scrubbed]": {"export_folder": "/Volumes/UnpluggedDrive/Zorg", "display_name": "Zorg"}},
    )
    cleared = sanitize_local_paths(str(local))
    assert cleared == []
    data = json.loads((local / "client_configs.json").read_text())
    assert data["[scrubbed]"]["export_folder"] == "/Volumes/UnpluggedDrive/Zorg"


def test_sanitize_noop_when_no_client_configs_file(tmp_path):
    local = tmp_path / "recordings"
    local.mkdir()
    assert sanitize_local_paths(str(local)) == []


def test_sanitize_noop_when_recordings_dir_unset(tmp_path):
    assert sanitize_local_paths("") == []


# NOTE: a Windows-dispatch (os.name == "nt") sanitize test is
# deliberately not included here — pathlib.Path's class (WindowsPath vs
# PosixPath) is itself selected from the REAL os.name at class-definition
# time, so monkeypatching os.name to "nt" while running on this Linux CI
# venv makes Path() raise NotImplementedError before
# _is_foreign_local_path ever runs. The Windows branch is exercised by
# _is_foreign_local_path's own logic (unit-testable in isolation) rather
# than through sanitize_local_paths' filesystem path here; see
# test_is_foreign_local_path_windows_dispatch below.

def test_is_foreign_local_path_windows_dispatch(tmp_path, monkeypatch):
    """Exercise the os.name == "nt" branch of _is_foreign_local_path
    directly (see the NOTE above for why sanitize_local_paths itself
    can't be driven through this branch on a POSIX CI host)."""
    from services.shared_state_sync import _is_foreign_local_path

    monkeypatch.setattr("services.shared_state_sync.os.name", "nt")
    # A POSIX-style path that can't exist is foreign on "Windows".
    assert _is_foreign_local_path("~/Documents/Zorg-does-not-exist") is True
    # A POSIX-style path that DOES resolve (e.g. WSL interop) is left alone.
    assert _is_foreign_local_path(str(tmp_path)) is False
    # A plain Windows-style path is never foreign on Windows.
    assert _is_foreign_local_path(r"C:\\Users\\<you>\Documents\Zorg") is False


def test_sanitize_returns_cleared_list_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("services.shared_state_sync.os.name", "posix")
    local = tmp_path / "recordings"
    _write(
        local / "client_configs.json",
        {"[scrubbed]": {"export_folder": r"G:\My Drive\Hooli", "display_name": "Hooli"}},
    )
    cleared = sanitize_local_paths(str(local))
    assert cleared == [{
        "client": "Hooli",
        "field": "export_folder",
        "old_value": r"G:\My Drive\Hooli",
    }]


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


# ── owner_aliases.json roaming ──────────────────────────────────────
#
# Added alongside client_configs.json / summary_templates.json so a
# user curating owner-name merges (e.g. Josh's spelling variants) on
# one machine sees the same grouping on their other machine against
# the same shared session library — otherwise the two machines would
# silently disagree about who owns what from identical underlying
# data. Like summary_templates.json, this carries no filesystem paths
# (see services/owner_service.py's OwnerAliasStore: just alias ids,
# canonical names, member keys, and rejected-pair bookkeeping), so it
# gets the plain whole-file, newest-wins copy — NOT the field-aware
# merge client_configs.json needs. These tests confirm that by reading
# the module's actual dispatch rather than assuming it.

def test_owner_aliases_is_in_shared_files():
    assert "owner_aliases.json" in SHARED_FILES


def test_owner_aliases_push_copies_plain_whole_file(tmp_path):
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()
    payload = {
        "aliases": {"a1": {"canonical": "Josh",
                            "members": ["josh", "joshua"],
                            "created_at": "2026-08-01T00:00:00"}},
        "rejected": [["dan", "mark"]],
    }
    _write(local / "owner_aliases.json", payload, mtime=2000)

    copied = push(str(local), str(archive))
    assert "owner_aliases.json" in copied
    assert json.loads((archive / "owner_aliases.json").read_text()) == payload


def test_owner_aliases_round_trip_push_then_pull_identical_bytes(tmp_path):
    """Machine A curates a merge and pushes; machine B (fresh local
    dir, same shared archive) pulls and ends up with the exact same
    grouping — the actual acceptance criterion: aliases made on one
    machine must not be silently absent on the other."""
    archive = tmp_path / "archive"
    archive.mkdir()
    machine_a = tmp_path / "machine_a"
    machine_b = tmp_path / "machine_b"
    machine_b.mkdir()

    payload = {
        "aliases": {"a1": {"canonical": "Josh",
                            "members": ["josh", "joshua", "[scrubbed]"],
                            "created_at": "2026-08-01T00:00:00"}},
        "rejected": [],
    }
    _write(machine_a / "owner_aliases.json", payload, mtime=9999)

    pushed = push(str(machine_a), str(archive))
    assert "owner_aliases.json" in pushed
    pulled = pull(str(machine_b), str(archive))
    assert "owner_aliases.json" in pulled

    a_bytes = (machine_a / "owner_aliases.json").read_bytes()
    b_bytes = (machine_b / "owner_aliases.json").read_bytes()
    assert a_bytes == b_bytes


def test_owner_aliases_round_trip_through_the_real_store(tmp_path):
    """End-to-end through OwnerAliasStore itself, not just raw JSON:
    an alias created on machine A resolves owners correctly on machine
    B after a push+pull, using nothing but the public store API on
    each side."""
    from services.owner_service import OwnerAliasStore, load_alias_index, resolve_owners

    archive = tmp_path / "archive"
    archive.mkdir()
    machine_a = tmp_path / "machine_a"
    machine_b = tmp_path / "machine_b"
    machine_b.mkdir()

    store_a = OwnerAliasStore(machine_a)
    store_a.create("Josh", ["Josh", "Joshua", "[scrubbed]"])

    pushed = push(str(machine_a), str(archive))
    assert "owner_aliases.json" in pushed
    pulled = pull(str(machine_b), str(archive))
    assert "owner_aliases.json" in pulled

    store_b = OwnerAliasStore(machine_b)
    idx_b = load_alias_index(store_b)
    assert resolve_owners("Joshua", idx_b) == ["Josh"]
    assert resolve_owners("[scrubbed]", idx_b) == ["Josh"]


def test_owner_aliases_pull_refuses_corrupt_archive_copy(tmp_path):
    """A corrupt/truncated archive copy must not overwrite a good
    local file — same JSON/dict safety gate every other SHARED_FILES
    entry gets — and reading through OwnerAliasStore afterwards must
    degrade to no grouping, never raise."""
    from services.owner_service import OwnerAliasStore, load_alias_index, resolve_owners

    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    good = {
        "aliases": {"a1": {"canonical": "Josh", "members": ["josh", "joshua"],
                            "created_at": ""}},
        "rejected": [],
    }
    _write(local / "owner_aliases.json", good, mtime=1000)
    _write_raw(archive / "owner_aliases.json", '{"aliases": {"a1": {"cano', mtime=9000)

    copied = pull(str(local), str(archive))
    assert "owner_aliases.json" not in copied
    assert json.loads((local / "owner_aliases.json").read_text()) == good

    store = OwnerAliasStore(local)
    idx = load_alias_index(store)
    assert resolve_owners("Joshua", idx) == ["Josh"]  # local alias intact


def test_owner_aliases_missing_on_both_sides_degrades_to_no_grouping(tmp_path):
    """Neither machine has curated any aliases yet — sync is a no-op,
    and reading the (absent) file locally must resolve owners
    unaliased rather than raising."""
    from services.owner_service import OwnerAliasStore, load_alias_index, resolve_owners

    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()

    assert push(str(local), str(archive)) == []
    assert pull(str(local), str(archive)) == []

    store = OwnerAliasStore(local)
    assert store.list_all() == []
    idx = load_alias_index(store)
    assert resolve_owners("Joshua", idx) == ["Joshua"]  # no grouping applied


def test_owner_aliases_corrupt_local_file_does_not_block_push_of_others(tmp_path):
    """A locally-corrupt owner_aliases.json must not raise out of
    push() and must not take summary_templates.json down with it —
    each SHARED_FILES entry is handled independently."""
    local = tmp_path / "recordings"
    archive = tmp_path / "archive"
    archive.mkdir()
    _write_raw(local / "owner_aliases.json", "not json at all {{{", mtime=5000)
    _write(local / "summary_templates.json", {"General": {"prompt": "x"}}, mtime=5000)

    copied = push(str(local), str(archive))
    # owner_aliases.json has no source-side JSON validation (same as
    # summary_templates.json — see push()'s docstring: only
    # client_configs.json gets that extra guard), so the raw bytes are
    # copied through; the receiving side's pull() is what refuses a
    # malformed copy. The key behavior this test locks is that push()
    # never raises and other files still go out.
    assert "summary_templates.json" in copied
