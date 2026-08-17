"""
Persists and loads session data as JSON.
Uses atomic write (temp file + rename) to prevent corrupt JSON on crash.
"""

import datetime
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

from models.session import Session
from services._cloud_sync import CloudFileNotReadyError, read_text_hydrated
from services.session_index import SessionIndex
from utils.logger import get_logger

logger = get_logger(__name__)


class SessionService:
    """Handles JSON serialization of Session objects.

    list_sessions() perf note: the session_*.json files on disk are the
    ONLY source of truth. When `index_db_path` is given (and
    `index_enabled` is True — the default), list_sessions() is served
    from a disposable SQLite cache (services/session_index.py) that
    re-stats every file on every call but only re-parses JSON for files
    whose (mtime, size) changed. Deleting that .db file, running with a
    corrupt one, or passing index_enabled=False all degrade to exactly
    the direct-scan behaviour this class has always had — see
    _list_sessions_direct(). The index is NEVER the only place a piece
    of session data lives.
    """

    def __init__(
        self,
        recordings_dir: str,
        extra_dirs=None,
        index_enabled: bool = True,
        index_db_path: Optional[str] = None,
    ):
        self._recordings_dir = Path(recordings_dir)
        self._recordings_dir.mkdir(parents=True, exist_ok=True)
        # Additional roots to READ sessions from (never written to).
        #
        # Field report 2026-08-07: a user with hundreds of recordings saw
        # ~15 in the app. Cause was structural, not data loss — scanning
        # was a single non-recursive glob over exactly one directory, so
        # every session recorded while RECORDINGS_DIR pointed somewhere
        # else stayed on disk and permanently invisible. Nothing migrated
        # them and nothing reported them missing.
        #
        # Reads now span the primary dir plus any extra roots, and recurse
        # into subfolders. Writes still go only to _recordings_dir, so the
        # extra roots are strictly an archive view.
        self._extra_dirs = [Path(d) for d in (extra_dirs or []) if str(d).strip()]
        # Roots that failed during the most recent list_sessions() walk.
        # Surfaced through scan_report() so an unreachable cloud folder
        # is visible rather than inferred from a short session list.
        self._last_root_errors: list = []

        # Kill switch (Settings.session_index_enabled, default True) plus
        # a concrete DB path are both required for the index to engage.
        # No path -> no index, full stop; nothing here ever guesses a
        # location on its own (server.py always passes USER_DATA_DIR /
        # "session_index.db" — next to config.env and the log files,
        # deliberately NEVER inside a recordings/archive root, which can
        # be cloud-synced and would corrupt a SQLite file living on it).
        self._index: Optional[SessionIndex] = None
        if index_enabled and index_db_path:
            try:
                self._index = SessionIndex(index_db_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Could not initialize session index at "
                    f"{index_db_path} ({e}); falling back to direct "
                    f"disk scans.")
                self._index = None

    @property
    def recordings_dir(self) -> Path:
        """Public read-only accessor — used by SearchService to find
        per-session embedding sidecar files (session_<id>.embeddings.npz
        / .embeddings.json)."""
        return self._recordings_dir

    def scan_roots(self) -> List[Path]:
        """Public accessor for the resolved, de-duplicated set of
        directories sessions are read from (primary + archive roots).

        Used by server.py to confine paths pulled out of a session's
        JSON (audio_path, screenshot paths) to trusted directories
        before serving them — session JSON files are synced between
        machines through cloud storage, so they are not fully trusted
        local input."""
        return self._scan_roots()

    def _scan_roots(self) -> List[Path]:
        """Every directory sessions are READ from, de-duplicated and
        with nested roots collapsed (a root inside another root would
        otherwise double-count under the recursive walk)."""
        roots: List[Path] = []
        seen = set()
        # Primary first, deliberately: ordering decides whose sessions
        # survive if a later root misbehaves (see list_sessions()).
        for d in [self._recordings_dir, *self._extra_dirs]:
            try:
                rd = d.expanduser().resolve()
            except OSError as e:
                logger.warning(f"Skipping unresolvable root {d}: {e}")
                continue
            key = str(rd).lower()
            if key in seen:
                continue
            try:
                if not rd.is_dir():
                    continue
            except OSError as e:
                # An offline network mount can raise rather than return
                # False. Treat it as absent, never as fatal.
                logger.warning(f"Root {rd} not reachable ({e}); skipping")
                continue
            seen.add(key)
            roots.append(rd)
        # Drop any root contained within another to avoid double-walking.
        pruned: List[Path] = []
        for r in roots:
            if any(r != o and o in r.parents for o in roots):
                continue
            pruned.append(r)
        return pruned

    def scan_report(self) -> dict:
        """Where sessions were found and what got skipped.

        Exists because every skip below is silent: an un-hydrated cloud
        placeholder, a truncated JSON, a non-dict file — each is logged
        and dropped, which renders identically to "you have no sessions".
        That ambiguity has now caused three separate field reports, so
        the counts are exposed rather than only logged.

        `primary_dir` and `visible_in_app` live here rather than in the
        /sessions/diagnostics endpoint handler (field report 2026-08-10:
        a user with 74 session files on disk saw 24 in the app and the
        only way to find out why was a PowerShell script, because this
        data existed internally but the endpoint that read it tacked
        `primary_dir`/`visible_in_app` on after the fact instead of
        scan_report() owning them — a second call site that could drift
        out of sync with what list_sessions() actually does). One
        method computes the full picture; the endpoint is a thin
        pass-through.
        """
        report = {"roots": [], "total": 0, "skipped": 0,
                  "skipped_detail": [], "unreachable_roots": [],
                  "primary_dir": str(self._recordings_dir),
                  "visible_in_app": 0}
        for root in self._scan_roots():
            found = 0
            try:
                candidates = list(root.rglob("session_*.json"))
            except OSError as e:
                # Same isolation rule as list_sessions(): report the
                # failed root, keep scanning the rest.
                report["unreachable_roots"].append(
                    {"path": str(root), "error": str(e)})
                report["roots"].append({"path": str(root),
                                        "session_files": 0,
                                        "unreachable": True})
                continue
            for path in candidates:
                if "." in path.stem:
                    continue
                found += 1
                try:
                    data = json.loads(read_text_hydrated(path))
                    if not isinstance(data, dict):
                        raise ValueError("root is not a JSON object")
                except CloudFileNotReadyError:
                    report["skipped"] += 1
                    report["skipped_detail"].append(
                        {"path": str(path), "reason": "cloud placeholder not downloaded"})
                except Exception as e:
                    report["skipped"] += 1
                    report["skipped_detail"].append(
                        {"path": str(path), "reason": str(e)})
            report["roots"].append({"path": str(root), "session_files": found,
                                    "unreachable": False})
            report["total"] += found
        # Keep the payload bounded — the count is what matters.
        report["skipped_detail"] = report["skipped_detail"][:50]
        # What the user actually sees in the Sessions list, computed the
        # exact same way the Sessions view does (same dedupe-by-mtime
        # rule) so this number can never drift from list_sessions()'s.
        report["visible_in_app"] = len(self.list_sessions())
        return report

    def save(self, session: Session) -> str:
        """
        Serialize a session to a JSON file using an atomic write.

        Writes to a temporary file first, then renames it to the final path.
        This ensures the target file is never left in a half-written state
        if the process is interrupted mid-write.

        Args:
            session: The completed Session object.

        Returns:
            The path of the saved JSON file.

        Raises:
            OSError: If writing or renaming fails.
        """
        final_path = self._recordings_dir / f"session_{session.session_id}.json"
        data = json.dumps(session.to_dict(), indent=2, ensure_ascii=False)

        # FIX #10: write to temp file in same directory, then atomic rename
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self._recordings_dir,
                suffix=".json.tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())  # Flush OS buffers before rename
                os.replace(tmp_path, final_path)  # Atomic on POSIX & Windows
            except Exception:
                # Clean up temp file if rename or write fails
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            raise OSError(f"Failed to save session {session.session_id}: {e}") from e

        logger.info(f"Session atomically saved: {final_path}")
        return str(final_path)

    def _resolve_json(self, session_id: str) -> Optional[Path]:
        """Find the session_<id>.json file across every root, preferring
        the primary dir's normal (non-recursive) location for the common
        case and otherwise the newest copy across all roots.

        Field report 2026-08-07: list_sessions() was made multi-root and
        recursive, but load() still hard-coded
        ``self._recordings_dir / f"session_{id}.json"`` — a session that
        list_sessions() found only under an archive root (or nested in a
        primary-dir subfolder) would 404 the moment a user clicked it,
        even though it was right there in the list. The two methods MUST
        agree on which copy is canonical when a session exists in more
        than one place, so this uses the exact same newest-mtime rule
        list_sessions() uses for its cross-root dedupe — otherwise the
        list shows one version and clicking it opens another.
        """
        target_name = f"session_{session_id}.json"
        best_path: Optional[Path] = None
        best_mtime = -1.0
        for root in self._scan_roots():
            for path in root.rglob(target_name):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > best_mtime:
                    best_mtime = mtime
                    best_path = path
        return best_path

    def load(self, session_id: str) -> Optional[dict]:
        """Load a session JSON by ID. Returns raw dict.

        Reads with ``utf-8-sig`` so a leading UTF-8 BOM (which PowerShell's
        ``Set-Content -Encoding UTF8`` emits by default, and which external
        recovery scripts therefore tend to inadvertently produce) doesn't
        cause a silent skip in ``list_sessions()``. Field repro
        2026-06-15: a recovered session was invisible in the UI because
        the recovery script wrote the JSON with a BOM, and the previous
        ``open(... encoding='utf-8')`` here raised JSONDecodeError on the
        BOM byte. ``utf-8-sig`` is a strict superset — files without a
        BOM still decode identically.

        Resolves across every root (see _resolve_json) — a session that
        exists only under an archive root must still load, not 404."""
        path = self._resolve_json(session_id)
        if path is None:
            logger.warning(
                f"Session file not found for {session_id} in any root")
            return None
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt session file {path}: {e}") from e

    def load_full(self, session_id: str) -> Optional[Session]:
        """Load a session and rebuild the full Session object."""
        data = self.load(session_id)
        if data is None:
            return None
        return Session.from_dict(data)

    def _build_summary(self, session_id: str, data: dict) -> dict:
        """The list_sessions() summary shape for one already-parsed
        session JSON dict. Pure function of (session_id, data) — no I/O
        — so it's shared verbatim by the direct scan and by the index's
        parse callback, and both are guaranteed to produce identical
        summaries for identical file content."""
        audio_path = data.get("audio_path")
        audio_exists = bool(audio_path) and Path(audio_path).exists()

        # Duration from started/ended_at
        duration_s = 0
        try:
            started = data.get("started_at")
            ended = data.get("ended_at")
            if started and ended:
                s = datetime.datetime.fromisoformat(started)
                e = datetime.datetime.fromisoformat(ended)
                duration_s = max(0, int((e - s).total_seconds()))
        except Exception:
            pass

        return {
            "session_id": session_id,
            "display_name": data.get("display_name") or f"Session {session_id}",
            "started_at": data.get("started_at"),
            "ended_at": data.get("ended_at"),
            "duration_s": duration_s,
            "audio_path": audio_path,
            "audio_exists": audio_exists,
            "has_transcript": bool(data.get("segments")),
            "has_summary": bool(data.get("summary")),
            "has_action_items": bool(data.get("action_items")),
            "has_requirements": bool(data.get("requirements")),
            "has_decisions": bool(data.get("decisions")),
            "client": data.get("client", "") or "",
            "project": data.get("project", "") or "",
            "action_items": data.get("action_items", "") or "",
            "summary": data.get("summary", "") or "",
            "decisions": data.get("decisions", "") or "",
            "requirements": data.get("requirements", "") or "",
            # Attendees are lightweight (email strings) and let the
            # Record view auto-tag client from the current calendar
            # meeting's attendees without an extra per-session fetch.
            "attendees": list(data.get("attendees") or []),
            # Compact map of speaker_id -> display_name so list-side
            # consumers (Follow-ups, Commitments) can resolve labels
            # like "SPEAKER_03" that the LLM extracted into a typed
            # human name when the user has renamed that speaker.
            # Omitting empty display_names so the wire is small —
            # the resolver treats a missing key as "no rename yet".
            "speakers": {
                sid: sd.get("display_name", "")
                for sid, sd in (data.get("speakers") or {}).items()
                if sd.get("display_name")
            },
            # Audio integrity flags — set when WAV duration disagrees
            # significantly with the recording window. Lets the
            # Sessions list render a warning chip without loading
            # the full session JSON.
            "audio_integrity_warning":
                data.get("audio_integrity_warning"),
            "audio_actual_duration_s":
                data.get("audio_actual_duration_s"),
            "audio_expected_duration_s":
                data.get("audio_expected_duration_s"),
            # Auto-process failure (retries exhausted). Lets the
            # Sessions list badge a failed session so it doesn't sit
            # silently unprocessed.
            "processing_error": data.get("processing_error"),
            # Read-only sync-integrity finding (mic/loopback drift or
            # dropped frames vs wall-clock). Surfaced as an info chip.
            "sync_warning": data.get("sync_warning"),
            # How long finalize (WAV merge + optional AEC) took, kept
            # separate from duration_s (the capture window) so a slow
            # post-process never reads as missing audio. See
            # Session.finalize_duration_s.
            "finalize_duration_s": data.get("finalize_duration_s"),
            # Offline AEC decision for this session's finalize, if the
            # toggle was on. See Session.aec_outcome for the shape.
            "aec_outcome": data.get("aec_outcome"),
            # Finalize-in-progress state — lets the Sessions list show
            # "Finalizing…" / "Finalize failed" without loading the full
            # session JSON, and lets recovery_service's startup sweep
            # find every "finalizing" session by scanning summaries
            # instead of every session's full JSON. See
            # Session.finalize_status for the three-state contract.
            "finalize_status": data.get("finalize_status"),
            "finalize_started_at": data.get("finalize_started_at"),
            "finalize_error": data.get("finalize_error"),
        }

    def _read_and_parse(self, path: Path) -> Optional[dict]:
        """Read + parse one session_*.json into a summary dict (see
        _build_summary), or None if it couldn't be read this round.

        None deliberately does NOT distinguish "cloud placeholder not
        hydrated yet" from "corrupt JSON" from "transient I/O error" —
        callers (direct scan, and the index's parse_fn) both treat every
        such case identically: skip this file for this pass, log why,
        never crash the whole listing over one bad file. Does not touch
        json_path — callers attach that themselves, since the index
        already tracks it as the row's primary key.
        """
        try:
            data = json.loads(read_text_hydrated(path))
        except CloudFileNotReadyError as e:
            # Synced from another device but not downloaded to this
            # disk yet — the "new session created today doesn't show
            # up on the Mac" bug. Log loud + actionable instead of a
            # quiet skip so the cause is obvious.
            logger.error(
                f"Session {path.name} is an un-downloaded cloud "
                f"placeholder; it will appear once the sync client "
                f"finishes downloading it. {e}")
            return None
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Skipping unreadable session {path.name}: {e}")
            return None
        if not isinstance(data, dict):
            # Belt-and-braces: anything that loads as valid JSON but
            # isn't a dict (legacy list-rooted file, future sidecar
            # we forgot to add to the dotted-stem skip above) gets
            # skipped instead of 500ing the whole endpoint.
            logger.warning(
                f"Skipping non-dict session file {path.name} "
                f"(root is {type(data).__name__}, expected dict)")
            return None

        session_id = data.get("session_id") or path.stem.replace("session_", "")
        return self._build_summary(session_id, data)

    @staticmethod
    def _dedupe_newest(rows) -> List[dict]:
        """Same session id can legitimately appear under two roots (an
        old recordings dir plus a copy in the current one). Keep the
        newest by file mtime so the app shows the most-processed
        version. `rows` is an iterable of (summary_dict, mtime)."""
        results: List[dict] = []
        seen_ids: dict = {}
        for summary, mtime in rows:
            session_id = summary["session_id"]
            prior = seen_ids.get(session_id)
            if prior is not None and prior[0] >= mtime:
                continue
            if prior is not None:
                # Same id seen in another root; this copy is newer, so it
                # replaces the earlier one in place rather than appending
                # a duplicate row.
                results[prior[1]] = summary
                seen_ids[session_id] = (mtime, prior[1])
            else:
                seen_ids[session_id] = (mtime, len(results))
                results.append(summary)
        results.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return results

    def list_sessions(self) -> List[dict]:
        """
        Scan every configured root for session_*.json files.
        Returns a list of summaries sorted newest first.

        Recursive and multi-root as of 2026-08-07 (see __init__): a
        single non-recursive glob over one directory hid every session
        recorded before RECORDINGS_DIR was last changed, and every
        session filed into a subfolder.

        Served from the SQLite index when one is configured (see
        __init__ / services/session_index.py). Any failure there —
        missing DB, corrupt file, locked file, schema mismatch it
        couldn't self-heal — falls back to a full direct disk scan for
        that call, never to an error or to stale/wrong data.
        """
        if self._index is not None:
            try:
                return self._list_sessions_indexed()
            except Exception as e:  # noqa: BLE001 - the index is best-effort
                logger.warning(
                    f"Session index read failed ({e}); falling back to "
                    f"a direct disk scan for this call.")
        return self._list_sessions_direct()

    def _list_sessions_direct(self) -> List[dict]:
        """The original always-correct path: walk every root, parse
        every session_*.json, build summaries. No caching."""
        # ONE BAD ROOT MUST NEVER TAKE DOWN THE WHOLE LIST.
        #
        # Field report 2026-08-07: a user configured a Google Drive
        # folder as their Session Archive and their Windows library
        # collapsed from 73 sessions to a handful. Nothing was deleted
        # and the local files were fine — this loop was
        # `paths.extend(root.rglob(...))` with no error handling, so a
        # single OSError from the Drive mount (stream hiccup, transient
        # permission failure, disconnect — routine on cloud filesystems)
        # propagated straight out of list_sessions(), 500'd GET
        # /sessions, and took the LOCAL sessions down with it.
        #
        # Roots are now isolated: a root that fails contributes nothing
        # and is recorded, while every other root still returns its
        # sessions. The primary recordings dir is walked first so a
        # flaky network root can never cost the user their local
        # library. archive_reconcile._scan_session_json already had this
        # guard; this one didn't, and that asymmetry is what shipped.
        self._last_root_errors = []
        paths = []
        for root in self._scan_roots():
            try:
                paths.extend(root.rglob("session_*.json"))
            except OSError as e:
                self._last_root_errors.append({"path": str(root),
                                               "error": str(e)})
                logger.error(
                    f"Session scan of {root} failed ({e}); sessions in "
                    f"other folders are unaffected. If this is a cloud "
                    f"folder, check the sync client is running.")

        rows = []
        for path in paths:
            # Skip sidecar files (session_<id>.commitments.json,
            # session_<id>.item_status.json, …) that share the
            # session_*.json glob but aren't canonical session records.
            # Canonical session IDs are dotless hex; a dot in the stem
            # means a sidecar suffix.
            if "." in path.stem:
                continue
            summary = self._read_and_parse(path)
            if summary is None:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            summary["json_path"] = str(path)
            rows.append((summary, mtime))

        return self._dedupe_newest(rows)

    def _list_sessions_indexed(self) -> List[dict]:
        """Same result as _list_sessions_direct(), but unchanged files
        are served from the SQLite cache instead of being re-parsed.

        Every call re-enumerates the filesystem (stat only — cheap) so
        a session created/edited/deleted on disk is reflected on the
        very next call; there's no separate cache-invalidation path to
        keep in sync with save()/delete()."""
        self._last_root_errors = []
        scanned_roots: List[str] = []
        candidates: dict = {}
        for root in self._scan_roots():
            root_str = str(root)
            try:
                found = list(root.rglob("session_*.json"))
            except OSError as e:
                self._last_root_errors.append({"path": root_str,
                                               "error": str(e)})
                logger.error(
                    f"Session scan of {root} failed ({e}); sessions in "
                    f"other folders are unaffected. If this is a cloud "
                    f"folder, check the sync client is running.")
                continue
            scanned_roots.append(root_str)
            for path in found:
                if "." in path.stem:
                    continue
                try:
                    st = path.stat()
                except OSError:
                    # Can't stat it right now (transient) — skip it as a
                    # candidate this round rather than guessing; any
                    # cached row for it is left untouched (see
                    # SessionIndex.refresh_and_query).
                    continue
                candidates[str(path)] = (st.st_mtime, st.st_size, root_str)

        rows = self._index.refresh_and_query(
            candidates, scanned_roots,
            lambda p: self._read_and_parse(Path(p)),
        )
        return self._dedupe_newest(rows)

    def delete(self, session_id: str) -> None:
        """Delete a session's JSON, audio, log, and search/derived
        sidecars — from EVERY root, not just the primary dir.

        Field report 2026-08-07 (shipped bug): the original delete()
        only removed (".json", ".wav", ".log") from self._recordings_dir.
        Two consequences once multi-root discovery landed:

          (a) The .embeddings.* / .commitments.json / .item_status.json
              sidecars were never cleaned up, so a deleted session's
              chunks stayed live in SearchService's in-memory index and
              Q&A kept citing a session_id that now 404s.
          (b) A copy of the same session in an extra/archive root
              survived the delete and resurrected the session in
              list_sessions() the next time it scanned.

        Embedding sidecars used to be a single ".embeddings.pkl"; since
        the pickle-to-npz/json migration a session may have
        ".embeddings.npz" + ".embeddings.json" (current format), a
        leftover ".embeddings.pkl" (pre-migration, never loaded — see
        services/search_service.py), or both if a rebuild ran before the
        old file was cleaned up. Delete handles all three so nothing —
        current or legacy — survives a delete.

        Best-effort per file: one locked/unreadable file must not stop
        the rest from being cleaned up.
        """
        primary_suffixes = (
            ".json", ".wav", ".log",
            ".embeddings.npz", ".embeddings.json", ".embeddings.pkl",
            ".commitments.json", ".item_status.json",
            ".channel_attribution.json",
        )
        for suffix in primary_suffixes:
            p = self._recordings_dir / f"session_{session_id}{suffix}"
            if p.exists():
                try:
                    p.unlink()
                    logger.info(f"Deleted {p.name}")
                except OSError as e:
                    logger.warning(f"Could not delete {p}: {e}")

        # Non-primary roots: audio never lives here (writes only ever go
        # to the primary dir — see __init__), but JSON + sidecars can,
        # via the roaming archive or an old RECORDINGS_DIR. rglob so a
        # copy filed into a subfolder is still found.
        sidecar_names = {
            f"session_{session_id}.json",
            f"session_{session_id}.log",
            f"session_{session_id}.embeddings.npz",
            f"session_{session_id}.embeddings.json",
            f"session_{session_id}.embeddings.pkl",
            f"session_{session_id}.commitments.json",
            f"session_{session_id}.item_status.json",
            f"session_{session_id}.channel_attribution.json",
        }
        try:
            primary_resolved = self._recordings_dir.expanduser().resolve()
        except OSError:
            primary_resolved = self._recordings_dir
        for root in self._scan_roots():
            if root == primary_resolved:
                continue
            for name in sidecar_names:
                for p in root.rglob(name):
                    try:
                        p.unlink()
                        logger.info(f"Deleted {p} (extra root)")
                    except OSError as e:
                        logger.warning(f"Could not delete {p}: {e}")

    def import_from_file(
        self,
        source_path: str,
        display_name: str = "",
        client: str = "",
        project: str = "",
    ) -> Session:
        """
        Import an existing audio file (WAV) as a new session.

        Copies (doesn't move) the source into the recordings directory
        using the standard `session_<id>.wav` naming, creates a Session
        with metadata from the file, and writes the session JSON. The
        returned session has no transcript/summary yet — the user will
        run processing from the UI like any freshly-recorded session.
        """
        src = Path(source_path)
        if not src.exists():
            raise FileNotFoundError(f"File not found: {source_path}")
        if src.suffix.lower() not in (
                ".wav", ".mp3", ".m4a", ".flac", ".mp4", ".mov"):
            raise ValueError(
                f"Unsupported audio format: {src.suffix}. "
                "Use .wav, .mp3, .m4a, .flac, .mp4, or .mov.")

        session_id = uuid.uuid4().hex[:8].upper()
        # Keep the original extension so downstream tooling doesn't
        # assume .wav when the user imported, say, an .m4a from Teams.
        dst = self._recordings_dir / f"session_{session_id}{src.suffix.lower()}"
        shutil.copy2(src, dst)

        session = Session(session_id=session_id)
        session.display_name = (display_name or src.stem).strip()
        session.started_at = datetime.datetime.fromtimestamp(src.stat().st_mtime)
        session.ended_at = session.started_at
        session.audio_path = str(dst)
        session.client = client
        session.project = project
        self.save(session)
        logger.info(f"Imported external file {src.name} as session {session_id}")
        return session
