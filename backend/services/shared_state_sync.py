"""Roams client_configs.json and summary_templates.json through the
Session Archive folder (SESSION_ARCHIVE_DIR).

Field report 2026-08-07: v2.20's roaming Session Archive copies
session_<id>.json (+ sidecars) between machines via archive_reconcile.py
/ _archive_session(), and SessionService reads the archive back as an
extra root — so MEETINGS roam. But client_configs.json and
summary_templates.json (ClientConfigService / TemplateService, both
constructed on `<recordings_dir>/<file>` in ServiceContainer.load_settings)
were never part of that copy. A user with a Windows PC and a Mac saw
sessions show up on both, but a client that exists ONLY in
client_configs.json (no tagged meeting yet) — plus that client's
Designated Folder / Knowledge Folder settings, plus the user's edited
summary templates — stayed stuck on whichever machine last wrote them.

This module is the client_configs.json / summary_templates.json
equivalent of archive_reconcile.py: pure filesystem inspection (status)
plus the actual copy (push/pull), no app singletons, so it's testable
without spinning up ServiceContainer and safe to call from a background
sweep. Same last-writer-wins-by-mtime rule as _archive_session() uses
for session JSONs, extended with a parse-and-shape check before any
pull can overwrite a local file — a config file is small enough that a
bad copy doesn't just lose one meeting, it can silently blank out every
client's export folder, so the bar for "safe to overwrite" is higher
here than for a session JSON.

CRITICAL SAFETY (same class of bug as three other field reports this
week): a cloud-synced file that hasn't finished downloading to this
device is NOT an empty or missing file — it's a placeholder whose bytes
aren't here yet. read_text_hydrated() (services/_cloud_sync.py) knows
how to wait it out and raises CloudFileNotReadyError instead of
returning "" when it gives up, so a half-synced archive copy of
client_configs.json can never look like "archive says you have zero
clients" and clobber a good local file. On top of that, before any pull
overwrites the local copy we also require the archive bytes to parse as
JSON *and* have a dict root — a truncated write mid-sync (or a stray
non-JSON file someone dropped in the archive folder) is skipped with a
recorded reason rather than applied.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from services._cloud_sync import CloudFileNotReadyError, read_text_hydrated
from utils.logger import get_logger

logger = get_logger(__name__)

# The two JSON stores that live alongside the recordings dir but are not
# part of the session-JSON archive copy. Order doesn't matter — both are
# handled independently — but keeping it as a tuple (not a set) gives
# status()/push()/pull() a stable, deterministic report order.
SHARED_FILES = ("client_configs.json", "summary_templates.json")


def _valid_dir(path: str) -> Optional[Path]:
    """Resolve `path` to a Path only if it's a non-empty, existing
    directory. Both push() and pull() are no-ops otherwise — an unset or
    unreachable (unplugged drive, typo'd path, not-yet-mounted cloud
    folder) archive dir must never be treated as "nothing to roam", but
    it also must never be silently mkdir'd into existence here; that
    decision belongs to the Settings save path (see server.py's
    save_settings), not to a sync helper that runs on every sweep."""
    p = (path or "").strip()
    if not p:
        return None
    resolved = Path(p).expanduser()
    if not resolved.is_dir():
        return None
    return resolved


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _is_valid_shared_json(text: str) -> bool:
    """A shared-state file is valid only if it parses as JSON with a
    dict root. Both client_configs.json and summary_templates.json are
    keyed dicts (client name -> config, template name -> {name, prompt})
    — a list, a bare string, or invalid JSON is never a legitimate
    contents for either file, so treating it as "unreadable, skip" is
    always correct rather than a guess."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict)


def push(recordings_dir: str, archive_dir: str) -> List[str]:
    """Copy shared files FROM recordings_dir INTO archive_dir wherever
    the local copy is strictly newer (or the archive has none yet).

    Returns the filenames actually copied. No-op (returns []) when
    archive_dir is unset/unreachable — same contract as
    archive_reconcile.pending_session_ids for a disconnected archive.
    """
    dest_dir = _valid_dir(archive_dir)
    if dest_dir is None:
        return []
    src_dir = Path(recordings_dir).expanduser()
    copied: List[str] = []
    for name in SHARED_FILES:
        src = src_dir / name
        if not src.is_file():
            continue
        dst = dest_dir / name
        src_mtime = _mtime(src)
        dst_mtime = _mtime(dst)
        if src_mtime is None:
            continue
        if dst_mtime is not None and dst_mtime >= src_mtime:
            continue  # archive copy is same age or newer — nothing to do
        try:
            shutil.copy2(src, dst)  # copy2 preserves mtime; the whole
            # comparison above (and every future comparison against this
            # copy) depends on that.
        except OSError as e:
            logger.warning(f"shared_state_sync.push: could not copy {name}: {e}")
            continue
        copied.append(name)
    if copied:
        logger.info(f"shared_state_sync: pushed {copied} to {dest_dir}")
    return copied


def pull(recordings_dir: str, archive_dir: str) -> List[str]:
    """Copy shared files FROM archive_dir INTO recordings_dir wherever
    the archive copy is strictly newer AND passes the JSON-dict safety
    check.

    Returns the filenames actually copied. No-op (returns []) when
    archive_dir is unset/unreachable.
    """
    src_dir = _valid_dir(archive_dir)
    if src_dir is None:
        return []
    dest_dir = Path(recordings_dir).expanduser()
    copied: List[str] = []
    for name in SHARED_FILES:
        src = src_dir / name
        if not src.is_file():
            continue
        dst = dest_dir / name
        src_mtime = _mtime(src)
        dst_mtime = _mtime(dst)
        if src_mtime is None:
            continue
        if dst_mtime is not None and dst_mtime >= src_mtime:
            continue  # local copy is same age or newer — nothing to do

        # Safety gate: never let an unreadable/un-hydrated/malformed
        # archive copy overwrite a good local file. read_text_hydrated
        # raises CloudFileNotReadyError (a subclass of OSError) rather
        # than returning "" for a placeholder that hasn't downloaded
        # yet — that exception is exactly the signal we want to treat
        # as "skip, try again next sweep", not "archive file is empty".
        try:
            text = read_text_hydrated(src)
        except CloudFileNotReadyError as e:
            logger.warning(
                f"shared_state_sync.pull: {name} not hydrated from cloud "
                f"sync yet, skipping this sweep: {e}")
            continue
        except OSError as e:
            logger.warning(f"shared_state_sync.pull: could not read {name}: {e}")
            continue

        if not _is_valid_shared_json(text):
            logger.warning(
                f"shared_state_sync.pull: archive copy of {name} is not "
                f"valid JSON (or not a dict) — refusing to overwrite the "
                f"local copy with it")
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as e:
            logger.warning(f"shared_state_sync.pull: could not write {name}: {e}")
            continue
        copied.append(name)
    if copied:
        logger.info(f"shared_state_sync: pulled {copied} from {src_dir}")
    return copied


def status(recordings_dir: str, archive_dir: str) -> Dict[str, dict]:
    """Per-file roaming status: presence + mtimes on each side, which
    direction a sync would move data, and any reason a pull would be
    skipped. Pure inspection (no copying) — cheap enough to call on
    every Settings poll, same contract as _archive_status_report().
    """
    local_root = Path(recordings_dir).expanduser() if recordings_dir else None
    archive_root = _valid_dir(archive_dir)

    report: Dict[str, dict] = {}
    for name in SHARED_FILES:
        local_path = local_root / name if local_root else None
        archive_path = archive_root / name if archive_root else None

        local_present = bool(local_path and local_path.is_file())
        archive_present = bool(archive_path and archive_path.is_file())
        local_mtime = _mtime(local_path) if local_present else None
        archive_mtime = _mtime(archive_path) if archive_present else None

        reason: Optional[str] = None
        if not archive_dir or not (archive_dir or "").strip():
            direction = "absent" if not local_present else "in-sync"
        elif archive_root is None:
            # Configured but unreachable — same "everything pending"
            # posture archive_reconcile.py takes for session JSONs: an
            # offline sync mount must never read as "all present".
            direction = "push" if local_present else "absent"
            reason = "archive folder not reachable"
        elif local_present and archive_present:
            if local_mtime is not None and archive_mtime is not None:
                if local_mtime > archive_mtime:
                    direction = "push"
                elif archive_mtime > local_mtime:
                    direction = "pull"
                else:
                    direction = "in-sync"
            else:
                direction = "in-sync"
            # Only worth reporting a parse problem when a pull would
            # otherwise be attempted — a stale-but-valid archive copy
            # doesn't need a warning.
            if direction == "pull":
                try:
                    text = read_text_hydrated(archive_path)
                except CloudFileNotReadyError:
                    reason = "archive copy not yet downloaded from cloud sync"
                except OSError as e:
                    reason = f"archive copy unreadable: {e}"
                else:
                    if not _is_valid_shared_json(text):
                        reason = "archive copy is malformed — will not be pulled"
        elif local_present and not archive_present:
            direction = "push"
        elif archive_present and not local_present:
            direction = "pull"
        else:
            direction = "absent"

        report[name] = {
            "local_present": local_present,
            "archive_present": archive_present,
            "local_mtime": local_mtime,
            "archive_mtime": archive_mtime,
            "direction": direction,
            "reason": reason,
        }
    return report
