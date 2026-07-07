"""Pure text helpers for the live co-pilot — no SDK imports so they can
be unit-tested without anthropic/openai installed.

The live co-pilot's biggest quality problem in the field wasn't the
model missing things — it was the model REPEATING itself: "vendor
lock-in risk" and "request an update" resurfaced in nearly every tick.
`dedup_against` drops a new suggestion when it's a near-duplicate of
anything already surfaced earlier in the same meeting, so the panel
shows genuinely new coaching instead of a broken record.
"""

from __future__ import annotations

import difflib
import re
from typing import List

# Filler phrases that are content-free for an SA co-pilot. An item is
# dropped when its normalized text is *dominated* by one of these (i.e.
# the phrase makes up most of the item), so "request an update" dies but
# "request UPS's IDT carrier commitment before the SOW locks" survives.
_FILLER_PATTERNS = (
    "request an update",
    "schedule a follow-up",
    "schedule a follow up",
    "request documentation",
    "request detailed documentation",
    "confirm the status",
    "follow up with",
    "request an update on the status",
)


def normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — the canonical
    form used for near-duplicate comparison."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_near_duplicate(a: str, b: str, threshold: float = 0.82) -> bool:
    """True when two suggestions say substantially the same thing.
    Compares normalized forms with a sequence-similarity ratio."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Containment: one being a near-subset of the other (the model
    # loves to re-emit a slightly-expanded version of a prior item).
    if na in nb or nb in na:
        return True
    return _ratio(na, nb) >= threshold


def is_filler(item: str) -> bool:
    """True when the item is generic process-chatter with no specific
    hook — the 'request an update / schedule a follow-up' pattern that
    applies to any meeting."""
    n = normalize(item)
    if not n:
        return True
    for pat in _FILLER_PATTERNS:
        if pat in n:
            # Only kill it if the filler dominates — a filler phrase
            # followed by a specific artifact/name is still useful.
            remainder = n.replace(pat, "").strip()
            if len(remainder) <= 12:  # essentially just the filler phrase
                return True
    return False


def dedup_against(
    new_items: List[str],
    prior_items: List[str],
    *,
    threshold: float = 0.82,
    drop_filler: bool = True,
) -> List[str]:
    """Return `new_items` with (a) generic filler removed and (b) any
    item that near-duplicates something in `prior_items` OR an earlier
    item in `new_items` itself dropped. Order preserved."""
    kept: List[str] = []
    for item in new_items:
        if not (item or "").strip():
            continue
        if drop_filler and is_filler(item):
            continue
        if any(is_near_duplicate(item, p, threshold) for p in prior_items):
            continue
        if any(is_near_duplicate(item, k, threshold) for k in kept):
            continue
        kept.append(item)
    return kept
