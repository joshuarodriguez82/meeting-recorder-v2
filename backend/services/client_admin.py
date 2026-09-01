"""
Renaming, merging and deleting clients and projects.

WHY THIS EXISTS
---------------
There was no way to remove or correct a client. ClientConfigService
carried ``rename()`` and ``delete()`` but nothing exposed them, and both
touch only the CONFIG entry — folders and portal binding — never the
meetings.

That gap produced a live state in a real install: both "Northwind" and
"Nortwind" configured, the misspelling holding no folders, and every
meeting tagged to the typo orphaned from the real account's data.
Deleting the typo's config would not have repaired it — the meetings
would still carry the wrong tag and still be missing from the right
client. The operation that actually repairs it is a MERGE, so that is
the primary operation here and delete is the lesser case.

THE RULE THAT OUTRANKS EVERYTHING ELSE
--------------------------------------
**Deleting a client never deletes a recording.** A client is a tag plus
some folder configuration; the meetings are the user's data and the
entire point of the product. There is deliberately no code path in this
module that removes a session, and a test asserts the delete plan cannot
even name one.

SHAPE
-----
Planning is pure: every ``plan_*`` function takes the session list and
the existing client names as arguments and returns what WOULD change,
with counts. The caller then applies it. That split exists so the
dangerous decisions — is this a merge? whose folders win? which sessions
move? — are all testable without a filesystem, and so the UI can show
the user what is about to happen before they agree to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from utils.logger import get_logger

logger = get_logger(__name__)

#: Config keys a merge reasons about. Anything else on the entry is
#: carried through untouched by merge_config's dict union.
_MERGEABLE_KEYS = ("export_folder", "knowledge_folder", "customer_id")


def _norm(name: Optional[str]) -> str:
    """Case- and whitespace-insensitive key, matching how
    ClientConfigService stores entries. Two names that normalise the
    same ARE the same client."""
    return (name or "").strip().lower()


@dataclass
class RenamePlan:
    old: str
    new: str
    #: Sessions whose client tag moves. Only the SOURCE's sessions — the
    #: target's are already where they belong.
    session_ids: List[str] = field(default_factory=list)
    #: True when the target name already exists, so this folds two
    #: clients into one rather than relabelling a single one. The word
    #: matters on screen: users accept a rename readily and should
    #: think harder about a merge.
    is_merge: bool = False
    is_noop: bool = False
    #: Stated explicitly so the flag is greppable and the guarantee is
    #: visible at the call site, not only in a docstring.
    deletes_recordings: bool = False


@dataclass
class DeletePlan:
    client: str
    #: Sessions whose client tag is cleared. Empty when the caller asked
    #: to remove the configuration only.
    session_ids: List[str] = field(default_factory=list)
    untag_sessions: bool = False
    deletes_recordings: bool = False


def _sessions_for_client(sessions: Sequence[Dict[str, Any]],
                         client: str) -> List[str]:
    key = _norm(client)
    return [str(s.get("session_id")) for s in sessions
            if _norm(s.get("client")) == key and s.get("session_id")]


def plan_rename(
    old: str,
    new: Optional[str],
    *,
    existing_clients: Sequence[str],
    sessions: Sequence[Dict[str, Any]],
) -> RenamePlan:
    """What renaming `old` to `new` would do.

    A target that already exists makes this a merge. A target that
    differs from the source only in case is NOT a merge — "acme" to
    "Acme" is one client wearing better capitalisation, and folding a
    client into itself would report work that never happened.
    """
    if not (new or "").strip():
        raise ValueError("A new client name is required.")
    new = new.strip()
    old_key, new_key = _norm(old), _norm(new)

    if old == new:
        return RenamePlan(old=old, new=new, is_noop=True)

    existing = {_norm(c) for c in existing_clients}
    # Same normalised key => same client, so this only changes how the
    # name is displayed.
    is_merge = new_key != old_key and new_key in existing

    return RenamePlan(
        old=old,
        new=new,
        session_ids=_sessions_for_client(sessions, old),
        is_merge=is_merge,
    )


def merge_config(
    *,
    source: Optional[Dict[str, Any]],
    target: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The surviving config when `source` folds into `target`.

    **The target wins every field it has.** This is the dangerous
    direction: the entry being folded in is usually the mistake — a typo
    with no folders — and letting it win would silently unconfigure a
    working client. The source only fills gaps the target left empty,
    which is the useful half of a merge.
    """
    if target is None:
        return dict(source or {})
    merged = dict(target)
    for key in _MERGEABLE_KEYS:
        if not (merged.get(key) or "") and (source or {}).get(key):
            merged[key] = source[key]
    return merged


def plan_delete(
    client: str,
    *,
    sessions: Sequence[Dict[str, Any]],
    untag_sessions: bool,
) -> DeletePlan:
    """What deleting `client` would do.

    Two modes, both of which keep every recording:

      untag_sessions=True   the meetings lose their client tag and stay
                            in the archive, untagged.
      untag_sessions=False  only the configuration goes; the meetings
                            keep the tag, so re-creating the client
                            later restores the association.

    The count is returned so the UI can say "this affects 34 meetings"
    before the user agrees. A delete that does not say how many meetings
    it touches is one nobody can consent to.
    """
    return DeletePlan(
        client=client,
        session_ids=(_sessions_for_client(sessions, client)
                     if untag_sessions else []),
        untag_sessions=untag_sessions,
    )


def plan_project_rename(
    client: str,
    old: str,
    new: Optional[str],
    *,
    sessions: Sequence[Dict[str, Any]],
) -> List[str]:
    """Sessions whose project tag would move. Scoped to one client:
    "Phase 1" under Acme and "Phase 1" under Globex are different
    projects that happen to share a name."""
    if not (new or "").strip():
        raise ValueError("A new project name is required.")
    return _sessions_for_project(sessions, client, old)


def plan_project_delete(
    client: str,
    project: str,
    *,
    sessions: Sequence[Dict[str, Any]],
) -> List[str]:
    """Sessions whose project tag would be cleared. Projects have no
    store of their own — they exist only as tags — so deleting one is
    exactly this and nothing else."""
    return _sessions_for_project(sessions, client, project)


def _sessions_for_project(sessions: Sequence[Dict[str, Any]],
                          client: str, project: str) -> List[str]:
    ckey, pkey = _norm(client), _norm(project)
    return [str(s.get("session_id")) for s in sessions
            if _norm(s.get("client")) == ckey
            and _norm(s.get("project")) == pkey
            and s.get("session_id")]


def rekey_documents(recordings_dir, old: str, new: str) -> int:
    """Point indexed documents at the new client name. Returns how many
    sidecars were rewritten.

    Without this a merge would leave the folded client's documents
    filed under a name nothing looks up any more — the "0 documents"
    failure again, self-inflicted this time.

    An unreadable sidecar is skipped rather than fatal: one corrupt file
    must not abandon a rename halfway through, leaving sessions moved
    and documents not.
    """
    doc_dir = Path(recordings_dir) / "doc_index"
    if not doc_dir.is_dir():
        return 0
    old_key = _norm(old)
    changed = 0
    for path in sorted(doc_dir.glob("doc_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.debug(f"Skipping unreadable doc sidecar {path.name}: {e}")
            continue
        if _norm(payload.get("client")) != old_key:
            continue
        payload["client"] = new
        try:
            path.write_text(json.dumps(payload), encoding="utf-8")
            changed += 1
        except OSError as e:
            logger.warning(f"Could not re-key {path.name}: {e}")
    return changed
