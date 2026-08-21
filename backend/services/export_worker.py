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

# Windows refuses to create a directory named after a reserved DOS
# device, with or without an extension (CON, NUL, COM1…). A client
# acronym that happens to match would make every mkdir/copy fail and
# the session silently never mirror; suffix such names to dodge it.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_folder_name(name: str) -> str:
    """Make a client name safe to use as a single folder component.
    Windows-reserved characters are replaced, edges trimmed of dots and
    spaces (Explorer refuses trailing dots), and reserved DOS device
    names are suffixed. Falls back to 'Unfiled' when nothing survivable
    remains."""
    cleaned = "".join(
        ("-" if c in _INVALID_FOLDER_CHARS or ord(c) < 32 else c)
        for c in (name or "")
    ).strip(" .")
    if not cleaned:
        return "Unfiled"
    # A reserved device name is matched case-insensitively and ignoring
    # any extension (CON, con, CON.txt all collide).
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"{cleaned}_"
    return cleaned


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
        # Queue items are (session_id, copy_audio, attempt). Retries are
        # re-queued (not re-slept-on the worker) so one failing job can't
        # head-of-line-block every other session's export.
        self._q: "queue.Queue[tuple[str, bool, int]]" = queue.Queue()
        # Coalesce NEW enqueues on session_id → OR'd copy_audio. Cleared
        # at DEQUEUE, so a re-enqueue arriving while a job runs schedules
        # a fresh export of the newer artifacts instead of being dropped.
        self._pending: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="export-worker", daemon=True)
        self._thread.start()

    def enqueue(self, session_id: str, copy_audio: bool = False) -> None:
        """Queue a session for export. Never blocks, never raises. A new
        enqueue for a session already pending ORs its copy_audio flag in
        (copy_audio=True is a superset export) rather than adding a
        redundant second job."""
        with self._lock:
            if session_id in self._pending:
                self._pending[session_id] = (
                    self._pending[session_id] or copy_audio)
                return
            self._pending[session_id] = copy_audio
        self._q.put((session_id, copy_audio, 0))

    def _run(self) -> None:
        while True:
            session_id, copy_audio, attempt = self._q.get()
            try:
                if attempt == 0:
                    # Clear the coalescing slot at dequeue and pick up any
                    # copy_audio flag OR'd in since it was queued.
                    with self._lock:
                        copy_audio = self._pending.pop(session_id, copy_audio)
                self._attempt_once(session_id, copy_audio, attempt)
            finally:
                self._q.task_done()

    def _attempt_once(self, session_id: str, copy_audio: bool,
                      attempt: int) -> None:
        try:
            self._do_export(session_id, copy_audio)
        except Exception as e:
            if attempt < len(_RETRY_DELAYS_S):
                delay = _RETRY_DELAYS_S[attempt]
                logger.warning(
                    f"Export of session {session_id} failed "
                    f"(attempt {attempt + 1}): {e} — retrying in "
                    f"{delay:.0f}s")
                # Re-queue after the delay WITHOUT blocking the worker, so
                # a dead mount can't stall every other session's export.
                t = threading.Timer(
                    delay, self._q.put,
                    args=((session_id, copy_audio, attempt + 1),))
                t.daemon = True
                t.start()
            else:
                logger.error(
                    f"Export of session {session_id} failed after "
                    f"{attempt + 1} attempts: {e} — giving up")

class PortalPushWorker:
    """Single daemon thread pushing engagement registers to the SA
    Tools Portal, off the hot path — the same architecture, retry
    schedule and reasoning as ExportWorker above (see the 2026-07-09
    incident in its docstring: flaky I/O must never share a thread with
    record → finalize → process; a flaky HTTPS endpoint belongs on
    exactly this kind of thread).

    ``do_push(client_key, project_key)`` is injected
    (PortalPushService.push). Retry contract, matching the portal's
    documented failure semantics:

      * PortalTransient  → re-queued on the (5s, 30s, 120s) schedule.
        503 is a partial write and is the one status that MEANS retry.
      * PortalBindingBroken → dropped. The service has already marked
        the binding broken, which also stops FUTURE enqueues; retrying
        a bad token forever is silent failure wearing a schedule.
      * PortalPermanent → dropped and logged. Identical bytes cannot
        produce a different answer from a 400/422.

    Enqueues coalesce per scope: a register regenerated three times
    while the portal is down pushes once on recovery, with the newest
    file — push() reads the register from disk at send time, so the
    queue never holds stale content.
    """

    def __init__(self, do_push: Callable[[str, str], object]):
        self._do_push = do_push
        self._q: "queue.Queue[tuple[str, str, int]]" = queue.Queue()
        self._pending: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="portal-push-worker", daemon=True)
        self._thread.start()

    def enqueue(self, client_key: str, project_key: str) -> None:
        """Never blocks, never raises — this is called from the
        register-write path, and acceptance criterion 7 is that the
        portal being unreachable (or this worker being broken) cannot
        affect recording, finalizing or processing."""
        try:
            key = (client_key, project_key)
            with self._lock:
                if key in self._pending:
                    return
                self._pending.add(key)
            self._q.put((client_key, project_key, 0))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"portal push enqueue failed: {e}")

    def _run(self) -> None:
        # Imported here, not at module top: this module predates the
        # portal service and is imported by the processing path's
        # neighbours; a cycle would be easy to create and hard to see.
        from services.portal_push_service import (
            PortalBindingBroken, PortalPermanent, PortalTransient)
        while True:
            client_key, project_key, attempt = self._q.get()
            try:
                if attempt == 0:
                    with self._lock:
                        self._pending.discard((client_key, project_key))
                try:
                    self._do_push(client_key, project_key)
                except PortalTransient as e:
                    if attempt < len(_RETRY_DELAYS_S):
                        delay = _RETRY_DELAYS_S[attempt]
                        logger.warning(
                            f"portal push {client_key}/{project_key} "
                            f"transient failure (attempt {attempt + 1}): "
                            f"{e} — retrying in {delay:.0f}s")
                        t = threading.Timer(
                            delay, self._q.put,
                            args=((client_key, project_key, attempt + 1),))
                        t.daemon = True
                        t.start()
                    else:
                        logger.error(
                            f"portal push {client_key}/{project_key} "
                            f"failed after {attempt + 1} attempts: {e} — "
                            f"giving up until the next register write")
                except PortalBindingBroken as e:
                    logger.error(
                        f"portal push {client_key}/{project_key}: binding "
                        f"broken ({e}) — not retrying; re-bind in Settings")
                except PortalPermanent as e:
                    logger.error(
                        f"portal push {client_key}/{project_key}: "
                        f"rejected ({e}) — not retrying")
                except Exception as e:  # noqa: BLE001
                    logger.error(
                        f"portal push {client_key}/{project_key}: "
                        f"unexpected error: {e} — not retrying")
            finally:
                self._q.task_done()
