"""
Persistent speaker profile store.

A SpeakerProfile maps a user-given display name (e.g. "John Smith") to
a voice fingerprint embedding (a 192-dim ECAPA centroid). When a new
session is processed, the recording pipeline computes per-speaker
centroids and looks them up against this store: a hit above the
similarity threshold auto-renames the new session's speaker without the
user having to type the name again.

Storage: a single JSON file in the user data directory. We don't bother
with SQLite or sqlite-vec because the volume is small (a typical user
ends up with 10-100 profiles, not 10,000) and a flat JSON file is
trivially debuggable, manually editable, and easy to back up. If the
volume ever crosses the threshold where linear kNN starts to feel slow
on the FastAPI thread, swap to faiss with no contract changes.

Concurrency: every public mutation is wrapped in a thread lock and
atomically rewrites the JSON file via tmp+rename. Read paths take the
same lock briefly, so concurrent /speaker-profiles GET requests during
a save can't see a half-written file.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)

# Default match threshold for auto-renaming. ECAPA cosine empirically:
#   same speaker, clean audio:    0.75-0.95
#   same speaker, noisy audio:    0.65-0.80
#   different speaker:            0.20-0.50
# 0.75 is the conservative cutoff: high-confidence matches auto-apply,
# borderline matches show in the UI as "Likely X (72%) — confirm?" but
# don't change anything unless the user accepts.
DEFAULT_MATCH_THRESHOLD = 0.75


@dataclass
class SpeakerProfile:
    profile_id: str
    display_name: str
    embedding: List[float]   # 192-dim, L2-normalized
    created_at: str          # ISO datetime
    updated_at: str
    confirmation_count: int = 0
    sessions_seen_in: List[str] = field(default_factory=list)


class SpeakerProfileService:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "speaker_profiles.json"
        self._lock = threading.Lock()
        self._profiles: List[SpeakerProfile] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            self._profiles = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._profiles = [SpeakerProfile(**p) for p in data]
            logger.info(f"Loaded {len(self._profiles)} speaker profiles "
                        f"from {self.path}")
        except Exception as e:
            # Don't crash the backend on a corrupt profiles file. A
            # broken profile store is recoverable (the user can rebuild
            # by relabeling speakers) — a backend that won't start is
            # not. Log loud and continue with an empty store.
            logger.exception(f"Could not load speaker profiles: {e}. "
                             f"Starting with empty store; existing JSON "
                             f"left in place at {self.path} for inspection.")
            self._profiles = []

    def _save(self) -> None:
        """Atomic write via tmp + rename. Caller must hold _lock."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([asdict(p) for p in self._profiles], indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.exception(f"Could not save speaker profiles: {e}")

    # ── Read paths ───────────────────────────────────────────────────

    def list_all(self) -> List[SpeakerProfile]:
        with self._lock:
            return list(self._profiles)

    def get(self, profile_id: str) -> Optional[SpeakerProfile]:
        with self._lock:
            for p in self._profiles:
                if p.profile_id == profile_id:
                    return p
        return None

    def find_match(
        self,
        embedding: np.ndarray,
        threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> Optional[Tuple[SpeakerProfile, float]]:
        """Best-similarity profile above threshold, or None."""
        if not self._profiles:
            return None
        best: Optional[Tuple[SpeakerProfile, float]] = None
        with self._lock:
            for p in self._profiles:
                stored = np.asarray(p.embedding, dtype=np.float32)
                if stored.shape != embedding.shape:
                    continue  # different model dimensionality — skip
                sim = float(np.dot(embedding, stored))
                if sim >= threshold and (best is None or sim > best[1]):
                    best = (p, sim)
        return best

    # ── Mutations ────────────────────────────────────────────────────

    def create(
        self,
        display_name: str,
        embedding: np.ndarray,
        session_id: str,
    ) -> SpeakerProfile:
        """Make a new profile from a freshly-labeled session speaker.

        Called when the user manually renames SPEAKER_XX → "John" in a
        session whose centroid we've already computed but that didn't
        match anything in the store.
        """
        now = datetime.now().isoformat()
        profile = SpeakerProfile(
            profile_id=uuid.uuid4().hex,
            display_name=display_name.strip(),
            embedding=[float(x) for x in embedding.tolist()],
            created_at=now,
            updated_at=now,
            confirmation_count=0,
            sessions_seen_in=[session_id] if session_id else [],
        )
        with self._lock:
            self._profiles.append(profile)
            self._save()
        logger.info(f"Created speaker profile: {profile.display_name} "
                    f"(id={profile.profile_id})")
        return profile

    def confirm_match(
        self,
        profile_id: str,
        embedding: np.ndarray,
        session_id: str,
    ) -> Optional[SpeakerProfile]:
        """User confirmed an auto-match. Refine the centroid via running
        mean and bump the confirmation count, so the profile gets MORE
        accurate the more times the user labels it."""
        with self._lock:
            for p in self._profiles:
                if p.profile_id != profile_id:
                    continue
                n = max(1, p.confirmation_count + 1)
                stored = np.asarray(p.embedding, dtype=np.float32)
                if stored.shape != embedding.shape:
                    # Should not happen, but if it does, just keep the
                    # existing centroid rather than crash.
                    logger.warning(
                        f"Embedding shape mismatch refining {profile_id}; "
                        f"keeping old centroid")
                    p.confirmation_count = n
                else:
                    new_centroid = (stored * (n - 1) + embedding) / n
                    norm = float(np.linalg.norm(new_centroid))
                    if norm > 1e-8:
                        new_centroid = new_centroid / norm
                        p.embedding = [float(x) for x in new_centroid.tolist()]
                    p.confirmation_count = n
                p.updated_at = datetime.now().isoformat()
                if session_id and session_id not in p.sessions_seen_in:
                    p.sessions_seen_in.append(session_id)
                self._save()
                return p
        return None

    def rename(self, profile_id: str, new_name: str) -> Optional[SpeakerProfile]:
        with self._lock:
            for p in self._profiles:
                if p.profile_id == profile_id:
                    p.display_name = new_name.strip()
                    p.updated_at = datetime.now().isoformat()
                    self._save()
                    return p
        return None

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            before = len(self._profiles)
            self._profiles = [p for p in self._profiles
                              if p.profile_id != profile_id]
            if len(self._profiles) < before:
                self._save()
                logger.info(f"Deleted speaker profile {profile_id}")
                return True
        return False

    def merge(
        self,
        profile_ids: List[str],
        target_name: str,
    ) -> Optional[SpeakerProfile]:
        """Combine multiple profiles into one. Centroid is a weighted
        mean by confirmation_count (well-confirmed profiles dominate).
        Used when the user spots two profiles that are clearly the same
        person — e.g. "John Smith" and "John S." created in different
        sessions because the first labeling didn't trigger a match."""
        if len(profile_ids) < 2:
            return None
        with self._lock:
            to_merge = [p for p in self._profiles
                        if p.profile_id in profile_ids]
            if len(to_merge) < 2:
                return None

            embs: list[np.ndarray] = []
            weights: list[float] = []
            sessions: set[str] = set()
            confirms = 0
            for p in to_merge:
                w = float(max(1, p.confirmation_count))
                weights.append(w)
                stored = np.asarray(p.embedding, dtype=np.float32)
                embs.append(stored * w)
                sessions.update(p.sessions_seen_in)
                confirms += p.confirmation_count

            merged = np.sum(np.stack(embs), axis=0) / sum(weights)
            norm = float(np.linalg.norm(merged))
            if norm > 1e-8:
                merged = merged / norm
            now = datetime.now().isoformat()
            new_profile = SpeakerProfile(
                profile_id=uuid.uuid4().hex,
                display_name=target_name.strip(),
                embedding=[float(x) for x in merged.tolist()],
                created_at=now,
                updated_at=now,
                confirmation_count=confirms,
                sessions_seen_in=sorted(sessions),
            )
            self._profiles = [p for p in self._profiles
                              if p.profile_id not in profile_ids]
            self._profiles.append(new_profile)
            self._save()
            logger.info(f"Merged {len(to_merge)} profiles → "
                        f"'{target_name}' ({new_profile.profile_id})")
            return new_profile
