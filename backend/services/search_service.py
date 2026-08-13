"""
Semantic search across all sessions.

Each session's transcript chunks + their embeddings live in a pair of
sibling files next to the session JSON:

  recordings/session_<id>.json                 (existing)
  recordings/session_<id>.embeddings.npz       (vectors)
  recordings/session_<id>.embeddings.json      (everything else)

The pair together holds the same information the old pickle sidecar
did:
  {
    "model_id": str,                # see core.embeddings.MODEL_ID
    "session_id": str,
    "chunks": [{"start_s", "end_s", "text"}, ...],
    "embeddings": np.ndarray (N, dim) float32, L2-normalized
  }
(embeddings live in the .npz; everything else in the .json — see
utils/embedding_store.py).

Security note: this used to be a single pickle file, and pickle.loads()
on an attacker-controlled file is arbitrary code execution. These
sidecars are not purely local — the recordings directory is synced
across machines via Google Drive / OneDrive, with an archive folder
explicitly configured for cross-machine replication — so a compromised
cloud account or a bad sync could have handed an attacker RCE with no
local access at all. See utils/embedding_store.py for the full
rationale. Legacy `.embeddings.pkl` files from before this migration
are never read, not even to migrate them — encountering one is treated
identically to "not indexed yet" and the index is rebuilt from the
session transcript instead.

SearchService keeps a flat in-memory matrix lazy-loaded from those files
on first query. Query is one numpy dot product over the whole matrix —
plenty fast for the 100s-of-thousands-of-chunks scale a single user
will ever produce on their laptop. invalidate() drops the cache so
the next query picks up newly-indexed sessions.

LMA gap analysis 2026-08-07: the same matrix now also loads per-client
document chunks from <recordings_dir>/doc_index/*.npz (see
services/document_service.py) — one npz+json pair per indexed document,
same model_id/embeddings conventions, so a document chunk and a
transcript chunk are directly comparable by cosine similarity in one
dot product. Each metadata row carries "source": "session" | "document"
so results and the client filter can tell them apart; session-hit
result dicts keep every existing field unchanged (the frontend depends
on them).

When a user runs a session through /process, recording_service triggers
index_session() to embed + persist that session's chunks. Old sessions
processed before this feature shipped need a one-time backfill
(POST /search/index/backfill, or use the Settings UI button).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from utils.embedding_store import delete_payload, load_payload, save_payload
from utils.logger import get_logger

logger = get_logger(__name__)


def _embeddings_npz_path(recordings_dir: Path, session_id: str) -> Path:
    return recordings_dir / f"session_{session_id}.embeddings.npz"


def _embeddings_json_path(recordings_dir: Path, session_id: str) -> Path:
    return recordings_dir / f"session_{session_id}.embeddings.json"


def _embeddings_legacy_pkl_path(recordings_dir: Path, session_id: str) -> Path:
    """Pre-migration pickle sidecar. NEVER loaded — see module docstring
    and utils/embedding_store.py. Only ever checked for existence (to
    decide "needs backfill") or unlinked (cleanup)."""
    return recordings_dir / f"session_{session_id}.embeddings.pkl"


class SearchService:
    """In-memory semantic search index built lazily from per-session
    .npz/.json sidecar file pairs."""

    def __init__(self, session_service):
        self._session_service = session_service
        self._lock = threading.Lock()
        self._loaded = False
        # Flat (N_chunks, dim) matrix concatenated across all sessions,
        # plus a parallel metadata list with session_id + chunk fields
        # so we can reconstruct provenance for each top-K hit.
        self._matrix: Optional[np.ndarray] = None
        self._metadata: List[dict] = []

    # ── Index management ────────────────────────────────────────────

    def invalidate(self) -> None:
        """Drop the in-memory cache so the next search re-loads. Cheap;
        called after every successful index_session()."""
        with self._lock:
            self._loaded = False
            self._matrix = None
            self._metadata = []

    def index_status(self) -> dict:
        """How many sessions are currently in the index, plus the total
        session count so the UI can show a backfill progress hint.

        A session with only a legacy .embeddings.pkl (no .npz yet)
        counts as NOT indexed — it needs a rebuild, same as a session
        that was never indexed at all."""
        try:
            recordings_dir = self._session_service.recordings_dir
        except Exception:
            return {"total_sessions": 0, "indexed_sessions": 0,
                    "model_id": _model_id(), "available": False}

        sessions = self._session_service.list_sessions()
        indexed = 0
        for s in sessions:
            if _embeddings_npz_path(recordings_dir, s["session_id"]).exists():
                indexed += 1
        from core.embeddings import is_available
        return {
            "total_sessions": len(sessions),
            "indexed_sessions": indexed,
            "model_id": _model_id(),
            "available": is_available(),
        }

    def index_session(self, session_id: str) -> bool:
        """Embed one session's transcript chunks and persist to disk.

        Returns True on success, False if the session has no transcript
        yet or sentence-transformers isn't installed. Invalidates the
        in-memory cache so the next search picks up the new chunks.
        """
        from core.embeddings import (
            chunk_segments, embed_chunks, is_available, MODEL_ID,
        )
        if not is_available():
            return False
        session = self._session_service.load_full(session_id)
        if not session or not session.segments:
            return False
        segments_for_chunking = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in session.segments
        ]
        chunks = chunk_segments(segments_for_chunking)
        if not chunks:
            return False
        embeddings = embed_chunks(chunks)
        recordings_dir = self._session_service.recordings_dir
        npz_path = _embeddings_npz_path(recordings_dir, session_id)
        json_path = _embeddings_json_path(recordings_dir, session_id)
        meta = {
            "model_id": MODEL_ID,
            "session_id": session_id,
            "chunks": [c.to_dict() for c in chunks],
        }
        save_payload(npz_path, json_path, embeddings=embeddings, meta=meta)
        # This session is freshly rebuilt from source — any legacy
        # pickle sidecar for it is now redundant AND a standing load
        # hazard if some future code path ever regressed to reading it.
        # Clean it up rather than leave it lying around.
        legacy = _embeddings_legacy_pkl_path(recordings_dir, session_id)
        if legacy.exists():
            delete_payload(legacy)
            logger.info(f"Removed legacy pickle sidecar {legacy.name}")
        self.invalidate()
        logger.info(
            f"Indexed session {session_id}: {len(chunks)} chunks "
            f"× {embeddings.shape[1]}-dim → {npz_path.name}")
        return True

    def delete_session_index(self, session_id: str) -> bool:
        """Remove a session's embedding sidecar (new format and any
        leftover legacy pickle). Called by SessionService when a
        session is deleted."""
        try:
            recordings_dir = self._session_service.recordings_dir
        except Exception:
            return False
        removed = delete_payload(
            _embeddings_npz_path(recordings_dir, session_id),
            _embeddings_json_path(recordings_dir, session_id),
            _embeddings_legacy_pkl_path(recordings_dir, session_id),
        )
        if removed:
            self.invalidate()
        return removed

    # ── Query ───────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        client: Optional[str] = None,
        project: Optional[str] = None,
    ) -> List[dict]:
        """Return top-K semantically-similar chunks for `query`.

        Each result dict contains: session_id, display_name, started_at,
        client, project, start_s, end_s, text, similarity (cosine 0-1).

        Filters narrow the candidate set BEFORE ranking, so a low-K
        query within "ACME / Q3 Planning" doesn't get drowned out by
        more-similar chunks from other clients.
        """
        from core.embeddings import embed_query, is_available
        if not is_available() or not query.strip():
            return []
        self._load_index()
        if self._matrix is None or len(self._metadata) == 0:
            return []

        # Build a session-id → metadata lookup once, including the
        # client/project columns for filtering.
        sess_lookup: Dict[str, dict] = {
            s["session_id"]: s for s in self._session_service.list_sessions()
        }

        # Mask out chunks whose parent session doesn't pass the filter.
        # Doing this on the cosine vector (not the matrix) keeps it cheap.
        sims = self._matrix @ embed_query(query)
        if client or project:
            mask = np.ones(len(sims), dtype=bool)
            for i, meta in enumerate(self._metadata):
                if meta.get("source") == "document":
                    # Documents have no "project" — a project-scoped
                    # search has nothing to match against, so exclude
                    # them entirely rather than let an unrelated
                    # document leak into a project-filtered result set.
                    if project:
                        mask[i] = False
                        continue
                    if client and (meta.get("client") or "") != client:
                        mask[i] = False
                    continue
                sess = sess_lookup.get(meta["session_id"])
                if not sess:
                    mask[i] = False
                    continue
                if client and (sess.get("client") or "") != client:
                    mask[i] = False
                    continue
                if project and (sess.get("project") or "") != project:
                    mask[i] = False
            sims = np.where(mask, sims, -np.inf)

        # Top-K via argpartition (O(N), faster than full sort for large
        # N) followed by a sort over just those K.
        if top_k >= len(sims):
            top_indices = np.argsort(-sims)
        else:
            unsorted = np.argpartition(-sims, top_k)[:top_k]
            top_indices = unsorted[np.argsort(-sims[unsorted])]

        results: List[dict] = []
        for idx in top_indices:
            sim = float(sims[int(idx)])
            if not np.isfinite(sim):
                continue
            meta = self._metadata[int(idx)]
            if meta.get("source") == "document":
                results.append({
                    "source": "document",
                    "doc_name": meta.get("doc_name", ""),
                    "doc_path": meta.get("doc_path", ""),
                    "client": meta.get("client", ""),
                    "text": meta["text"],
                    "similarity": sim,
                })
                continue
            sess = sess_lookup.get(meta["session_id"], {})
            results.append({
                # Existing fields are untouched — the frontend depends
                # on this exact shape for session hits. "source" is
                # additive.
                "source": "session",
                "session_id": meta["session_id"],
                "display_name": sess.get("display_name") or "",
                "started_at": sess.get("started_at") or "",
                "client": sess.get("client") or "",
                "project": sess.get("project") or "",
                "start_s": meta["start_s"],
                "end_s": meta["end_s"],
                "text": meta["text"],
                "similarity": sim,
            })
        return results

    # ── Internals ───────────────────────────────────────────────────

    def _load_index(self) -> None:
        """Load every per-session embedding sidecar, plus every
        per-document doc_index sidecar, into one flat matrix.

        Legacy .pkl sidecars (pre-migration) are never loaded — they're
        logged and skipped so the gap is visible, and best-effort
        cleaned up so they stop being a standing hazard. A session or
        document only reachable via a legacy pickle is effectively
        unindexed until something re-runs index_session()/index_folder()
        to rebuild it from source.
        """
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                recordings_dir = self._session_service.recordings_dir
            except Exception as e:
                logger.warning(f"recordings_dir unavailable: {e}")
                self._matrix = None
                self._metadata = []
                self._loaded = True
                return

            all_vectors: List[np.ndarray] = []
            all_meta: List[dict] = []

            legacy_files = sorted(
                recordings_dir.glob("session_*.embeddings.pkl"))
            for legacy in legacy_files:
                logger.warning(
                    f"Ignoring legacy pickle sidecar {legacy.name} "
                    f"(never loaded — needs re-indexing from source)")
                try:
                    legacy.unlink()
                except OSError as e:
                    logger.warning(f"Could not remove {legacy}: {e}")

            files = sorted(recordings_dir.glob("session_*.embeddings.npz"))
            for f in files:
                # "session_<id>.embeddings.npz" -> "session_<id>.embeddings.json"
                # (with_suffix only touches the final suffix).
                json_path = f.with_suffix(".json")
                payload = load_payload(f, json_path)
                if payload is None:
                    logger.warning(f"Could not read {f.name} — skipping")
                    continue
                if payload.get("model_id") != _model_id():
                    # Stale embedding from a different model — silently
                    # ignored so search still works for the rest. The
                    # Settings backfill button can re-index these.
                    continue
                vecs = payload.get("embeddings")
                chunks = payload.get("chunks") or []
                if vecs is None or len(vecs) != len(chunks):
                    logger.warning(
                        f"{f.name} length mismatch (vecs={len(vecs) if vecs is not None else 'None'}, "
                        f"chunks={len(chunks)}) — skipping")
                    continue
                session_id = payload.get("session_id") or _session_id_from_filename(f)
                for i, ch in enumerate(chunks):
                    all_meta.append({
                        "source": "session",
                        "session_id": session_id,
                        "chunk_index": i,
                        "start_s": float(ch["start_s"]),
                        "end_s": float(ch["end_s"]),
                        "text": ch["text"],
                    })
                all_vectors.append(np.asarray(vecs, dtype=np.float32))

            doc_count = 0
            doc_dir = recordings_dir / "doc_index"
            if doc_dir.is_dir():
                legacy_doc_files = sorted(doc_dir.glob("doc_*.pkl"))
                for legacy in legacy_doc_files:
                    logger.warning(
                        f"Ignoring legacy pickle sidecar {legacy.name} "
                        f"(never loaded — needs re-indexing from source)")
                    try:
                        legacy.unlink()
                    except OSError as e:
                        logger.warning(f"Could not remove {legacy}: {e}")

                for f in sorted(doc_dir.glob("doc_*.npz")):
                    json_path = f.with_suffix(".json")
                    payload = load_payload(f, json_path)
                    if payload is None:
                        logger.warning(f"Could not read {f.name} — skipping")
                        continue
                    if payload.get("model_id") != _model_id():
                        continue
                    vecs = payload.get("embeddings")
                    chunks = payload.get("chunks") or []
                    if vecs is None or len(vecs) != len(chunks):
                        logger.warning(
                            f"{f.name} length mismatch (vecs="
                            f"{len(vecs) if vecs is not None else 'None'}, "
                            f"chunks={len(chunks)}) — skipping")
                        continue
                    doc_name = payload.get("doc_name") or f.name
                    doc_path = payload.get("doc_path") or ""
                    doc_client = payload.get("client") or ""
                    for i, ch in enumerate(chunks):
                        all_meta.append({
                            "source": "document",
                            "doc_name": doc_name,
                            "doc_path": doc_path,
                            "client": doc_client,
                            "chunk_index": i,
                            "text": ch["text"],
                        })
                    all_vectors.append(np.asarray(vecs, dtype=np.float32))
                    doc_count += 1

            if all_vectors:
                self._matrix = np.concatenate(all_vectors, axis=0)
            else:
                from core.embeddings import embedding_dim
                self._matrix = np.zeros((0, embedding_dim()), dtype=np.float32)
            self._metadata = all_meta
            self._loaded = True
            logger.info(
                f"Search index loaded: {len(all_meta)} chunks from "
                f"{len(files)} session(s) + {doc_count} document(s) "
                f"({self._matrix.nbytes // 1024} KB)")


def _model_id() -> str:
    """Defer the import so the module is importable even if
    sentence-transformers is missing."""
    try:
        from core.embeddings import MODEL_ID
        return MODEL_ID
    except Exception:
        return "unknown"


def _session_id_from_filename(path: Path) -> str:
    # session_<ID>.embeddings.npz  →  <ID>
    name = path.name
    if name.startswith("session_") and name.endswith(".embeddings.npz"):
        return name[len("session_"):-len(".embeddings.npz")]
    return ""
