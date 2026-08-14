"""
A disposable SQLite cache of session_*.json metadata, keyed on (path,
mtime, size). Exists purely to make ``SessionService.list_sessions()``
fast when a user has hundreds of session files spread across several
roots (some on cloud-synced storage) — see the crash dump referenced in
``session_service.py`` for what unconditionally re-parsing every file on
every call did on a real machine.

THE JSON FILES ON DISK ARE THE ONLY SOURCE OF TRUTH. This module must
never become a second one:

  - Nothing here is ever written back to a session_*.json file. This
    class only ever *reads* file metadata (mtime/size) and caches the
    already-parsed summary fields SessionService computed from the JSON.
  - Deleting the .db file is always safe: the next refresh_and_query()
    call rebuilds every row from scratch by re-stating and re-parsing.
  - A corrupt/truncated/locked database degrades to
    ``SessionIndexUnavailable`` — the caller (SessionService) is
    expected to fall back to a direct disk scan, never to invent data.
  - A row is only ever removed when the caller's candidate set (i.e.
    what a fresh ``rglob`` of a *successfully scanned* root actually
    found) no longer contains its path. A file that exists but couldn't
    be *read* this round (un-hydrated cloud placeholder, transient I/O
    hiccup, corrupt JSON mid-sync-write) is never treated as "gone" —
    its row, if any, is left exactly as it was. This is the invariant
    that field reports have repeatedly caught this codebase missing
    elsewhere (see session_service.py's CloudFileNotReadyError notes);
    it is deliberately enforced here at the cache layer too.

The cache design is intentionally "dumb": callers hand over the full set
of currently-known (path -> (mtime, size, root)) tuples plus a
``parse_fn`` that turns a path into a fully-formed summary dict (or None
if it couldn't be read). Rows whose (mtime, size) haven't changed are
served straight from SQLite with zero JSON parsing; only new/changed
paths call back into ``parse_fn``. There is no separate "invalidate"
API — because every call re-supplies the full current candidate set,
a session created, edited, or deleted on disk is reflected on the very
next call, not on next restart.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1

# The exact summary shape SessionService.list_sessions() has always
# returned (see its docstring / the summary dict built in
# _build_summary). Stored verbatim as a JSON blob per row so there is
# zero ambiguity about types surviving the SQLite round-trip (bools,
# ints vs floats, None) — only the columns actually filtered/sorted on
# (session_id, root, started_at, client) get their own indexed column.
SUMMARY_FIELDS: Tuple[str, ...] = (
    "display_name", "started_at", "ended_at", "duration_s",
    "audio_path", "audio_exists",
    "has_transcript", "has_summary", "has_action_items",
    "has_requirements", "has_decisions",
    "client", "project", "action_items", "summary", "decisions",
    "requirements", "attendees", "speakers",
    "audio_integrity_warning", "audio_actual_duration_s",
    "audio_expected_duration_s", "processing_error", "sync_warning",
    "finalize_duration_s", "aec_outcome",
)


class SessionIndexUnavailable(Exception):
    """The index can't currently serve a query (missing/corrupt/locked
    DB, schema mismatch it couldn't self-heal, …). Callers must treat
    this as "use the direct scan for this call", not as an error to
    surface to the user — the index is disposable by design."""


class SessionIndex:
    """Thread-safe SQLite-backed cache. One instance per SessionService.

    Connection strategy: one sqlite3.Connection per thread (via
    threading.local — sqlite3 connections aren't safe to share across
    threads without check_same_thread=False, and this codebase runs
    handlers via ``asyncio.to_thread`` from a pool, so "per thread" is
    the natural fit). A single process-wide lock serializes the actual
    read-modify-write refresh so two threads racing on the same changed
    file can't both parse it and interleave conflicting writes; plain
    cache-hit reads elsewhere are not required to take it.
    """

    def __init__(self, db_path: "str | Path"):
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._ok = True
        try:
            with self._lock:
                self._ensure_schema_locked()
        except Exception as e:  # noqa: BLE001 - deliberately broad
            logger.warning(
                f"Session index at {self._db_path} unavailable at "
                f"startup ({e}); callers will fall back to a direct "
                f"disk scan until it recovers.")
            self._ok = False

    @property
    def available(self) -> bool:
        return self._ok

    # ── connection / schema management ──────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            # Some filesystems (network mounts, certain cloud-sync
            # clients) don't support WAL's shared-memory file. Fall
            # back to the connection's default journal mode rather
            # than failing the whole index over it.
            pass
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self._local.conn = conn
        return conn

    def _ensure_schema_locked(self) -> None:
        """Caller must hold self._lock."""
        conn = self._connect()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='sessions'"
        ).fetchone()
        if version != SCHEMA_VERSION or table is None:
            # Schema version mismatch (or first run, or a half-built
            # file): drop and rebuild rather than attempt a migration.
            # This is a cache — there is nothing in it worth preserving
            # across a schema change, and every row it would hold is
            # trivially recomputable from the JSON files on disk.
            self._create_schema_locked(conn)

    def _create_schema_locked(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS sessions")
        conn.execute(
            """
            CREATE TABLE sessions (
                path        TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                root        TEXT NOT NULL,
                mtime       REAL NOT NULL,
                size        INTEGER NOT NULL,
                started_at  TEXT,
                client      TEXT,
                project     TEXT,
                data_json   TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_sessions_session_id ON sessions(session_id)")
        conn.execute("CREATE INDEX idx_sessions_root ON sessions(root)")
        conn.execute(
            "CREATE INDEX idx_sessions_started_at ON sessions(started_at)")
        conn.execute("CREATE INDEX idx_sessions_client ON sessions(client)")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _recover_locked(self) -> None:
        """The DB file is corrupt/unreadable. It's disposable — nuke and
        rebuild from an empty file rather than attempting repair.
        Caller must hold self._lock."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = Path(str(self._db_path) + suffix)
            try:
                if p.exists():
                    p.unlink()
            except OSError as e:
                logger.warning(f"Could not remove stale index file {p}: {e}")
        self._create_schema_locked(self._connect())

    # ── the one real operation ───────────────────────────────────────

    def refresh_and_query(
        self,
        candidates: Dict[str, Tuple[float, int, str]],
        scanned_roots: Iterable[str],
        parse_fn: Callable[[str], Optional[dict]],
    ) -> List[Tuple[dict, float]]:
        """Bring the index in sync with `candidates` and return every
        summary that's now current.

        Args:
            candidates: {absolute_path_str: (mtime, size, root_str)} —
                every session_*.json this call found on disk, across
                every root that was successfully enumerated.
            scanned_roots: the subset of roots that were actually
                walked without error this call. Rows belonging to a
                root NOT in this set are left completely untouched
                (neither served nor pruned) — a root that failed to
                enumerate this round must not look like "everything in
                it just got deleted".
            parse_fn: given a path string, returns a fully-built
                summary dict, or None if the file couldn't be read this
                round (cloud placeholder, corrupt JSON, transient I/O
                error). A None result NEVER deletes an existing row.

        Returns:
            [(summary_dict, file_mtime), ...] for every path in
            `candidates` that has a valid cached-or-freshly-parsed row.
            A path with no prior row and a `parse_fn` failure this
            round simply doesn't appear — identical to what a direct
            scan would do with an unreadable file.

        Raises:
            SessionIndexUnavailable if the database can't be used at
            all (even after attempting self-recovery). Callers must
            catch this and fall back to a direct disk scan.
        """
        if not self._ok:
            raise SessionIndexUnavailable(
                f"session index at {self._db_path} is marked unavailable")
        try:
            return self._refresh_and_query(candidates, scanned_roots, parse_fn)
        except sqlite3.DatabaseError as e:
            # DatabaseError covers "file is not a database" (corruption)
            # among other things — worth one self-heal attempt before
            # giving up on the index for this process lifetime.
            logger.warning(
                f"Session index at {self._db_path} looked corrupt "
                f"({e}); rebuilding from scratch.")
            try:
                with self._lock:
                    self._recover_locked()
                return self._refresh_and_query(
                    candidates, scanned_roots, parse_fn)
            except Exception as e2:  # noqa: BLE001
                self._ok = False
                raise SessionIndexUnavailable(str(e2)) from e2
        except sqlite3.Error as e:
            # Locked, disk full, permission denied, etc. — not
            # something a rebuild fixes. Disable the index for this
            # process; SessionService falls back to direct scans.
            self._ok = False
            raise SessionIndexUnavailable(str(e)) from e

    def _refresh_and_query(
        self,
        candidates: Dict[str, Tuple[float, int, str]],
        scanned_roots: Iterable[str],
        parse_fn: Callable[[str], Optional[dict]],
    ) -> List[Tuple[dict, float]]:
        conn = self._connect()
        roots_list = list(dict.fromkeys(scanned_roots))  # de-dup, keep order
        with self._lock:
            existing: Dict[str, sqlite3.Row] = {}
            if roots_list:
                placeholders = ",".join("?" for _ in roots_list)
                cur = conn.execute(
                    f"SELECT * FROM sessions WHERE root IN ({placeholders})",
                    roots_list,
                )
                for row in cur.fetchall():
                    existing[row["path"]] = row

            results: List[Tuple[dict, float]] = []
            to_upsert: List[tuple] = []
            for path, (mtime, size, root) in candidates.items():
                row = existing.get(path)
                if (row is not None
                        and row["mtime"] == mtime
                        and row["size"] == size):
                    results.append((self._row_to_summary(row), mtime))
                    continue

                summary = parse_fn(path)
                if summary is None:
                    # Could not read/parse this round. Per the class
                    # docstring: never treat this as "file is gone".
                    # Leave any existing row exactly as-is (stale but
                    # present beats vanished) and simply don't offer a
                    # fresh summary for it this round — matches what a
                    # direct scan does with an unreadable file.
                    continue

                session_id = (
                    summary.get("session_id")
                    or Path(path).stem.replace("session_", ""))
                to_upsert.append(
                    self._make_row(path, session_id, root, mtime, size, summary))
                # Match _row_to_summary's shape exactly (cache hits and
                # freshly-parsed rows must be indistinguishable to the
                # caller) — json_path isn't one of SUMMARY_FIELDS, it's
                # derived from the row's primary key.
                summary_out = dict(summary)
                summary_out["json_path"] = path
                results.append((summary_out, mtime))

            # Prune only within roots we actually scanned this round —
            # a path that used to be there and now isn't (within a
            # HEALTHY root) is a genuine deletion.
            stale = [p for p in existing if p not in candidates]

            if to_upsert or stale:
                # One explicit transaction for the whole write batch.
                # isolation_level=None puts the connection in autocommit
                # mode, so without this a couple hundred individual
                # INSERT/DELETE statements would each be its own commit
                # (and, under WAL, its own fsync) — the exact kind of
                # per-row overhead that would eat the whole point of
                # caching on a cold index.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if to_upsert:
                        conn.executemany(
                            "INSERT OR REPLACE INTO sessions "
                            "(path, session_id, root, mtime, size, "
                            "started_at, client, project, data_json) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            to_upsert,
                        )
                    if stale:
                        conn.executemany(
                            "DELETE FROM sessions WHERE path = ?",
                            [(p,) for p in stale],
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

            return results

    # ── row <-> summary dict conversion ──────────────────────────────

    @staticmethod
    def _make_row(
        path: str, session_id: str, root: str, mtime: float, size: int,
        summary: dict,
    ) -> tuple:
        data_json = json.dumps({f: summary.get(f) for f in SUMMARY_FIELDS})
        return (
            path, session_id, root, mtime, size,
            summary.get("started_at"),
            summary.get("client") or "",
            summary.get("project") or "",
            data_json,
        )

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> dict:
        payload = json.loads(row["data_json"])
        summary = {"session_id": row["session_id"]}
        summary.update(payload)
        summary["json_path"] = row["path"]
        return summary

    # ── maintenance / diagnostics ────────────────────────────────────

    def row_count(self) -> int:
        """Number of cached rows. Diagnostic / test use only."""
        try:
            conn = self._connect()
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        except sqlite3.Error:
            return 0
