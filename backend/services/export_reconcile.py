"""Reconciliation for per-client Designated Folder exports.

Why this exists (field report 2026-08-07): a user set a client's
Designated Folder to `G:\\My Drive\\[scrubbed]`, tagged meetings to that
client, and found the folder holding only a subset of the expected
files. Nothing was broken in the copy path — `ExportWorker` had simply
never been *told* about those sessions.

Exports were enqueued from exactly three places (full process, the
individual extractors, summarize). The operations the user actually
performs to organize a back-catalogue — tagging an already-processed
meeting to a client, or setting a client's folder *after* recording —
mutated the session or the config and returned without enqueueing
anything. The artifacts stayed local forever.

The obvious patch is to add an enqueue call to every mutation site.
That was rejected: it is only correct for as long as someone remembers
to add the call to the *next* site, and its failure mode is silent —
precisely the bug being fixed.

So this module implements convergence instead of enumeration:

    Compare what a session SHOULD have in the folder against what is
    actually on disk, and enqueue whatever is missing.

Enqueue-on-mutation remains the fast path (a freshly-tagged meeting
mirrors within seconds). Reconciliation is the correctness guarantee —
idempotent, cheap, and safe to run on folder-set, on client rename, on
app start, and on demand. A missed trigger becomes a delay, never a
permanent hole.

Everything here is pure except `missing_artifacts`, which only stats
files. No copying, no network writes, no session mutation — so it is
trivially testable and can never stall the event loop on a cloud mount.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from utils.logger import get_logger

logger = get_logger(__name__)


# Mirrors ExportService._base_name. Duplicated deliberately rather than
# imported: reconciliation must predict the filename WITHOUT constructing
# an ExportService (which mkdirs its recordings dir as a side effect of
# __init__). The pair is locked together by
# test_export_reconcile.py::test_base_name_matches_export_service, which
# fails loudly if either side drifts.
def base_name(display_name: str, session_id: str) -> str:
    """Filename stem an export uses for this session."""
    if display_name:
        safe = "".join(
            c if c.isalnum() or c in " -_" else ""
            for c in display_name
        ).strip()
        return safe or session_id
    return f"session_{session_id}"


# (Session attribute, list_sessions() summary key, filename prefix).
# Order matches ExportService.export_all so the two read the same way
# side by side.
#
# Two shapes are supported on purpose. Reconciling a whole client walks
# `SessionService.list_sessions()`, which returns cheap summary dicts —
# loading every full Session just to learn "does it have a summary" would
# re-read and re-parse the entire library on a mount that may be a cloud
# drive. Single-session callers already hold a real Session object.
_ARTIFACTS: Sequence[tuple[str, str, str]] = (
    ("segments", "has_transcript", "transcript"),
    ("summary", "has_summary", "summary"),
    ("action_items", "has_action_items", "action_items"),
    ("decisions", "has_decisions", "decisions"),
    ("requirements", "has_requirements", "requirements"),
)


def _field(session, attr: str, summary_key: str):
    """Read a field from either a Session object or a list_sessions()
    summary dict."""
    if isinstance(session, dict):
        # Prefer the explicit has_* boolean; fall back to the raw value
        # for keys the summary carries verbatim (summary, decisions, …).
        if summary_key in session:
            return session.get(summary_key)
        return session.get(attr)
    return getattr(session, attr, None)


def expected_artifacts(session) -> List[str]:
    """Filenames this session should have in its Designated Folder.

    Driven by what the session actually holds: a meeting with only a
    transcript expects exactly one file. An unprocessed session expects
    none, so it can never be counted as "missing" and drive a pointless
    re-export.
    """
    sid = _field(session, "session_id", "session_id") or ""
    display = _field(session, "display_name", "display_name") or ""
    # SessionService.list_sessions() substitutes a "Session <id>"
    # placeholder for an empty display_name, but ExportService._base_name
    # sees the real (empty) value and stems the file "session_<id>".
    # Undo the placeholder so reconciliation predicts the filename the
    # exporter actually wrote — otherwise every untitled meeting looks
    # permanently missing and re-exports on every pass.
    if display == f"Session {sid}":
        display = ""
    stem = base_name(display, sid)
    out: List[str] = []
    for attr, summary_key, prefix in _ARTIFACTS:
        if _field(session, attr, summary_key):
            out.append(f"{prefix}_{stem}.txt")
    return out


def missing_artifacts(session, folder: Optional[str]) -> List[str]:
    """Expected filenames absent from `folder`.

    Returns [] when the session has nothing to export or no folder is
    configured — both mean "nothing owed", not "everything missing".

    An unreadable / offline folder raises OSError from the stat, which
    the caller treats as "can't tell, assume owed" rather than "all
    present". Guessing "present" here would let a disconnected Drive
    mount mark a client fully mirrored when nothing had been written.
    """
    expected = expected_artifacts(session)
    if not expected or not folder:
        return []
    base = Path(folder).expanduser()
    if not base.is_dir():
        # Folder configured but not currently there (Drive offline, path
        # typo, external disk unplugged). Everything is outstanding.
        return list(expected)
    missing: List[str] = []
    for name in expected:
        try:
            if not (base / name).exists():
                missing.append(name)
        except OSError as e:
            logger.warning(f"Reconcile stat failed for {name}: {e}")
            missing.append(name)
    return missing


def needs_export(session, folder: Optional[str]) -> bool:
    """True when at least one expected artifact isn't in the folder."""
    return bool(missing_artifacts(session, folder))


def normalize_client(name: str) -> str:
    """Canonical client key — must match
    ClientConfigService._normalize, which is what the export path uses
    to resolve a Designated Folder.

    The 2026-08-07 report also exposed a split between this and the
    Clients UI, which compared `session.client === selected` with an
    exact, case-sensitive `===`. A session tagged "[scrubbed]" therefore
    exported into the "[scrubbed]" folder (resolved case-insensitively) while
    being invisible under "[scrubbed]" in the UI (compared case-sensitively).
    Everything that groups sessions by client goes through this.
    """
    return (name or "").strip().lower()


def sessions_for_client(sessions, client: str) -> list:
    """Every session tagged to `client`, matched case-insensitively and
    ignoring surrounding whitespace."""
    key = normalize_client(client)
    if not key:
        return []
    return [
        s for s in sessions
        if normalize_client(_field(s, "client", "client") or "") == key
    ]
