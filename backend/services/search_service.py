"""
Semantic search across all sessions.

Each session's transcript chunks + their embeddings live in a sibling
pickle file next to the session JSON:

  recordings/session_<id>.json                 (existing)
  recordings/session_<id>.embeddings.pkl       (new)

The pickle holds:
  {
    "model_id": str,                # see core.embeddings.MODEL_ID
    "session_id": str,
    "chunks": [{"start_s", "end_s", "text"}, ...],
    "embeddings": np.ndarray (N, dim) float32, L2-normalized
  }

SearchService keeps a flat in-memory matrix lazy-loaded from those files
on first query. Query is one numpy dot product over the whole matrix —
plenty fast for the 100s-of-thousands-of-chunks scale a single user
will ever produce on their laptop. invalidate() drops the cache so
the next query picks up newly-indexed sessions.

LMA gap analysis 2026-08-07: the same matrix now also loads per-client
document chunks from <recordings_dir>/doc_index/*.pkl (see
services/document_service.py) — one pickle per indexed document, same
model_id/embeddings conventions, so a document chunk and a transcript
chunk are directly comparable by cosine similarity in one dot product.
Each metadata row carries "source": "session" | "document" so results
and the client filter can tell them apart; session-hit result dicts
keep every existing field unchanged (the frontend depends on them).

When a user runs a session through /process, recording_service triggers
index_session() to embed + persist that session's chunks. Old sessions
processed before this feature shipped need a one-time backfill
(POST /search/index/backfill, or use the Settings UI button).
"""

from __future__ import annotations

import pickle
import threading
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)


def _embeddings_path(recordings_dir: Path, session_id: str) -> Path:
    return recordings_dir / f"session_{session_id}.embeddings.pkl"


class SearchService:
    """In-memory semantic search index built lazily from per-session
    pickle sidecar files."""

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
        session count so the UI can show a backfill progress hint."""
        try:
            recordings_dir = self._session_service.recordings_dir
        except Exception:
            return {"total_sessions": 0, "indexed_sessions": 0,
                    "model_id": _model_id(), "available": False}

        sessions = self._session_service.list_sessions()
        indexed = 0
        for s in sessions:
            if _embeddings_path(recordings_dir, s["session_id"]).exists():
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
        out_path = _embeddings_path(recordings_dir, session_id)
        payload = {
            "model_id": MODEL_ID,
            "session_id": session_id,
            "chunks": [c.to_dict() for c in chunks],
            "embeddings": embeddings,
        }
        # Atomic-ish write: tmp then rename. A torn pickle file is
        # noisy at load time (we'd skip it with a warning), but the
        # next index_session run rewrites it cleanly.
        tmp_path = out_path.with_suffix(".pkl.tmp")
        tmp_path.write_bytes(pickle.dumps(payload, protocol=4))
        tmp_path.replace(out_path)
        self.invalidate()
        logger.info(
            f"Indexed session {session_id}: {len(chunks)} chunks "
            f"× {embeddings.shape[1]}-dim → {out_path.name}")
        return True

    def delete_session_index(self, session_id: str) -> bool:
        """Remove a session's embedding file. Called by SessionService
        when a session is deleted."""
        try:
            recordings_dir = self._session_service.recordings_dir
        except Exception:
            return False
        path = _embeddings_path(recordings_dir, session_id)
        if path.exists():
            try:
                path.unlink()
                self.invalidate()
                return True
            except OSError as e:
                logger.warning(f"Could not delete {path}: {e}")
        return False

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
        """Load every per-session embedding pickle, plus every per-document
        doc_index pickle, into one flat matrix."""
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

            files = sorted(recordings_dir.glob("session_*.embeddings.pkl"))
            all_vectors: List[np.ndarray] = []
            all_meta: List[dict] = []
            for f in files:
                try:
                    payload = pickle.loads(f.read_bytes())
                except Exception as e:
                    logger.warning(f"Could not read {f.name}: {e}")
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
                for f in sorted(doc_dir.glob("doc_*.pkl")):
                    try:
                        payload = pickle.loads(f.read_bytes())
                    except Exception as e:
                        logger.warning(f"Could not read {f.name}: {e}")
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
    # session_<ID>.embeddings.pkl  →  <ID>
    name = path.name
    if name.startswith("session_") and name.endswith(".embeddings.pkl"):
        return name[len("session_"):-len(".embeddings.pkl")]
    return ""
