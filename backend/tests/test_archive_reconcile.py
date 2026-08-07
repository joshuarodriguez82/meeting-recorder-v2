"""Tests for the Session Archive reconciliation helper.

Guards the 2026-08-07 field report: SESSION_ARCHIVE_DIR had no Settings
UI at all (env var only), and the "what's pending" comparison lived
inline inside _reconcile_archive() with no way to query it without
actually enqueueing exports. services/archive_reconcile.py pulls the
comparison out so GET /sessions/archive-status and the "Sync now"
button read from the identical rule.
"""

from pathlib import Path

from services.archive_reconcile import (
    archived_session_ids,
    local_session_ids,
    pending_session_ids,
)


def _touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


# ── local_session_ids ───────────────────────────────────────────────

def test_local_session_ids_finds_recursive_sessions(tmp_path):
    """Field report 2026-08-07 (bug 3): a non-recursive scan made a
    session filed into a subfolder of the primary dir invisible to the
    archive machinery even though it showed up in the app."""
    _touch(tmp_path / "session_top.json")
    _touch(tmp_path / "Acme" / "session_nested.json")
    ids = set(local_session_ids(tmp_path))
    assert ids == {"top", "nested"}


def test_local_session_ids_skips_dotted_sidecars(tmp_path):
    _touch(tmp_path / "session_abc.json")
    _touch(tmp_path / "session_abc.commitments.json")
    _touch(tmp_path / "session_abc.item_status.json")
    assert local_session_ids(tmp_path) == ["abc"]


def test_local_session_ids_empty_dir(tmp_path):
    assert local_session_ids(tmp_path) == []


# ── archived_session_ids ────────────────────────────────────────────

def test_archived_session_ids_counts_flat_files_only(tmp_path):
    dest = tmp_path / "archive"
    _touch(dest / "session_a.json")
    _touch(dest / "session_b.json")
    _touch(dest / "session_b.commitments.json")
    # A subfolder is not scanned — the archive writer never creates one.
    _touch(dest / "nested" / "session_c.json")
    assert sorted(archived_session_ids(str(dest))) == ["a", "b"]


def test_archived_session_ids_no_folder_configured():
    assert archived_session_ids("") == []
    assert archived_session_ids(None) == []


def test_archived_session_ids_folder_missing(tmp_path):
    assert archived_session_ids(str(tmp_path / "does-not-exist")) == []


# ── pending_session_ids ─────────────────────────────────────────────

def test_pending_empty_when_everything_present(tmp_path):
    src = tmp_path / "recordings"
    dest = tmp_path / "archive"
    _touch(src / "session_a.json", mtime=1000)
    _touch(dest / "session_a.json", mtime=2000)  # archived copy is newer
    assert pending_session_ids(src, str(dest)) == []


def test_pending_returns_sessions_missing_from_archive(tmp_path):
    src = tmp_path / "recordings"
    dest = tmp_path / "archive"
    _touch(src / "session_a.json")
    _touch(src / "session_b.json")
    _touch(dest / "session_a.json")
    assert pending_session_ids(src, str(dest)) == ["b"]


def test_pending_returns_stale_archive_copies(tmp_path):
    """A local session that has changed since it was archived (mtime
    newer than the archived copy) still owes a fresh copy."""
    src = tmp_path / "recordings"
    dest = tmp_path / "archive"
    _touch(src / "session_a.json", mtime=2000)
    _touch(dest / "session_a.json", mtime=1000)  # stale
    assert pending_session_ids(src, str(dest)) == ["a"]


def test_pending_no_archive_configured_is_not_pending(tmp_path):
    """Feature off (no folder configured at all) is not the same thing
    as a disconnected folder — nothing is owed when archiving isn't in
    use."""
    src = tmp_path / "recordings"
    _touch(src / "session_a.json")
    assert pending_session_ids(src, "") == []
    assert pending_session_ids(src, None) == []


def test_pending_missing_archive_folder_is_all_pending(tmp_path):
    """THE guarantee: a configured-but-unreachable archive folder (sync
    mount offline, path typo, external disk unplugged) must read as
    'every local session pending' — never 'all present'. Getting this
    backwards would let a disconnected Drive/OneDrive mount silently
    report the archive fully caught up when nothing was ever written."""
    src = tmp_path / "recordings"
    _touch(src / "session_a.json")
    _touch(src / "session_b.json")
    gone = tmp_path / "not-mounted" / "archive"
    assert sorted(pending_session_ids(src, str(gone))) == ["a", "b"]


def test_pending_recursive_and_skips_dotted_sidecars(tmp_path):
    src = tmp_path / "recordings"
    dest = tmp_path / "archive"
    _touch(src / "Acme" / "session_nested.json")
    _touch(src / "session_nested.commitments.json")  # sidecar, skipped
    assert pending_session_ids(src, str(dest)) == ["nested"]
