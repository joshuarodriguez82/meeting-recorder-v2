"""Reconciliation for the roaming Session Archive (SESSION_ARCHIVE_DIR).

Same shape as services/export_reconcile.py, one level simpler: the
archive holds exactly one artifact per session (the session JSON, plus
sidecars that ride along with it), not five independently-earned
artifacts. So there's no "expected" computation — just "is the local
file newer than what's in the archive."

Field report 2026-08-07 (bug 3): _reconcile_archive() used to inline
this comparison directly against a non-recursive glob, while
list_sessions() had already become recursive — a session filed into a
subfolder of the primary dir would show up locally but never reach the
roaming archive. The comparison also had no way to be queried without
actually enqueueing exports, so a user with a disconnected sync mount
had no way to *see* the gap before it silently grew. Pulling the
comparison out here lets GET /sessions/archive-status report the same
"P pending" number that POST /sessions/archive/sync would act on,
without duplicating (and risking drifting) the rule.

Everything here is pure filesystem inspection — no copying, no session
mutation — so it's cheap to call from a status endpoint on every poll.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def _scan_session_json(root: Path) -> List[Path]:
    try:
        return list(root.rglob("session_*.json"))
    except OSError:
        return []


def local_session_ids(src_dir) -> List[str]:
    """Every real session id found under the primary recordings dir.

    Recursive (matches SessionService.list_sessions()'s post-2026-08-07
    behavior). Sidecars — session_<id>.embeddings.npz / .embeddings.json
    don't match the glob, but session_<id>.commitments.json and
    session_<id>.item_status.json do; both have a "." in their stem
    (`session_<id>.commitments`) and are skipped, same rule
    list_sessions() and _reconcile_archive() have always used.
    """
    ids = []
    for p in _scan_session_json(Path(src_dir)):
        if "." in p.stem:
            continue
        ids.append(p.stem.replace("session_", "", 1))
    return ids


def archived_session_ids(dest_dir: Optional[str]) -> List[str]:
    """Session ids with a JSON file directly in the archive folder.

    Non-recursive on purpose: _archive_session() always writes flat,
    one level, never into subfolders — so a recursive scan here would
    only risk picking up something a user dropped in manually.
    """
    if not dest_dir:
        return []
    dest = Path(dest_dir).expanduser()
    if not dest.is_dir():
        return []
    ids = []
    try:
        candidates = list(dest.glob("session_*.json"))
    except OSError:
        candidates = []
    for p in candidates:
        if "." in p.stem:
            continue
        ids.append(p.stem.replace("session_", "", 1))
    return ids


def pending_session_ids(src_dir, dest_dir: Optional[str]) -> List[str]:
    """Local session ids not yet present (or stale) in the archive.

    THE SAME RULE _reconcile_archive() enqueues from — this function
    doesn't touch the export worker, it only decides what's owed, so
    the status endpoint and the sync endpoint can never disagree about
    what "pending" means.

    No dest_dir configured -> nothing to compare against -> []. A
    dest_dir that IS configured but not currently reachable (sync mount
    offline, path typo, external disk unplugged) must read as "every
    local session pending" — never "all present". That falls out for
    free here: `(dest / name).exists()` is False for every candidate
    when `dest` itself doesn't exist, so nothing is ever skipped as
    already-archived.
    """
    if not dest_dir:
        return []
    dest = Path(dest_dir).expanduser()
    pending: List[str] = []
    for src in _scan_session_json(Path(src_dir)):
        if "." in src.stem:
            continue
        d = dest / src.name
        try:
            if d.exists() and d.stat().st_mtime >= src.stat().st_mtime:
                continue
        except OSError:
            pass  # can't stat the destination copy — treat as pending
        pending.append(src.stem.replace("session_", "", 1))
    return pending
