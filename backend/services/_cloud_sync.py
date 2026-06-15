"""
Cloud-synced-folder aware file reads.

When RECORDINGS_DIR points at a OneDrive / iCloud folder (the supported
"roam clients + sessions across devices" setup), files that arrive from
another device land as *online-only placeholders*: the name is in the
directory listing and `stat()` reports a size, but the bytes aren't on
local disk yet. Finder/Explorer transparently fault them in the instant
you click; a background process that just does `read_text()` can instead
get an empty read or an OSError ("No data available") and — in the old
code — silently treat the file as absent. That's why a freshly-synced
client_configs.json / session_*.json was "there but not showing".

Reading the file is itself what triggers the OS to download it, so a
short bounded retry usually self-heals (we just have to give the sync
client a beat to materialize it). If it still won't come down, we raise
a typed error with an actionable message instead of pretending the data
doesn't exist.
"""

from __future__ import annotations

import errno
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# errno values a not-yet-materialized cloud placeholder read can surface.
# ENODATA: Linux/OneDrive "No data available". EAGAIN: hydration in
# flight. EIO: provider hiccup mid-fault-in. Any of these is retryable.
_HYDRATING_ERRNOS = {errno.ENODATA, errno.EAGAIN, errno.EIO}


class CloudFileNotReadyError(OSError):
    """A file exists in a cloud-synced folder but its contents haven't
    downloaded to this device yet (online-only placeholder)."""


def _looks_dataless(path: Path) -> bool:
    """Heuristic: a file with a non-zero logical size but zero allocated
    blocks is an un-hydrated placeholder (macOS File Provider / OneDrive
    Files On-Demand). st_blocks is best-effort and absent on some FSes,
    so this is only ever used as a *hint*, never a hard gate."""
    try:
        st = path.stat()
        blocks = getattr(st, "st_blocks", None)
        return st.st_size > 0 and blocks == 0
    except OSError:
        return False


def read_text_hydrated(
    path: Path,
    *,
    retries: int = 4,
    delay: float = 0.5,
    encoding: str = "utf-8-sig",
) -> str:
    """Read a text file, tolerating cloud-placeholder fault-in latency.

    Returns the decoded contents. Raises CloudFileNotReadyError if the
    file exists but its bytes never materialize within the retry budget,
    so callers can surface an actionable message instead of silently
    losing the user's synced data. A genuinely-absent file still raises
    FileNotFoundError as usual.
    """
    last_exc: OSError | None = None
    for attempt in range(retries):
        try:
            raw = path.read_bytes()
            # An existing-but-empty read of a file the OS still reports
            # as non-empty is the classic "placeholder not faulted in
            # yet" signature — keep nudging it.
            if not raw and _looks_dataless(path):
                logger.info(
                    "%s looks like an un-downloaded cloud file "
                    "(attempt %d/%d) — waiting for it to materialize",
                    path.name, attempt + 1, retries)
                time.sleep(delay * (attempt + 1))
                continue
            return raw.decode(encoding)
        except FileNotFoundError:
            raise
        except OSError as e:
            if e.errno in _HYDRATING_ERRNOS and attempt < retries - 1:
                logger.info(
                    "%s not yet hydrated from cloud sync (errno=%s, "
                    "attempt %d/%d) — retrying",
                    path.name, e.errno, attempt + 1, retries)
                last_exc = e
                time.sleep(delay * (attempt + 1))
                continue
            last_exc = e
            break

    raise CloudFileNotReadyError(
        f"{path.name} exists in the synced folder but its contents "
        f"haven't downloaded to this device yet. In OneDrive (or "
        f"iCloud), right-click the recordings folder and choose "
        f"\"Always Keep on This Device\", or open the folder once so "
        f"the file downloads, then retry."
    ) from last_exc
