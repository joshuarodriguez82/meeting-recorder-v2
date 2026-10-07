"""
The Co-Pilot board: one living list instead of a pile of ticks.

WHY
---
Every tick used to arrive as its own card, newest on top. A 60-minute
call produced dozens of cards, most restating the last one in different
words, and the model was only ever shown its single previous tick — so
anything older than that came back again. Nothing could be marked as
asked or dismissed, so the only way to stop seeing a suggestion was to
scroll past it, forever.

The board keeps ONE entry per suggestion:

* a new bullet that says what an existing entry says is merged into it
  (``times_suggested`` goes up) instead of becoming another line;
* each entry has a status the user controls — ``open``, ``done`` (asked
  / handled), ``dismissed`` (not useful) or ``saved`` (turned into a
  follow-up, decision or note);
* the model is shown the board, so it can see what is already there and
  what the user threw away, rather than one tick of memory.

Two bullets are "the same" when their content words overlap, or —
when the local sentence model is on this machine — when they mean the
same thing. Word overlap alone missed the commonest repeat: the model
re-asking one question with the words changed. Field board 2026-10-06,
a call on one topic: 22 open questions, none merged; the true
rephrasings shared 44-55 % of their content words (under the 60 % bar)
and scored 0.72-0.77 on the sentence model, where distinct questions on
the same topic scored 0.33-0.65.

No I/O and no LLM in the merge. The sentence model runs in
``prepare()``, which the caller runs off the event loop; ``merge()``
only reads the vectors it left. The session persists ``to_list()``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np

KINDS = ("clarifying_questions", "risks", "follow_ups")
STATUSES = ("open", "done", "dismissed", "saved")

#: Two bullets sharing this fraction of their content words say the
#: same thing. Measured against the SHORTER bullet, so a rephrasing
#: that adds a clause still merges.
SAME_THRESHOLD = 0.6

#: Cosine similarity (local sentence model) at which two bullets mean
#: the same thing. See the module docstring for the measurement.
SAME_MEANING_THRESHOLD = 0.7

_WORD = re.compile(r"[a-z0-9']+")
# Words that carry no meaning of their own in a coaching bullet; without
# this "Ask about the timeline" and "Ask about the budget" look 60 % alike.
_STOP = frozenset("""
a an the and or of to for in on at by with about is are was were be been
do does did we you they it this that these those their our your what which
who how when why will would should could can may might ask confirm clarify
check whether if any there here them us vs via per
""".split())


def _content_words(text: str) -> set:
    return {w for w in _WORD.findall((text or "").lower())
            if w not in _STOP and len(w) > 1}


def same_suggestion(a: str, b: str) -> bool:
    """Whether two bullets say the same thing."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return (a or "").strip().lower() == (b or "").strip().lower()
    overlap = len(wa & wb) / float(min(len(wa), len(wb)))
    return overlap >= SAME_THRESHOLD


@dataclass
class BoardItem:
    kind: str
    text: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "open"
    first_seen: str = ""
    last_seen: str = ""
    times_suggested: int = 1
    #: True when the latest tick brought it in or re-raised it.
    fresh: bool = True


def _local_encoder() -> Optional[Callable[[List[str]], np.ndarray]]:
    """The app's sentence model, if it is already on this machine.

    Never downloads: a first-time download in the middle of a call is
    the wrong moment, and the search index fetches it anyway. Returns
    None when unavailable, and the board falls back to word overlap."""
    try:
        from config.settings import USER_DATA_DIR
        from core import embeddings
        cache = Path(USER_DATA_DIR) / "models" / "minilm-l6"
        if embeddings._model is None and not (
                cache.is_dir() and any(cache.iterdir())):
            return None
        if not embeddings.is_available():
            return None
    except Exception:
        return None

    def encode(texts: List[str]) -> np.ndarray:
        return embeddings._get_model().encode(
            texts, normalize_embeddings=True, convert_to_numpy=True,
            show_progress_bar=False).astype(np.float32, copy=False)
    return encode


#: How `prepare()` turns text into vectors. Replaced in tests.
encoder_factory: Callable[[], Optional[Callable[[List[str]], np.ndarray]]] \
    = _local_encoder


class CopilotBoard:
    def __init__(self, items: Optional[Iterable[dict]] = None):
        self.items: List[BoardItem] = []
        #: text → unit vector, filled by prepare(). Not persisted.
        self._vectors: Dict[str, np.ndarray] = {}
        for d in items or []:
            try:
                self.items.append(BoardItem(**{
                    k: d[k] for k in BoardItem.__dataclass_fields__
                    if k in d}))
            except TypeError:
                continue

    # ── merging ticks ────────────────────────────────────────────────

    def prepare(self, tick: Dict[str, List[str]]) -> None:
        """Embed the tick's bullets and any board entries not yet
        embedded, so merge() can compare meanings. Slow on first use
        (loads the model): run it off the event loop. Any failure
        leaves merge() on word overlap."""
        texts = [t.strip() for kind in KINDS for t in (tick.get(kind) or [])
                 if isinstance(t, str) and t.strip()]
        texts += [it.text for it in self.items]
        todo = list(dict.fromkeys(t for t in texts if t not in self._vectors))
        if not todo:
            return
        try:
            encode = encoder_factory()
            if encode is None:
                return
            vecs = encode(todo)
            for t, v in zip(todo, vecs):
                self._vectors[t] = np.asarray(v, dtype=np.float32)
        except Exception:
            return

    def _same(self, existing: str, new: str) -> bool:
        if same_suggestion(existing, new):
            return True
        a, b = self._vectors.get(existing), self._vectors.get(new)
        if a is None or b is None or a.shape != b.shape:
            return False
        return float(np.dot(a, b)) >= SAME_MEANING_THRESHOLD

    def merge(self, tick: Dict[str, List[str]],
              now: Optional[str] = None) -> List[BoardItem]:
        """Fold one tick's bullets in. Returns the entries that are NEW.

        A bullet matching an existing entry of the same kind updates
        that entry instead; one matching a DISMISSED entry stays
        dismissed — the user already said no.
        """
        now = now or datetime.now().isoformat(timespec="seconds")
        for it in self.items:
            it.fresh = False
        added: List[BoardItem] = []
        for kind in KINDS:
            for text in tick.get(kind) or []:
                text = (text or "").strip()
                if not text:
                    continue
                match = next((it for it in self.items
                              if it.kind == kind
                              and self._same(it.text, text)), None)
                if match is not None:
                    match.times_suggested += 1
                    match.last_seen = now
                    match.fresh = match.status == "open"
                    continue
                item = BoardItem(kind=kind, text=text, first_seen=now,
                                 last_seen=now)
                self.items.append(item)
                added.append(item)
        return added

    # ── user actions ─────────────────────────────────────────────────

    def set_status(self, item_id: str, status: str) -> BoardItem:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        for it in self.items:
            if it.id == item_id:
                it.status = status
                it.fresh = False
                return it
        raise KeyError(item_id)

    def get(self, item_id: str) -> Optional[BoardItem]:
        return next((it for it in self.items if it.id == item_id), None)

    # ── views ────────────────────────────────────────────────────────

    def open_items(self) -> List[BoardItem]:
        return [it for it in self.items if it.status == "open"]

    def prompt_memory(self, limit: int = 30) -> str:
        """What the model is shown so it doesn't repeat itself: what is
        already on the board, what the user has handled, and what they
        threw away. Most recent first, bounded."""
        if not self.items:
            return ""
        label = {"clarifying_questions": "question", "risks": "risk",
                 "follow_ups": "follow-up"}
        groups = {
            "open": "ALREADY ON THE USER'S BOARD (do not repeat these)",
            "done": "ALREADY ASKED OR HANDLED (do not raise again)",
            "saved": "ALREADY SAVED BY THE USER (do not raise again)",
            "dismissed": "DISMISSED AS NOT USEFUL (avoid anything like these)",
        }
        recent = sorted(self.items, key=lambda i: i.last_seen,
                        reverse=True)[:limit]
        out = []
        for status, heading in groups.items():
            rows = [f"  - ({label.get(i.kind, i.kind)}) {i.text}"
                    for i in recent if i.status == status]
            if rows:
                out.append(heading + ":\n" + "\n".join(rows))
        return "\n".join(out)

    def to_list(self) -> List[dict]:
        return [asdict(it) for it in self.items]
