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

FIELD REPORT 2026-08-07 (second incident, same day): the whole-file
last-writer-wins copy above is flat-out wrong for client_configs.json,
because ClientConfig carries per-machine, absolute filesystem paths —
`export_folder` (per-client Designated Folder) and `knowledge_folder`
(per-client document folder) — alongside the roam-worthy identity
fields (the client key + `display_name`). The user runs Windows and a
Mac sharing one archive folder; the Windows config's paths
(`G:\\My Drive\\Aon`) roamed onto the Mac by whole-file copy, and the
Mac started repeatedly queuing exports against a drive letter that can
never exist there:

    Reconcile 'Aon': queued 1 session(s) for export to G:\\My Drive\\Aon

So client_configs.json now gets a FIELD-AWARE MERGE instead of a raw
copy, in both directions:

  - push(): the archive copy is written with export_folder and
    knowledge_folder blanked on every client, keeping the key set and
    display_name intact. This machine's local paths are never published
    into a shared folder another machine could read them back from.
  - pull(): client KEYS union (archive-only clients are added locally
    with empty folder fields), but a key that already exists locally
    keeps its LOCAL export_folder/knowledge_folder verbatim no matter
    what the archive copy says — only display_name may be refreshed
    from a newer archive. A local client already present is never
    deleted just because the archive doesn't have it yet (a machine
    that hasn't synced yet must not wipe the other's list).

summary_templates.json has no filesystem paths (verified against
services/template_service.py: a Template is name/prompt/is_default/
default_prompt/hidden — nothing machine-specific) so it keeps the
original whole-file, newest-wins copy semantics.

See also sanitize_local_paths() below, which heals a client_configs.json
that already has a foreign-platform path sitting in it (from before this
fix, or from any other route) instead of only preventing new damage.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from services._cloud_sync import CloudFileNotReadyError, read_text_hydrated
from utils.logger import get_logger

logger = get_logger(__name__)

# The file that carries per-machine, absolute filesystem paths and so
# needs the field-aware merge instead of a whole-file copy. Kept as a
# named constant (rather than a magic string sprinkled through the
# dispatch below) since it's checked in several places.
CLIENT_CONFIGS_FILE = "client_configs.json"

# The two JSON stores that live alongside the recordings dir but are not
# part of the session-JSON archive copy. Order doesn't matter — both are
# handled independently — but keeping it as a tuple (not a set) gives
# status()/push()/pull() a stable, deterministic report order.
SHARED_FILES = (CLIENT_CONFIGS_FILE, "summary_templates.json")

# ClientConfig fields that are absolute, machine-specific filesystem
# paths (services/client_config_service.py's ClientConfig dataclass).
# These must never be taken from the archive, in either direction.
PER_MACHINE_FIELDS = ("export_folder", "knowledge_folder")


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


def _atomic_write_json(path: Path, data: dict) -> None:
    """Same atomic-write pattern as ClientConfigService._write_all:
    temp file in the same directory, fsync, os.replace. A config file
    this small is exactly the kind of thing a crash-mid-write can
    otherwise truncate to zero bytes and lose every client's settings,
    so every writer in this module goes through here rather than a bare
    Path.write_text()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _blank_per_machine_fields(entry: dict) -> dict:
    """Copy of `entry` with export_folder/knowledge_folder forced to "".
    Keys + display_name (and anything else the caller has already put on
    the entry) pass through untouched — only the two absolute-path
    fields are ever cleared, since those are the only ones that can
    point somewhere that doesn't exist on the machine reading them."""
    blanked = dict(entry) if isinstance(entry, dict) else {}
    for field in PER_MACHINE_FIELDS:
        blanked[field] = ""
    return blanked


def _read_local_dict(path: Path) -> Dict[str, dict]:
    """Best-effort read of a local shared-state JSON file for merge
    purposes. Never raises — an absent, un-hydrated, or malformed local
    file degrades to "no local clients yet" rather than blocking the
    pull; the archive side already has its own safety gate, and a local
    client_configs.json this broken has bigger problems than a sync
    skip."""
    if not path.is_file():
        return {}
    try:
        text = read_text_hydrated(path)
    except (CloudFileNotReadyError, OSError):
        return {}
    if not _is_valid_shared_json(text):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_client_configs(
    local_raw: Dict[str, dict], archive_raw: Dict[str, dict]
) -> Dict[str, dict]:
    """The field-aware merge at the heart of the 2026-08-07 fix.

    - Every LOCAL key survives with its export_folder/knowledge_folder
      untouched, no matter what the archive says — that's the whole
      point, a Windows path must never land on a Mac (or vice versa).
      display_name is refreshed from the archive when the archive has a
      non-empty one for that key (the caller only invokes this when the
      archive copy is the newer file, so "refresh display_name from a
      newer archive" falls out of that mtime gate for free).
    - A key present ONLY in the archive is a client that doesn't exist
      on this machine yet: it's added with BLANKED folder fields (the
      user sets them locally) — the archive's own copy of those fields
      is already blanked by push(), but we blank again here too in case
      the archive file predates this fix or was written by a stray
      whole-file copy.
    - Nothing is ever deleted: a local key absent from the archive is
      simply a client the other machine hasn't synced yet, not a client
      to remove.
    """
    merged: Dict[str, dict] = {}
    for key, local_entry in local_raw.items():
        local_entry = local_entry if isinstance(local_entry, dict) else {}
        merged_entry = dict(local_entry)
        archive_entry = archive_raw.get(key)
        if isinstance(archive_entry, dict):
            archive_display = archive_entry.get("display_name")
            if archive_display:
                merged_entry["display_name"] = archive_display
        merged[key] = merged_entry

    for key, archive_entry in archive_raw.items():
        if key in merged:
            continue
        if not isinstance(archive_entry, dict):
            merged[key] = archive_entry
            continue
        merged[key] = _blank_per_machine_fields(archive_entry)

    return merged


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_foreign_local_path(value: str) -> bool:
    """True only for a path that is STRUCTURALLY impossible on the
    platform we're running on right now — never for a path that merely
    doesn't currently resolve (an unplugged external drive, a not-yet-
    mounted network share) since those are still perfectly plausible
    local paths that must survive a sanitize pass.

    - POSIX (macOS/Linux): a Windows drive-letter root (`G:\\...` or
      `G:/...`) or a UNC path (`\\\\server\\share`) can never exist here.
    - Windows: a POSIX-style absolute path (`/Users/...`) that doesn't
      resolve to anything on this machine came from macOS/Linux, not
      from a locally-unplugged drive (a real Windows-local plausible
      path never starts with a bare `/`).
    """
    if not value:
        return False
    if os.name == "nt":
        if value.startswith("/"):
            # os.path.exists (not Path(value).exists()) deliberately —
            # pathlib's Path() dispatches to WindowsPath/PosixPath based
            # on os.name at *every* construction (including internal
            # ones like joinpath), so a test harness that monkeypatches
            # os.name to exercise this branch on a POSIX CI host would
            # otherwise blow up with "cannot instantiate 'WindowsPath'
            # on your system" the moment any Path() gets constructed
            # downstream. os.path.exists() just calls stat() on the raw
            # string and carries no such dependency.
            try:
                return not os.path.exists(value)
            except OSError:
                return False
        return False
    return bool(_WINDOWS_DRIVE_RE.match(value)) or value.startswith("\\\\")


def sanitize_local_paths(recordings_dir: str) -> List[dict]:
    """Heal an already-damaged LOCAL client_configs.json: clear any
    export_folder/knowledge_folder that is definitively foreign to the
    CURRENT platform (see _is_foreign_local_path).

    Field report 2026-08-07: this is the after-the-fact half of the fix
    — push()/pull() above stop NEW foreign paths from landing, but the
    user's Mac already had `G:\\My Drive\\Aon` / `G:\\My Drive\\Ricoh`
    written into its local client_configs.json (roamed there by the
    pre-fix whole-file copy) and the app was actively re-queuing exports
    against them every reconcile sweep:

        Reconcile 'Aon': queued 1 session(s) for export to G:\\My Drive\\Aon

    Detection is deliberately conservative — see _is_foreign_local_path
    — so a merely-missing-but-plausible local path is never touched.
    Never raises; an unreadable/un-hydrated/malformed local file is
    treated as "nothing to sanitize this pass" the same way push()/
    pull() treat those conditions elsewhere in this module.

    Returns what was cleared: one dict per cleared field with the
    client's display name (or key), which field, and the value that was
    cleared, so the caller can log and surface it to the user rather
    than silently rewriting their config.
    """
    if not recordings_dir:
        return []
    path = Path(recordings_dir).expanduser() / CLIENT_CONFIGS_FILE
    if not path.is_file():
        return []
    try:
        text = read_text_hydrated(path)
    except CloudFileNotReadyError as e:
        logger.warning(
            f"shared_state_sync.sanitize_local_paths: {path.name} not "
            f"hydrated from cloud sync yet, skipping this pass: {e}")
        return []
    except OSError as e:
        logger.warning(
            f"shared_state_sync.sanitize_local_paths: could not read "
            f"{path.name}: {e}")
        return []
    if not _is_valid_shared_json(text):
        logger.warning(
            "shared_state_sync.sanitize_local_paths: local "
            "client_configs.json is not valid JSON (or not a dict) — "
            "skipping")
        return []
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []

    cleared: List[dict] = []
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        for field in PER_MACHINE_FIELDS:
            value = entry.get(field)
            if not value or not isinstance(value, str):
                continue
            if _is_foreign_local_path(value):
                cleared.append({
                    "client": entry.get("display_name") or key,
                    "field": field,
                    "old_value": value,
                })
                entry[field] = ""

    if cleared:
        try:
            _atomic_write_json(path, raw)
        except OSError as e:
            logger.warning(
                f"shared_state_sync.sanitize_local_paths: could not "
                f"write {path.name}: {e}")
            return []
        logger.info(
            f"shared_state_sync: sanitized {len(cleared)} foreign "
            f"folder path(s) in {path}")
    return cleared


def push(recordings_dir: str, archive_dir: str) -> List[str]:
    """Copy shared files FROM recordings_dir INTO archive_dir wherever
    the local copy is strictly newer (or the archive has none yet).

    client_configs.json gets the field-aware treatment: the archive
    copy is written with every client's export_folder/knowledge_folder
    blanked (see _blank_per_machine_fields) — this machine's local paths
    are never published into a shared folder another machine could read
    them back from. summary_templates.json (no filesystem paths) keeps
    a plain byte-for-byte copy.

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

        if name == CLIENT_CONFIGS_FILE:
            ok = _push_client_configs(src, dst, src_mtime)
        else:
            try:
                shutil.copy2(src, dst)  # copy2 preserves mtime; the whole
                # comparison above (and every future comparison against
                # this copy) depends on that.
                ok = True
            except OSError as e:
                logger.warning(
                    f"shared_state_sync.push: could not copy {name}: {e}")
                ok = False
        if ok:
            copied.append(name)
    if copied:
        logger.info(f"shared_state_sync: pushed {copied} to {dest_dir}")
    return copied


def _push_client_configs(src: Path, dst: Path, src_mtime: float) -> bool:
    try:
        text = read_text_hydrated(src)
    except CloudFileNotReadyError as e:
        logger.warning(
            f"shared_state_sync.push: {src.name} not hydrated from cloud "
            f"sync yet, skipping this sweep: {e}")
        return False
    except OSError as e:
        logger.warning(f"shared_state_sync.push: could not read {src.name}: {e}")
        return False
    if not _is_valid_shared_json(text):
        logger.warning(
            f"shared_state_sync.push: local {src.name} is not valid JSON "
            f"(or not a dict) — refusing to publish it to the archive")
        return False
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return False

    blanked = {
        key: (_blank_per_machine_fields(entry) if isinstance(entry, dict) else entry)
        for key, entry in raw.items()
    }
    try:
        _atomic_write_json(dst, blanked)
        # Preserve the source mtime the same way shutil.copy2 would, so
        # every future push()/pull()/status() mtime comparison against
        # this archive copy behaves exactly as if it had been a plain
        # copy — an idempotent push (nothing changed locally) must not
        # look like "the archive just got newer" on the next sweep.
        os.utime(dst, (src_mtime, src_mtime))
    except OSError as e:
        logger.warning(f"shared_state_sync.push: could not write {dst.name}: {e}")
        return False
    return True


def pull(recordings_dir: str, archive_dir: str) -> List[str]:
    """Copy shared files FROM archive_dir INTO recordings_dir wherever
    the archive copy is strictly newer AND passes the JSON-dict safety
    check.

    client_configs.json gets the field-aware merge (see
    _merge_client_configs): local export_folder/knowledge_folder always
    win, archive-only clients are added with blanked folder fields, and
    a local client is never deleted just because the archive doesn't
    have it yet. summary_templates.json (no filesystem paths) keeps a
    plain byte-for-byte copy.

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
        except OSError as e:
            logger.warning(f"shared_state_sync.pull: could not write {name}: {e}")
            continue

        if name == CLIENT_CONFIGS_FILE:
            ok = _pull_client_configs(text, dst, src_mtime)
        else:
            try:
                shutil.copy2(src, dst)
                ok = True
            except OSError as e:
                logger.warning(f"shared_state_sync.pull: could not write {name}: {e}")
                ok = False
        if ok:
            copied.append(name)
    if copied:
        logger.info(f"shared_state_sync: pulled {copied} from {src_dir}")
    return copied


def _pull_client_configs(archive_text: str, dst: Path, archive_mtime: float) -> bool:
    try:
        archive_raw = json.loads(archive_text)  # already validated as a dict by the caller
    except json.JSONDecodeError:
        return False
    local_raw = _read_local_dict(dst)
    merged = _merge_client_configs(local_raw, archive_raw)
    try:
        _atomic_write_json(dst, merged)
        # Match the archive's mtime (same reasoning as _push_client_configs)
        # so an unchanged archive doesn't look "newer" again next sweep.
        os.utime(dst, (archive_mtime, archive_mtime))
    except OSError as e:
        logger.warning(f"shared_state_sync.pull: could not write {dst.name}: {e}")
        return False
    return True


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
