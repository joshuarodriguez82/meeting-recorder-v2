"""Session discovery across roots and subfolders.

Field report 2026-08-07: a user with hundreds of recordings saw ~15 in
the app. Nothing was lost — discovery was a single non-recursive glob
over one directory, so sessions recorded while RECORDINGS_DIR pointed
somewhere else, or filed into a subfolder, were permanently invisible.
"""

import json

from services.session_service import SessionService


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
        import os
        os.utime(p, (mtime, mtime))
    return p


def test_finds_sessions_in_subfolders(tmp_path):
    """A single non-recursive glob was the original bug."""
    root = tmp_path / "recordings"
    _write(root, "AA01")
    _write(root / "2025", "AA02")
    _write(root / "2025" / "archive", "AA03")

    svc = SessionService(str(root))
    ids = {s["session_id"] for s in svc.list_sessions()}
    assert ids == {"AA01", "AA02", "AA03"}


def test_finds_sessions_in_extra_roots(tmp_path):
    """Sessions recorded under a previous RECORDINGS_DIR stay reachable."""
    current = tmp_path / "current"
    old = tmp_path / "old-library"
    _write(current, "BB01")
    _write(old, "BB02")
    _write(old / "nested", "BB03")

    svc = SessionService(str(current), extra_dirs=[str(old)])
    ids = {s["session_id"] for s in svc.list_sessions()}
    assert ids == {"BB01", "BB02", "BB03"}


def test_same_session_in_two_roots_is_not_duplicated(tmp_path):
    """An old dir and the current one can hold the same session; show
    one row, and prefer the newer (more-processed) copy."""
    current = tmp_path / "current"
    old = tmp_path / "old"
    _write(old, "CC01", name="stale", summary="", mtime=1_000_000)
    _write(current, "CC01", name="fresh", summary="done", mtime=2_000_000)

    svc = SessionService(str(current), extra_dirs=[str(old)])
    rows = svc.list_sessions()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "fresh"
    assert rows[0]["has_summary"] is True


def test_older_copy_does_not_override_newer_regardless_of_scan_order(tmp_path):
    """Same as above with the mtimes swapped, so the result can't be an
    artifact of which root happens to be walked first."""
    current = tmp_path / "current"
    old = tmp_path / "old"
    _write(old, "DD01", name="newer", summary="done", mtime=2_000_000)
    _write(current, "DD01", name="older", summary="", mtime=1_000_000)

    svc = SessionService(str(current), extra_dirs=[str(old)])
    rows = svc.list_sessions()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "newer"


def test_nested_extra_root_is_not_double_counted(tmp_path):
    """An archive root inside the primary dir must not yield two rows
    for the same file under the recursive walk."""
    root = tmp_path / "recordings"
    inner = root / "archive"
    _write(root, "EE01")
    _write(inner, "EE02")

    svc = SessionService(str(root), extra_dirs=[str(inner)])
    rows = svc.list_sessions()
    assert len({r["session_id"] for r in rows}) == len(rows) == 2


def test_sidecars_still_ignored(tmp_path):
    root = tmp_path / "recordings"
    _write(root, "FF01")
    (root / "session_FF01.commitments.json").write_text("{}", encoding="utf-8")
    (root / "session_FF01.item_status.json").write_text("{}", encoding="utf-8")

    svc = SessionService(str(root))
    assert [s["session_id"] for s in svc.list_sessions()] == ["FF01"]


def test_missing_extra_root_is_ignored_not_fatal(tmp_path):
    root = tmp_path / "recordings"
    _write(root, "GG01")
    svc = SessionService(str(root), extra_dirs=[str(tmp_path / "does-not-exist")])
    assert len(svc.list_sessions()) == 1


# ── scan_report: skips become visible ─────────────────────────────────

def test_scan_report_counts_per_root(tmp_path):
    current = tmp_path / "current"
    old = tmp_path / "old"
    _write(current, "HH01")
    _write(old, "HH02")
    _write(old / "deep", "HH03")

    svc = SessionService(str(current), extra_dirs=[str(old)])
    rep = svc.scan_report()
    assert rep["total"] == 3
    assert rep["skipped"] == 0
    by_path = {r["path"]: r["session_files"] for r in rep["roots"]}
    assert sum(by_path.values()) == 3
    assert len(rep["roots"]) == 2


def test_scan_report_surfaces_unreadable_files(tmp_path):
    """The whole point: a file we couldn't read must be COUNTED, not
    silently dropped so it reads as 'you have no sessions'."""
    root = tmp_path / "recordings"
    _write(root, "II01")
    (root / "session_II02.json").write_text("{ this is not json", encoding="utf-8")
    (root / "session_II03.json").write_text('["a list, not an object"]', encoding="utf-8")

    svc = SessionService(str(root))
    rep = svc.scan_report()
    assert rep["total"] == 3          # three candidate files on disk
    assert rep["skipped"] == 2        # two of them unusable
    assert len(svc.list_sessions()) == 1
    reasons = " ".join(d["reason"] for d in rep["skipped_detail"])
    assert reasons  # each skip carries a reason


# ── roaming archive (three-location rule) ─────────────────────────────

def test_archive_root_is_scanned_alongside_local(tmp_path):
    """A Mac and a Windows box each record locally but share a synced
    archive folder; both must see one merged library."""
    local = tmp_path / "local"
    archive = tmp_path / "synced"
    _write(local, "LOC01")
    _write(archive, "MAC01")
    _write(archive, "MAC02")

    svc = SessionService(str(local), extra_dirs=[str(archive)])
    ids = {s["session_id"] for s in svc.list_sessions()}
    assert ids == {"LOC01", "MAC01", "MAC02"}


def test_local_copy_wins_when_newer_than_archive(tmp_path):
    """This machine just processed a session the archive has an older
    copy of; the local, more-complete one must be what's shown."""
    local = tmp_path / "local"
    archive = tmp_path / "synced"
    _write(archive, "SH01", name="from-archive", summary="", mtime=1_000_000)
    _write(local, "SH01", name="just-processed", summary="full", mtime=2_000_000)

    svc = SessionService(str(local), extra_dirs=[str(archive)])
    rows = svc.list_sessions()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "just-processed"
    assert rows[0]["has_summary"] is True


def test_archive_copy_wins_when_newer_than_local(tmp_path):
    """The other machine processed it more recently — its version wins,
    so a stale local stub can't mask a finished summary."""
    local = tmp_path / "local"
    archive = tmp_path / "synced"
    _write(local, "SH02", name="stale-stub", summary="", mtime=1_000_000)
    _write(archive, "SH02", name="processed-elsewhere", summary="full", mtime=2_000_000)

    svc = SessionService(str(local), extra_dirs=[str(archive)])
    rows = svc.list_sessions()
    assert len(rows) == 1
    assert rows[0]["display_name"] == "processed-elsewhere"
    assert rows[0]["has_summary"] is True
