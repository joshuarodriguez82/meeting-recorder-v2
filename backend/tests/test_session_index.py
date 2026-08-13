"""SQLite session-list index (services/session_index.py).

Design constraint under test throughout: the session_*.json files on
disk are the ONLY source of truth. The index is a disposable cache —
every test here is really asking one of two questions:

  1. Does the index ever produce output that DIFFERS from a direct scan
     of the same files? (It must not, ever.)
  2. Does the index ever lose track of a file that's still on disk —
     by deleting its row, or by treating "couldn't read it this round"
     as "it's gone"? (It must not, ever.)

Perf (the actual point of the index — unchanged files are served from
cache with zero JSON parsing) is covered via a parse-call spy rather
than wall-clock timing, which would be flaky in CI.
"""

import json
import os
import sqlite3
import threading

from services.session_index import SessionIndex
from services.session_service import SessionService
import services.session_service as session_service_mod


def _write(d, sid, *, name=None, mtime=None, summary=None):
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"session_{sid}.json"
    p.write_text(json.dumps({
        "session_id": sid,
        "display_name": name or f"Meeting {sid}",
        "started_at": f"2026-01-01T00:00:{int(sid[-2:]) % 60:02d}",
        "summary": summary or "",
    }), encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _spy_parses(monkeypatch):
    """Count calls to SessionService._read_and_parse — the only place
    JSON actually gets parsed, on both the direct and indexed paths."""
    calls = []
    orig = session_service_mod.SessionService._read_and_parse

    def spy(self, path):
        calls.append(str(path))
        return orig(self, path)

    monkeypatch.setattr(session_service_mod.SessionService, "_read_and_parse", spy)
    return calls


# ── index results match direct scan exactly ────────────────────────────

def test_indexed_matches_direct_scan_on_a_mixed_fixture_tree(tmp_path):
    """The strongest regression guard: build a realistic multi-root tree
    (subfolders, an extra/archive root, a cross-root duplicate id, and
    sidecar files that must be ignored) and assert the indexed code path
    returns EXACTLY the same list as the direct-scan code path."""
    current = tmp_path / "current"
    archive = tmp_path / "archive"

    _write(current, "AAAA0001")
    _write(current / "2025" / "sub", "AAAA0002")
    _write(archive, "AAAA0003")
    _write(archive / "nested", "AAAA0004")
    # Same id in both roots — archive is older, current should win.
    _write(archive, "AAAA0005", name="stale", summary="", mtime=1_000_000)
    _write(current, "AAAA0005", name="fresh", summary="done", mtime=2_000_000)
    (current / "session_AAAA0001.commitments.json").write_text("{}", encoding="utf-8")

    direct_svc = SessionService(
        str(current), extra_dirs=[str(archive)], index_enabled=False)
    direct_result = direct_svc.list_sessions()

    indexed_svc = SessionService(
        str(current), extra_dirs=[str(archive)],
        index_enabled=True, index_db_path=str(tmp_path / "idx.db"))
    indexed_result = indexed_svc.list_sessions()

    assert indexed_result == direct_result
    assert len(indexed_result) == 5  # AAAA0001..0004 + AAAA0005 de-duped

    # Run the indexed path a second time (now everything is cache-hit)
    # and confirm it's still byte-identical.
    assert indexed_svc.list_sessions() == direct_result


# ── incremental refresh: the actual performance win ─────────────────────

def test_unchanged_files_are_not_reparsed(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    _write(root, "BB000001")
    _write(root, "BB000002")
    _write(root, "BB000003")

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(tmp_path / "idx.db"))
    calls = _spy_parses(monkeypatch)

    first = svc.list_sessions()
    assert len(first) == 3
    assert len(calls) == 3  # cold cache: every file parsed once

    calls.clear()
    second = svc.list_sessions()
    assert second == first
    assert calls == []  # nothing changed on disk -> zero re-parses


def test_changed_file_is_reparsed_others_are_not(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    _write(root, "CC000001", mtime=1_000_000)
    _write(root, "CC000002", mtime=1_000_000)

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(tmp_path / "idx.db"))
    svc.list_sessions()

    calls = _spy_parses(monkeypatch)
    _write(root, "CC000001", name="renamed", summary="new", mtime=5_000_000)
    result = svc.list_sessions()

    assert calls == [str(root / "session_CC000001.json")]
    row = next(r for r in result if r["session_id"] == "CC000001")
    assert row["display_name"] == "renamed"
    assert row["has_summary"] is True


def test_deleted_file_disappears_from_index_results(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    _write(root, "DD000001")
    _write(root, "DD000002")

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(tmp_path / "idx.db"))
    assert len(svc.list_sessions()) == 2

    (root / "session_DD000001.json").unlink()
    calls = _spy_parses(monkeypatch)
    result = svc.list_sessions()

    assert {r["session_id"] for r in result} == {"DD000002"}
    assert calls == []  # the surviving file was unchanged, no re-parse


# ── corrupt / zero-byte DB self-heals to correct results ────────────────

def test_corrupt_db_file_falls_back_and_still_returns_correct_results(tmp_path):
    root = tmp_path / "recordings"
    _write(root, "EE000001")
    _write(root, "EE000002")

    db_path = tmp_path / "idx.db"
    db_path.write_bytes(b"this is not a sqlite database, just garbage bytes")

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(db_path))
    result = svc.list_sessions()
    assert {r["session_id"] for r in result} == {"EE000001", "EE000002"}

    # And it keeps working on a second call (self-healed, not one-shot).
    result2 = svc.list_sessions()
    assert {r["session_id"] for r in result2} == {"EE000001", "EE000002"}


def test_zero_byte_db_file_is_handled(tmp_path):
    root = tmp_path / "recordings"
    _write(root, "FF000001")

    db_path = tmp_path / "idx.db"
    db_path.write_bytes(b"")

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(db_path))
    result = svc.list_sessions()
    assert {r["session_id"] for r in result} == {"FF000001"}


def test_index_unavailable_after_db_directory_is_unwritable(tmp_path):
    """If SessionIndex can't even be constructed (e.g. the parent dir
    can't be created), SessionService must still work via direct scan —
    never raise, never return nothing."""
    root = tmp_path / "recordings"
    _write(root, "GG000001")

    # A path whose parent is a FILE, not a directory — mkdir(parents=True)
    # inside SessionIndex._connect() will fail with a clear NotADirectoryError.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    bad_db_path = blocker / "idx.db"

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(bad_db_path))
    # Construction itself must not raise, and the index must have been
    # disabled rather than left half-initialized.
    assert svc._index is None or not svc._index.available
    result = svc.list_sessions()
    assert {r["session_id"] for r in result} == {"GG000001"}


# ── one bad root must not take down the rest (indexed path) ─────────────

def test_failing_root_does_not_lose_local_sessions_when_indexed(tmp_path, monkeypatch):
    local = tmp_path / "local"
    flaky = tmp_path / "cloud"
    _write(local, "HH000001")
    _write(local, "HH000002")
    _write(flaky, "HH000003")

    svc = SessionService(
        str(local), extra_dirs=[str(flaky)],
        index_enabled=True, index_db_path=str(tmp_path / "idx.db"))

    real_rglob = type(local).rglob

    def boom(self, pattern):
        if str(self).startswith(str(flaky)):
            raise OSError(5, "Input/output error")
        return real_rglob(self, pattern)

    monkeypatch.setattr(type(local), "rglob", boom)

    ids = {s["session_id"] for s in svc.list_sessions()}
    assert ids == {"HH000001", "HH000002"}
    assert any(str(flaky) in e["path"] for e in svc._last_root_errors)


# ── unreadable file: reported present, never silently dropped ───────────

def test_unreadable_file_row_is_not_deleted_from_the_index(tmp_path, monkeypatch):
    """A file that fails to read this round (cloud placeholder, corrupt
    mid-sync-write, transient I/O error) must never be treated as
    "deleted". The visible list_sessions() result for that round can
    omit it (same as a direct scan would), but its cached row in the
    index must survive untouched — proving the file is still tracked as
    present on disk, not silently forgotten."""
    root = tmp_path / "recordings"
    path = _write(root, "II000001", mtime=1_000_000)

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(tmp_path / "idx.db"))
    first = svc.list_sessions()
    assert len(first) == 1
    assert svc._index.row_count() == 1

    # Simulate the file changing on disk (new mtime) but becoming
    # unreadable this round — e.g. a cloud sync client mid-write.
    os.utime(path, (2_000_000, 2_000_000))
    orig_read = session_service_mod.read_text_hydrated

    def boom(p, **kw):
        if str(p) == str(path):
            raise OSError(5, "Input/output error")
        return orig_read(p, **kw)

    monkeypatch.setattr(session_service_mod, "read_text_hydrated", boom)

    second = svc.list_sessions()
    # Not readable this round -> absent from the visible list, exactly
    # like a direct scan would omit it.
    assert second == []
    # But the row itself was NEVER deleted — it's stale, not gone.
    assert svc._index.row_count() == 1

    # Once the read succeeds again, it reappears without needing a
    # special "recover" step — same session_index.db, plain next call.
    monkeypatch.setattr(session_service_mod, "read_text_hydrated", orig_read)
    third = svc.list_sessions()
    assert {r["session_id"] for r in third} == {"II000001"}


def test_unreadable_new_file_with_no_prior_row_is_simply_absent(tmp_path):
    """A brand-new file that fails to parse on its very first sighting
    has no prior row to preserve — it's correctly just not in the
    results yet, matching a direct scan's behaviour exactly."""
    root = tmp_path / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    (root / "session_JJ000001.json").write_text("{ not valid json", encoding="utf-8")

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(tmp_path / "idx.db"))
    assert svc.list_sessions() == []
    assert svc._index.row_count() == 0


# ── concurrency ──────────────────────────────────────────────────────────

def test_concurrent_reads_from_multiple_threads_do_not_error(tmp_path):
    root = tmp_path / "recordings"
    for i in range(20):
        _write(root, f"KK{i:06d}")

    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(tmp_path / "idx.db"))

    errors = []
    results = []
    lock = threading.Lock()

    def worker():
        try:
            r = svc.list_sessions()
            with lock:
                results.append(len(r))
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert results == [20] * 8


# ── kill switch ──────────────────────────────────────────────────────────

def test_kill_switch_bypasses_the_index_entirely(tmp_path, monkeypatch):
    root = tmp_path / "recordings"
    _write(root, "LL000001")

    svc = SessionService(
        str(root), index_enabled=False, index_db_path=str(tmp_path / "idx.db"))
    assert svc._index is None
    # No DB file should even get created when the kill switch is off.
    assert not (tmp_path / "idx.db").exists()

    calls = _spy_parses(monkeypatch)
    result = svc.list_sessions()
    assert {r["session_id"] for r in result} == {"LL000001"}
    assert len(calls) == 1  # direct scan: parses on every call, no cache
    calls.clear()
    svc.list_sessions()
    assert len(calls) == 1  # still parses every time -> confirms no caching


def test_no_db_path_means_no_index_even_if_enabled(tmp_path):
    root = tmp_path / "recordings"
    _write(root, "MM000001")
    svc = SessionService(str(root), index_enabled=True, index_db_path=None)
    assert svc._index is None
    assert {r["session_id"] for r in svc.list_sessions()} == {"MM000001"}


# ── SessionIndex unit-level behaviour ─────────────────────────────────────

def test_schema_version_mismatch_triggers_rebuild_not_migration(tmp_path):
    db_path = tmp_path / "idx.db"
    idx = SessionIndex(str(db_path))
    assert idx.available

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 999999")
    conn.commit()
    conn.close()

    idx2 = SessionIndex(str(db_path))
    assert idx2.available
    conn = sqlite3.connect(str(db_path))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    from services.session_index import SCHEMA_VERSION
    assert version == SCHEMA_VERSION
    # The table exists and is queryable (rebuilt, not migrated).
    assert idx2.row_count() == 0


def test_deleting_the_db_file_between_calls_is_safe(tmp_path):
    """Deleting session_index.db must be a completely safe operation —
    the next call just rebuilds from the JSON files on disk."""
    root = tmp_path / "recordings"
    _write(root, "NN000001")
    _write(root, "NN000002")

    db_path = tmp_path / "idx.db"
    svc = SessionService(
        str(root), index_enabled=True, index_db_path=str(db_path))
    assert len(svc.list_sessions()) == 2

    db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = tmp_path / f"idx.db{suffix}"
        if p.exists():
            p.unlink()

    # SessionIndex still has open connections pointing at the (now
    # gone) file; sqlite recreates it transparently on next write.
    result = svc.list_sessions()
    assert {r["session_id"] for r in result} == {"NN000001", "NN000002"}
