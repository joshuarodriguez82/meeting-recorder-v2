"""
Owner normalisation and grouping — the shared logic behind the
Follow Ups / Commitments "owner" filter.

LLM extraction produces owner names as free text, so one real person
ends up scattered across many spellings ("Josh", "Josh (AWS)", "[scrubbed]", "Joshua") and multi-owner strings ("Mark/Josh") are opaque
blobs that belong to nobody individually. This module is the ONE place
that:

  1. Splits a multi-owner string into individual people.
  2. Auto-normalises spelling/format variants that are unambiguously
     the same string in different clothing (case, whitespace, trailing
     punctuation, a trailing "(Org)" suffix).
  3. Suggests — never silently applies — judgement-call merges of
     names that are *probably* the same person (first-token match,
     nickname/prefix relationships), for the user to confirm.
  4. Persists confirmed merges (aliases) so they survive restarts.

Both `commitments_service.py` (owner filtering) and the frontend
Follow Ups view use this module's rules. The frontend can't literally
import Python, so `src/lib/owner-grouping.ts` is a hand-kept mirror of
the pure split/normalise functions below — same pattern already used
in this codebase for item-status hashing (`compute_item_hash` /
`computeItemHash`) and for the follow-up/decision markdown parsers
mirrored between `insights_service.py` and the *-view.tsx files. Keep
the two files in lock-step; the alias STORE itself (the stateful,
persisted part) only exists here and is reached by the frontend
through the `/owners/*` HTTP endpoints in server.py.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


# ── Tier 1: splitting a multi-owner string ─────────────────────────────
#
# Split on '/', '&', and the standalone word "and". Deliberately do
# NOT split on comma: real names in this data contain commas (calendar
# organisers show up as "[scrubbed], Bob Jr. [US-US]"), and a comma
# split would shred a single name into two fake people. If you're
# tempted to "improve" this by adding comma — don't; that's the one
# regression this module exists to prevent. See
# backend/tests/test_owner_service.py::test_comma_is_not_split.

_SPLIT_RE = re.compile(r"\s*(?:/|&|\band\b)\s*", re.IGNORECASE)


def split_owners(raw: str) -> List[str]:
    """Split a raw owner string into individual owner pieces.

    "Mark/Josh" -> ["Mark", "Josh"]
    "Melissa & Kendra" -> ["Melissa", "Kendra"]
    "Osmo/Craig/Josh" -> ["Osmo", "Craig", "Josh"]
    "[scrubbed], Bob Jr." -> ["[scrubbed], Bob Jr."]   (comma preserved)

    Case-insensitive de-dupe within the same raw string (so "Josh /
    Josh" from a sloppy extraction doesn't produce a duplicate), order
    preserved otherwise.
    """
    s = (raw or "").strip()
    if not s:
        return []
    out: List[str] = []
    seen = set()
    for piece in _SPLIT_RE.split(s):
        piece = piece.strip()
        if not piece:
            continue
        key = piece.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(piece)
    return out


# ── Tier 2: safe normalisation of a single owner piece ─────────────────
#
# Only transformations that are unambiguously the same string in
# different clothing. None of these can merge two different people.

_ORG_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")
_TRAILING_PUNCT_RE = re.compile(r"[\s.,;:!?]+$")


def normalize_owner(raw: str) -> Tuple[str, str]:
    """Normalise a single (already-split) owner piece.

    Returns (key, display):
      - key: lowercase, whitespace-collapsed, trailing-punctuation and
        trailing-org-suffix stripped — used for matching/de-dupe.
      - display: same transforms, original casing preserved — used
        for showing the person's name in the UI.

    "Josh (AWS)"  -> ("josh", "Josh")
    "  Josh  "    -> ("josh", "Josh")
    "JOSH."       -> ("josh", "JOSH")
    "Jake (AWS)"  -> ("jake", "Jake")   # different base name: NOT the
                                        # same key as "Josh (AWS)"
    """
    s = (raw or "").strip()
    # Strip trailing punctuation / org-suffix / punctuation again, in
    # that order, to catch "Josh (AWS)." as well as "Josh (AWS)".
    s = _TRAILING_PUNCT_RE.sub("", s).strip()
    stripped = _ORG_SUFFIX_RE.sub("", s).strip()
    if stripped:  # don't collapse a name that IS just "(AWS)" to ""
        s = stripped
    s = _TRAILING_PUNCT_RE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower(), s


# ── Tier 3: suggested (never auto-applied) judgement-call merges ───────


def _first_token(key: str) -> str:
    return key.split(" ", 1)[0] if key else ""


def _looks_related(a: str, b: str) -> bool:
    """True if two normalised keys are plausibly the same person:
    same first token ("josh" / "[scrubbed]"), or one is a strict
    prefix of the other ("josh" / "joshua"). Both are heuristics for
    a human to confirm, not to auto-apply — see module docstring."""
    if a == b:
        return False
    fa, fb = _first_token(a), _first_token(b)
    if fa and fa == fb:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 3 and longer.startswith(shorter):
        return True
    return False


class _UnionFind:
    def __init__(self, items: Iterable[str]):
        self.parent = {k: k for k in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class SuggestedMember:
    key: str
    display: str
    count: int


@dataclass
class SuggestedGroup:
    group_id: str
    suggested_canonical: str
    members: List[SuggestedMember]


def suggest_groups(
    counts: Dict[str, int],
    display: Dict[str, str],
    already_grouped_keys: Optional[Iterable[str]] = None,
    rejected_pairs: Optional[Iterable[Tuple[str, str]]] = None,
) -> List[SuggestedGroup]:
    """Compute suggested merge groups over a set of tier-2 normalised
    keys. Purely advisory — callers must never apply these without
    explicit user confirmation.

    `already_grouped_keys` excludes keys that already belong to a
    confirmed alias (nothing to suggest, they're already merged).
    `rejected_pairs` excludes specific pairs the user already said "no"
    to, so a dismissed suggestion doesn't keep resurfacing.
    """
    grouped = set(already_grouped_keys or ())
    rejected = {frozenset(p) for p in (rejected_pairs or ())}
    keys = sorted(k for k in counts if k and k not in grouped)
    if len(keys) < 2:
        return []

    uf = _UnionFind(keys)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            if frozenset((a, b)) in rejected:
                continue
            if _looks_related(a, b):
                uf.union(a, b)

    components: Dict[str, List[str]] = defaultdict(list)
    for k in keys:
        components[uf.find(k)].append(k)

    groups: List[SuggestedGroup] = []
    for members in components.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda k: (-counts.get(k, 0), k))
        groups.append(SuggestedGroup(
            group_id=members[0],
            suggested_canonical=display.get(members[0], members[0]),
            members=[
                SuggestedMember(key=m, display=display.get(m, m),
                                 count=counts.get(m, 0))
                for m in members
            ],
        ))
    groups.sort(key=lambda g: -sum(m.count for m in g.members))
    return groups


# ── Aggregating raw owner strings across items ─────────────────────────


def aggregate_raw_owners(
    raw_strings: Iterable[str],
) -> Tuple[Dict[str, int], Dict[str, str]]:
    """Split + tier-2-normalise every raw owner string, and count how
    many items (raw strings) touch each resulting key. An item with
    several owners contributes one count to EACH person it names —
    this is a per-person count, not a per-item count, so summing it
    across people will legitimately exceed the item total; callers
    must report the item total separately rather than imply the sum
    is it."""
    counts: Dict[str, int] = {}
    variant_freq: Dict[str, Counter] = defaultdict(Counter)
    for raw in raw_strings:
        for piece in split_owners(raw):
            key, disp = normalize_owner(piece)
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            variant_freq[key][disp] += 1
    display = {
        key: ctr.most_common(1)[0][0] for key, ctr in variant_freq.items()
    }
    return counts, display


# ── Alias persistence ───────────────────────────────────────────────────


@dataclass
class OwnerAlias:
    id: str
    canonical: str
    members: List[str]  # tier-2 normalised keys
    created_at: str


class OwnerAliasStore:
    """Thread-safe JSON-on-disk store for confirmed owner merges.

    Lives at `<recordings_dir>/owner_aliases.json` — alongside
    `client_configs.json` — so it travels with the session library and
    syncs between machines the same way (see the comment in
    server.py's `load_settings` about why config lives there rather
    than in USER_DATA_DIR).

    Mirrors ClientConfigService's structure and atomic-write pattern:
    temp file in the same directory, fsync, os.replace, so a crash
    never leaves a half-written JSON. A missing or corrupt file
    degrades to "no aliases" (no grouping) rather than raising —
    losing the merges is much better than the app failing to start.
    """

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "owner_aliases.json"
        self._lock = threading.Lock()

    # ── low-level read/write ────────────────────────────────────────

    def _read_raw(self) -> dict:
        if not self._path.exists():
            return {"aliases": {}, "rejected": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("owner_aliases.json is not a JSON object")
            data.setdefault("aliases", {})
            data.setdefault("rejected", [])
            if not isinstance(data["aliases"], dict):
                data["aliases"] = {}
            if not isinstance(data["rejected"], list):
                data["rejected"] = []
            return data
        except Exception as e:
            logger.warning(
                f"owner_aliases.json unreadable ({e}); degrading to no "
                f"owner grouping rather than raising")
            return {"aliases": {}, "rejected": []}

    def _write_raw(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── reads ────────────────────────────────────────────────────────

    def list_all(self) -> List[OwnerAlias]:
        with self._lock:
            raw = self._read_raw()
        out = []
        for aid, entry in (raw.get("aliases") or {}).items():
            if not isinstance(entry, dict):
                continue
            out.append(OwnerAlias(
                id=aid,
                canonical=(entry.get("canonical") or aid),
                members=sorted(set(entry.get("members") or [])),
                created_at=entry.get("created_at") or "",
            ))
        out.sort(key=lambda a: a.canonical.lower())
        return out

    def member_keys(self) -> set:
        return {m for a in self.list_all() for m in a.members}

    def rejected_pairs(self) -> List[Tuple[str, str]]:
        with self._lock:
            raw = self._read_raw()
        out = []
        for p in raw.get("rejected") or []:
            if isinstance(p, (list, tuple)) and len(p) == 2:
                out.append((p[0], p[1]))
        return out

    # ── mutations ────────────────────────────────────────────────────

    def create(self, canonical: str, members: List[str]) -> OwnerAlias:
        canonical = (canonical or "").strip()
        norm_members = sorted({
            (m or "").strip().lower() for m in members if (m or "").strip()
        })
        if not canonical:
            raise ValueError("alias needs a canonical display name")
        if len(norm_members) < 1:
            raise ValueError("alias needs at least one member")
        alias_id = uuid.uuid4().hex
        with self._lock:
            raw = self._read_raw()
            # A key can only belong to one alias at a time — pull these
            # members out of any other group first so merges stay a
            # clean partition, not overlapping sets.
            for aid, entry in list(raw["aliases"].items()):
                remaining = [
                    m for m in entry.get("members", [])
                    if m not in norm_members
                ]
                if remaining:
                    entry["members"] = remaining
                else:
                    del raw["aliases"][aid]
            raw["aliases"][alias_id] = {
                "canonical": canonical,
                "members": norm_members,
                "created_at": datetime.now().isoformat(),
            }
            self._write_raw(raw)
        return OwnerAlias(id=alias_id, canonical=canonical,
                           members=norm_members, created_at="")

    def update(
        self,
        alias_id: str,
        canonical: Optional[str] = None,
        add_members: Optional[List[str]] = None,
        remove_members: Optional[List[str]] = None,
    ) -> Optional[OwnerAlias]:
        """Rename a group and/or add/remove members (manual merge into
        an existing group / manual split out of one). Returns the
        updated alias, or None if the group no longer exists (e.g. its
        last member was just removed, which deletes the group —
        removing a member is how "split" is implemented: the item
        falls back to being its own unaliased entry)."""
        with self._lock:
            raw = self._read_raw()
            entry = raw["aliases"].get(alias_id)
            if not entry:
                return None
            if canonical is not None and canonical.strip():
                entry["canonical"] = canonical.strip()
            members = set(entry.get("members") or [])
            if add_members:
                added = {(m or "").strip().lower() for m in add_members if (m or "").strip()}
                members |= added
                for aid, other in raw["aliases"].items():
                    if aid == alias_id:
                        continue
                    other["members"] = [
                        m for m in other.get("members", []) if m not in added
                    ]
            if remove_members:
                for m in remove_members:
                    members.discard((m or "").strip().lower())
            entry["members"] = sorted(members)
            if not entry["members"]:
                del raw["aliases"][alias_id]
                self._write_raw(raw)
                return None
            self._write_raw(raw)
            return OwnerAlias(
                id=alias_id, canonical=entry["canonical"],
                members=entry["members"],
                created_at=entry.get("created_at") or "")

    def delete(self, alias_id: str) -> bool:
        """Fully ungroup — every member reverts to being its own
        (tier-2-normalised) entry. Reversible by design: this is the
        opposite of create()."""
        with self._lock:
            raw = self._read_raw()
            if alias_id in raw["aliases"]:
                del raw["aliases"][alias_id]
                self._write_raw(raw)
                return True
            return False

    def reject(self, key_a: str, key_b: str) -> None:
        """Record that the user declined a suggested pair so it stops
        resurfacing. Purely advisory bookkeeping — has no effect on
        resolved owners, only on future suggestion lists."""
        a = (key_a or "").strip().lower()
        b = (key_b or "").strip().lower()
        if not a or not b or a == b:
            return
        with self._lock:
            raw = self._read_raw()
            pair = sorted([a, b])
            if pair not in [sorted(p) for p in raw["rejected"] if isinstance(p, (list, tuple)) and len(p) == 2]:
                raw["rejected"].append(pair)
                self._write_raw(raw)


# ── Resolving an item's raw owner field to canonical people ────────────


class AliasIndex:
    """O(1)-lookup snapshot of an OwnerAliasStore, safe to build once
    and reuse across many resolve_owners() calls in a loop (e.g.
    CommitmentsService.list_all iterating hundreds of commitments)
    without re-reading the JSON file on every item."""

    def __init__(self, aliases: Optional[List[OwnerAlias]] = None):
        self.by_member: Dict[str, OwnerAlias] = {}
        for a in (aliases or []):
            for m in a.members:
                self.by_member[m] = a


def load_alias_index(store: Optional[OwnerAliasStore]) -> AliasIndex:
    """Build an AliasIndex from a store, degrading to an empty (no
    grouping) index if the store is missing or unreadable — mirrors
    OwnerAliasStore's own degrade-don't-raise behaviour so a caller
    that forgets to handle the None case still gets a safe result."""
    if store is None:
        return AliasIndex()
    try:
        return AliasIndex(store.list_all())
    except Exception as e:
        logger.warning(
            f"Owner alias store unreadable ({e}); degrading to no "
            f"owner grouping rather than raising")
        return AliasIndex()


def resolve_owners(
    raw: str, alias_index: Optional[AliasIndex] = None,
) -> List[str]:
    """Resolve a raw owner field (possibly multi-owner) to the list of
    canonical display names it belongs to. This is what both the
    Commitments owner filter and the Follow Ups filter dropdown are
    built on.

    "Mark/Josh" with no aliases         -> ["Mark", "Josh"]
    "Josh (AWS)"                        -> ["Josh"]
    "Joshua" with a Josh/Joshua alias   -> ["Josh"]
    """
    out: List[str] = []
    seen = set()
    for piece in split_owners(raw):
        key, display = normalize_owner(piece)
        if not key:
            continue
        canonical = display
        alias = alias_index.by_member.get(key) if alias_index else None
        if alias:
            canonical = alias.canonical
        dedupe_key = canonical.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(canonical)
    return out
