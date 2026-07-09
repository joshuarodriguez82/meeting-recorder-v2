"""Background export worker + network-folder resolution for sessions.

Why this exists (field repro 2026-07-09, session B677ED80): the
per-client Designated Folder export used to run SYNCHRONOUSLY on the
stop/process path. With the folder on a cloud-stream mount (Google
Drive `G:\\`), the WAV copy blocked on the sync filter driver, the
backend went unresponsive, the Tauri watchdog killed + respawned it,
and the UI showed "failed to fetch" storms — occasionally costing
recordings. The rule that fell out of that incident:

    The record → finalize → process path NEVER touches a network
    folder. Network copies happen here, in the background, with
    retries, best-effort.

Folder resolution ("by the correct client"):

    1. The client's explicit Designated Folder, when configured.
    2. `<cloud_mirror_dir>/<client name>/` when the global mirror root
       is set and the session has a client.
    3. `<cloud_mirror_dir>/Unfiled/` when the mirror root is set but
       the session has no client tag.
    4. None → no export (no mirror root, no designated folder).

The worker is a single daemon thread draining a queue, so exports are
serialized (no two jobs mutate the same session JSON concurrently) and
a stalled network write delays only other exports — never the app.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# Retry schedule for a failed export job. Cloud-stream mounts fail
# transiently (sync client busy, token refresh, brief offline); a few
# spaced retries recover most of those. After the last attempt the job
# is dropped with a loud log — the next artifact-producing step (or a
# manual re-export) will enqueue the session again.
_RETRY_DELAYS_S = (5.0, 30.0, 120.0)

_INVALID_FOLDER_CHARS = '<>:"/\\|?*'


def sanitize_folder_name(name: str) -> str:
    """Make a client name safe to use as a single folder component.
    Windows-reserved characters are replaced, edges trimmed of dots and
    spaces (Explorer refuses trailing dots). Falls back to 'Unfiled'
    when nothing survivable remains."""
    cleaned = "".join(
        ("-" if c in _INVALID_FOLDER_CHARS or ord(c) < 32 else c)
        for c in (name or "")
    ).strip(" .")
    return cleaned or "Unfiled"


def resolve_export_folder(
    explicit_folder: str,
    client: str,
    mirror_root: str,
) -> Optional[str]:
    """Apply the resolution order documented in the module docstring.
    Returns the target directory as a string, or None for "don't
    export". Pure — no filesystem access — so it's trivially testable
    and never blocks."""
    if explicit_folder and explicit_folder.strip():
        return explicit_folder.strip()
    root = (mirror_root or "").strip()
    if not root:
        return None
    from pathlib import Path
    return str(Path(root) / sanitize_folder_name(client))


class ExportWorker:
    """Single background thread that runs export jobs off the hot path.

    ``do_export`` is injected: ``(session_id: str, copy_audio: bool) ->
    None``. It's expected to re-load the session fresh from disk (the
    enqueue-er's in-memory object may be stale by the time the job
    runs) and to raise on failure so the retry schedule applies.
    """

    def __init__(self, do_export: Callable[[str, bool], None]):
        self._do_export = do_export
        self._q: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        self._pending: set[tuple[str, bool]] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="export-worker", daemon=True)
        self._thread.start()

    def enqueue(self, session_id: str, copy_audio: bool = False) -> None:
        """Queue a session for export. Never blocks, never raises.
        Coalesces an identical job already waiting in the queue."""
        job = (session_id, copy_audio)
        with self._lock:
            if job in self._pending:
                return
            self._pending.add(job)
        self._q.put(job)

    def _run(self) -> None:
        while True:
            job = self._q.get()
            session_id, copy_audio = job
            try:
                self._run_with_retries(session_id, copy_audio)
            finally:
                with self._lock:
                    self._pending.discard(job)
                self._q.task_done()

    def _run_with_retries(self, session_id: str, copy_audio: bool) -> None:
        attempts = 1 + len(_RETRY_DELAYS_S)
        for i in range(attempts):
            try:
                self._do_export(session_id, copy_audio)
                return
            except Exception as e:
                if i < len(_RETRY_DELAYS_S):
                    delay = _RETRY_DELAYS_S[i]
                    logger.warning(
                        f"Export of session {session_id} failed "
                        f"(attempt {i + 1}/{attempts}): {e} — retrying "
                        f"in {delay:.0f}s")
                    time.sleep(delay)
                else:
                    logger.error(
                        f"Export of session {session_id} failed after "
                        f"{attempts} attempts: {e} — giving up (the next "
                        f"processing step will re-enqueue it)")
