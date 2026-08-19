"""
``events.jsonl`` — one JSON object per **meaningful outcome**.

WHY THIS EXISTS
---------------
Five real bugs were fixed in one day off the user's logs. None was caught
by a test; 779 were green throughout. Every diagnosis meant regex-scraping
prose out of a 231 MB ``backend.log``::

    SYNC_INTEGRITY: session A1B2C3D4 mic=3421.0s lb=3418.2s window=3617.6s
        mic_gap=196.6s drift=2.8s overflows(mic/lb)=0/0 finalize_s=191.9
    [stop] finalize done in 191.9s
    Offline AEC decision: accepted=False reason=erle_non_positive erle_db=-0.4 ...
    Extension calendar: path=briefing-fallback raw=5 kept=1 dropped=4

Those lines are good lines. They stay exactly as they are — this file is
strictly additive. What it adds is the same outcomes in a shape a machine
can read without a regex per release, and in a file small enough to read
end to end.

This is NOT a second copy of backend.log. Nothing routine is written
here: no status polls, no per-chunk transcription progress, no request
log. One line per outcome that someone once had to go digging for.

PRIVACY — HARD CONSTRAINT
-------------------------
This file is designed to be **handed to a third party** in a bug report,
so it carries counts, durations, enums and opaque ids and nothing else.
No transcript text, no meeting titles, no attendee names, no email
addresses, no file paths, no secrets.

Emit sites are written to that rule, and :func:`_scrub` below enforces it
mechanically as a backstop rather than trusting every future caller:

* keys must look like ``[a-z][a-z0-9_]*`` — anything else is dropped;
* strings must be short, low-cardinality, **space-free** tokens (see
  ``_TOKEN_RE``). A value with a space, a path separator, an ``@``, a
  quote, or simply too many characters to be an enum is replaced with a
  redaction marker instead of being written. Free prose cannot reach
  this file even if a future caller passes some — the no-space rule is
  what makes that true, and it is load-bearing, not stylistic;
* anything that is not a scalar / list / dict is recorded as its type
  name, never its repr.

The one deliberate exception to "no identifiers" is ``session_id``: an
8-character random hex handle (``uuid4().hex[:8].upper()``, see
``SessionService``) that means nothing outside this machine and is the
only way to correlate the events of one recording.

ROTATION AND CAPS
-----------------
4 MiB live + 3 backups = **16 MiB ceiling**, independent of backend.log's
80 MiB. Unlike backend.log, this file is owned end-to-end by Python —
nothing else holds a handle on it — so ordinary rename-based
``RotatingFileHandler`` rotation is correct here. At roughly 250 bytes an
event and a few dozen events on a busy day, 4 MiB is on the order of a
year of history per file; the cap exists to bound a pathological loop,
not to expire normal use.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

EVENT_LOG_NAME = "events.jsonl"

#: Bumped when the meaning of an existing field changes. Adding a new
#: field or a new event name does not require a bump — readers must
#: tolerate unknown keys.
SCHEMA_VERSION = 1

DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3

# ── event names ──────────────────────────────────────────────────────
# Namespaced ``area.outcome``. Kept as constants so a typo is an
# ImportError at the emit site instead of an unsearchable string in the
# file three weeks later.
BACKEND_START = "backend.start"
BACKEND_STOP = "backend.stop"
BACKEND_PRIOR_CRASH = "backend.prior_crash"
RECOVERY_SWEEP = "recovery.sweep"
RECOVERY_SESSION = "recovery.session"
CAPTURE_STOPPED = "capture.stopped"
FINALIZE_COMPLETED = "finalize.completed"
FINALIZE_FAILED = "finalize.failed"
AUDIO_INTEGRITY = "audio.integrity"
CHANNEL_ATTRIBUTION = "channel_attribution.evaluated"
CALENDAR_IMPORT = "calendar.extension_import"
DOCUMENTS_INDEXED = "documents.indexed"

ALL_EVENTS = (
    BACKEND_START,
    BACKEND_STOP,
    BACKEND_PRIOR_CRASH,
    RECOVERY_SWEEP,
    RECOVERY_SESSION,
    CAPTURE_STOPPED,
    FINALIZE_COMPLETED,
    FINALIZE_FAILED,
    AUDIO_INTEGRITY,
    CHANNEL_ATTRIBUTION,
    CALENDAR_IMPORT,
    DOCUMENTS_INDEXED,
)

# ── scrubbing ────────────────────────────────────────────────────────
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

# Enum-ish tokens only. Excludes "/" and "\" (paths), "@" (addresses),
# quotes, and — critically — the SPACE.
#
# An earlier version of this regex allowed spaces on the theory that a
# 64-character cap was enough to stop prose. It is not, and the test
# suite caught it: "So then I told the client we would ship on Friday"
# is 48 characters and sailed straight through, as would most real
# meeting titles. Every reason code, path label, alignment mode,
# platform name and version string this app emits is space-free
# (`overlap_dominant`, `erle_non_positive`, `briefing-fallback`,
# `wallclock`, `RuntimeError`, `1.2.0`), whereas a sentence or a
# person's name essentially always contains a space. Banning it is the
# single cheapest thing that separates the two, so it stays banned —
# a new reason code must be snake_case, not a phrase.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+=\-]{0,64}$")

_REDACTED_LONG = "<redacted:not-a-token>"
_MAX_ITEMS = 40
_MAX_DEPTH = 3


def _scrub_value(value: Any, depth: int = 0) -> Any:
    """Coerce one value into something safe to hand a third party."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Durations and fractions. Three decimals is more than any
        # consumer of these needs and keeps lines short.
        try:
            return round(value, 3)
        except Exception:
            return None
    if isinstance(value, str):
        return value if _TOKEN_RE.match(value) else _REDACTED_LONG
    if depth >= _MAX_DEPTH:
        return f"<truncated:{type(value).__name__}>"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in list(value.items())[:_MAX_ITEMS]:
            key = str(k)
            if not _KEY_RE.match(key):
                continue
            out[key] = _scrub_value(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_scrub_value(v, depth + 1) for v in list(value)[:_MAX_ITEMS]]
    # Never str(value) — that is exactly how a path or an exception
    # message carrying a filename would get in.
    return f"<unsupported:{type(value).__name__}>"


def _scrub(fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in list(fields.items())[:_MAX_ITEMS]:
        if not _KEY_RE.match(str(k)):
            continue
        out[str(k)] = _scrub_value(v)
    return out


# ── writer ───────────────────────────────────────────────────────────
_LOCK = threading.Lock()
_LOGGER: Optional[logging.Logger] = None
_PATH: Optional[Path] = None
_ENABLED = True


def event_log_path() -> Path:
    """Where ``events.jsonl`` lives — next to ``backend.log``."""
    if _PATH is not None:
        return _PATH
    override = os.environ.get("MEETING_RECORDER_EVENT_LOG")
    if override:
        return Path(override)
    from utils.logger import default_log_dir
    return default_log_dir() / EVENT_LOG_NAME


def configure(
    path: Optional[Path] = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    enabled: bool = True,
) -> None:
    """Point the event log at ``path`` (tests; also the startup call)."""
    global _LOGGER, _PATH, _ENABLED
    with _LOCK:
        _teardown_locked()
        _PATH = Path(path) if path is not None else None
        _ENABLED = enabled
        if not enabled:
            return
        _LOGGER = _build_logger(
            event_log_path(), max_bytes=max_bytes, backup_count=backup_count)


def reset() -> None:
    """Close and forget the current writer. Tests only."""
    global _PATH, _ENABLED
    with _LOCK:
        _teardown_locked()
        _PATH = None
        _ENABLED = True


def _teardown_locked() -> None:
    global _LOGGER
    if _LOGGER is not None:
        for h in list(_LOGGER.handlers):
            try:
                h.close()
            except Exception:
                pass
            _LOGGER.removeHandler(h)
    _LOGGER = None


def _build_logger(path: Path, *, max_bytes: int,
                  backup_count: int) -> Optional[logging.Logger]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            str(path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
    except Exception:
        # No event log is survivable; a backend that won't start is not.
        return None
    handler.setFormatter(logging.Formatter("%(message)s"))
    # A private logger name, never reached through get_logger(), with
    # propagation off so these lines can never also land in backend.log.
    lg = logging.getLogger("meeting_recorder.events")
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    return lg


def _writer() -> Optional[logging.Logger]:
    global _LOGGER
    if _LOGGER is not None or not _ENABLED:
        return _LOGGER
    with _LOCK:
        if _LOGGER is None and _ENABLED:
            _LOGGER = _build_logger(
                event_log_path(),
                max_bytes=DEFAULT_MAX_BYTES,
                backup_count=DEFAULT_BACKUP_COUNT,
            )
    return _LOGGER


def build_record(event: str, session_id: Optional[str] = None,
                 **fields: Any) -> Dict[str, Any]:
    """The exact dict :func:`emit` would write. Public for tests."""
    rec: Dict[str, Any] = {
        # Local time with UTC offset — every other log the user copies
        # into a bug report is local, and correlating events.jsonl
        # against backend.log by eye is the whole point.
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "v": SCHEMA_VERSION,
        "event": str(event),
        "pid": os.getpid(),
    }
    if session_id is not None:
        rec["session_id"] = _scrub_value(str(session_id))
    rec.update(_scrub(fields))
    return rec


def emit(event: str, session_id: Optional[str] = None, **fields: Any) -> None:
    """Append one event. Never raises — observability must not be able to
    break the thing it observes."""
    try:
        lg = _writer()
        if lg is None:
            return
        lg.info(json.dumps(build_record(event, session_id, **fields),
                           ensure_ascii=False, separators=(",", ":")))
    except Exception:
        pass


def read_events(path: Optional[Path] = None, limit: Optional[int] = None):
    """Parse the event log back into dicts (tests, and the export)."""
    p = Path(path) if path is not None else event_log_path()
    out = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return out
    return out[-limit:] if limit else out
