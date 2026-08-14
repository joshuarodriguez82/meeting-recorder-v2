"""
Ships the Chrome extension inside the app so the user never has to find
the release page, download a zip, and locate their unpacked-extension
folder by hand for every release that touches it.

TWO HALVES
----------
1. LOCATE + READ the bundled copy of ``chrome-extension/`` that
   ``zip-bundle.py`` packs alongside ``server.py`` (see its
   ``INCLUDE_DIRS`` — the extension directory is written into the zip
   at the same level as ``server.py``, so it lands as a sibling of
   ``server.py`` inside the extracted runtime; see
   ``src-tauri/src/lib.rs``'s ``ensure_runtime_extracted``). A dev
   checkout run straight out of the repo (no zip-bundle build) instead
   finds ``chrome-extension/`` one level up, as a sibling of
   ``backend/`` — both are tried, in that order, and neither existing
   is a legitimate, non-crashing state (see ``find_bundled_extension_
   dir``).

2. EXPORT that copy into a STABLE, predictable folder under the user's
   app data dir — ``export_dir()`` — that never changes between
   releases. The user loads it unpacked in Chrome ONCE; every future
   "Install / Update extension files" click rewrites the same folder
   in place, so updating becomes "click Update, click Reload in
   Chrome" instead of another file hunt (see settings-view.tsx's
   Chrome Extension card).

VERSION MISMATCH
-----------------
``extension_version_status`` compares the bundled version (read from
the bundled ``manifest.json``) against the last-seen version the
extension itself reported on its most recent POST (recorded by
``ExtensionCalendarService.record_extension_version`` — see that
module). Four distinct states, deliberately not collapsed into a
binary "ok"/"stale":

  - "never_posted"    — the extension has never sent a single POST.
  - "unknown_version"  — it HAS posted, but never reported a version
                         (pre-1.2.0 — background.js only started
                         sending ``chrome.runtime.getManifest()
                         .version`` in 1.2.0). Must not be presented
                         as current just because we have no evidence
                         it's stale.
  - "update_available" — the last-seen version is older than bundled.
  - "up_to_date"        — the last-seen version matches (or,
                         degenerately, exceeds) bundled.
  - "unknown"           — the bundled version itself is unavailable
                         (dev checkout with no zip-bundle build) — we
                         can't judge either way.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

from config.settings import USER_DATA_DIR
from utils.logger import get_logger

logger = get_logger(__name__)

EXPORT_DIRNAME = "chrome-extension"


def _default_backend_dir() -> Path:
    """Where this module itself lives, one level up (``services/`` ->
    the backend dir — ``backend/`` in a dev checkout, or the extracted
    ``<data_root>/runtime/`` in a packaged build)."""
    return Path(__file__).resolve().parent.parent


def _candidate_bundle_dirs(backend_dir: Path) -> List[Path]:
    return [
        # Packaged runtime: zip-bundle.py writes chrome-extension/ into
        # the SAME zip as server.py, so it extracts as a sibling of it.
        backend_dir / "chrome-extension",
        # Dev checkout: chrome-extension/ lives at the repo root,
        # a sibling of backend/, not inside it.
        backend_dir.parent / "chrome-extension",
    ]


def find_bundled_extension_dir(backend_dir: Optional[Path] = None) -> Optional[Path]:
    """The bundled ``chrome-extension/`` directory, or None if this
    build doesn't carry one (a dev checkout that was never run through
    zip-bundle.py, or a corrupted/partial extraction). Never raises —
    every caller must degrade cleanly, not 500, when this returns
    None.

    ``backend_dir`` is overridable for tests; production callers
    should omit it.
    """
    base = Path(backend_dir) if backend_dir is not None else _default_backend_dir()
    for candidate in _candidate_bundle_dirs(base):
        if candidate.is_dir() and (candidate / "manifest.json").is_file():
            return candidate
    return None


def bundled_extension_version(backend_dir: Optional[Path] = None) -> Optional[str]:
    """The version string this app ships, read from the bundled
    ``manifest.json``. None if there's no bundled copy to read, or its
    manifest is unreadable/malformed — degrades clearly rather than
    raising, so a dev checkout without the bundle never 500s a
    Settings-page read."""
    ext_dir = find_bundled_extension_dir(backend_dir)
    if ext_dir is None:
        return None
    try:
        manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning(f"Bundled chrome-extension/manifest.json unreadable: {e}")
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    version = str(version).strip() if version else None
    return version or None


def export_dir() -> Path:
    """The STABLE, predictable folder the extension is exported to.
    Deliberately a fixed name under ``USER_DATA_DIR`` (the same
    ``%LOCALAPPDATA%\\MeetingRecorder`` / ``~/Library/Application
    Support/MeetingRecorder`` root every other per-user store lives
    under) — it must NEVER change between releases or installs, since
    that's the entire point: load it unpacked in Chrome once, and
    every subsequent "Install / Update" click rewrites this same path
    in place."""
    return USER_DATA_DIR / EXPORT_DIRNAME


def export_extension_files(dest: Optional[Path] = None,
                           backend_dir: Optional[Path] = None) -> List[str]:
    """Copy the bundled extension into ``dest`` (default:
    ``export_dir()``).

    Atomically-ish: every file is copied into a fresh temp directory
    NEXT TO the destination first; the old destination is only removed
    and replaced once every file has copied successfully. A failure
    partway through therefore leaves the temp directory as the only
    casualty (cleaned up in the ``except``) — the destination is either
    the complete previous install or the complete new one, never a
    partial mix presented as a success.

    Returns the relative paths written (posix-style, sorted), so the
    caller can report exactly what landed.

    Raises ``FileNotFoundError`` if no bundled extension is available
    (dev checkout without a zip-bundle build) — the caller turns that
    into a clear 404, not a 500.
    """
    src = find_bundled_extension_dir(backend_dir)
    if src is None:
        raise FileNotFoundError(
            "No bundled chrome-extension/ found next to the backend runtime. "
            "This is either a dev checkout that hasn't been packaged with "
            "zip-bundle.py, or a corrupted install."
        )

    target = Path(dest) if dest is not None else export_dir()
    target.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(
        prefix=".chrome-extension-export-", dir=target.parent))
    written: List[str] = []
    try:
        for root, _dirs, files in os.walk(src):
            rel_root = Path(root).relative_to(src)
            for fname in files:
                rel = rel_root / fname
                dst_file = tmp_dir / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(root) / fname, dst_file)
                written.append(rel.as_posix())

        # Swap: only touch the real destination once every file above
        # has copied without error.
        if target.exists():
            shutil.rmtree(target)
        os.replace(tmp_dir, target)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    written.sort()
    logger.info(f"Exported {len(written)} extension file(s) to {target}")
    return written


def _parse_semver(version: Optional[str]) -> Optional[tuple]:
    """"1.2.0" -> (1, 2, 0). None for anything that doesn't parse as
    dot-separated integers (never raises)."""
    if not version:
        return None
    parts = str(version).strip().split(".")
    if not parts:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def extension_version_status(bundled_version: Optional[str],
                             last_seen_version: Optional[str],
                             last_seen_at: Optional[str]) -> str:
    """Classify the relationship between what this app ships and what
    last posted. See the module docstring for the five states. Pure:
    no I/O, never raises.
    """
    if not bundled_version:
        return "unknown"
    if not last_seen_at:
        return "never_posted"
    if not last_seen_version:
        return "unknown_version"

    bundled_t = _parse_semver(bundled_version)
    seen_t = _parse_semver(last_seen_version)
    if bundled_t is None or seen_t is None:
        return "unknown_version"
    if seen_t < bundled_t:
        return "update_available"
    return "up_to_date"
