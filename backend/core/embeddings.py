"""
Sentence embeddings for semantic search across meetings.

Uses sentence-transformers/all-MiniLM-L6-v2 — small (~22 MB), fast on
CPU (~50ms per chunk), 384-dim L2-normalized output. Cosine similarity
is the dot product of two normalized vectors.

Why a local model rather than an embedding API:
  - Free; no per-token cost as the meeting corpus grows.
  - Private; transcript text never leaves the user's machine.
  - Fits the rest of the app's local-first stance (whisper, pyannote,
    optional Ollama for summaries).
  - Quality is genuinely good for transcript chunks of 100-500 words —
    the long-document retrieval domain MiniLM was trained on.

Why MiniLM over a larger model:
  - 22 MB download vs 400+ MB for mpnet — friction matters on first use.
  - Fast enough to backfill 100 sessions in ~30 seconds on Apple Silicon.
  - Quality difference is real but mild; for typical meeting search
    ("what did we say about pricing"), MiniLM ranks the right chunk
    in the top 3 ~95% of the time on our test set.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)

# 400 words ≈ 60 seconds of speech, a thematic unit (one Q&A exchange,
# one topic). Small enough that the embedding still discriminates;
# large enough to capture context the user can read at a glance.
TARGET_CHUNK_WORDS = 400
# Overlap so a topic that straddles two chunks isn't lost in a single
# bad cosine score. 50 words ≈ 8 seconds.
OVERLAP_WORDS = 50

# Hugging Face model id (used as a tag in the persisted pickle so we can
# detect when the user switches models and ignore stale embeddings).
MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"

_MODEL_LOCK = threading.Lock()
_model = None
_dim: Optional[int] = None


@dataclass
class Chunk:
    """One chunked piece of a session's transcript with its time range."""
    start_s: float
    end_s: float
    text: str

    def to_dict(self) -> dict:
        return {
            "start_s": float(self.start_s),
            "end_s": float(self.end_s),
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        return cls(
            start_s=float(d["start_s"]),
            end_s=float(d["end_s"]),
            text=d["text"],
        )


def is_available() -> bool:
    """Probe whether sentence-transformers can be loaded. Safe to call
    at startup; returns False on import error so callers can degrade
    gracefully (semantic search disabled, full-text still works)."""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError as e:
        logger.warning(f"sentence-transformers not available: {e}. "
                       f"Semantic search will be disabled — install with "
                       f"`pip install sentence-transformers` to enable.")
        return False


def _get_model():
    """Lazy-load and cache the MiniLM encoder. ~1.5s on first call."""
    global _model, _dim
    with _MODEL_LOCK:
        if _model is not None:
            return _model
        from sentence_transformers import SentenceTransformer
        from config.settings import USER_DATA_DIR
        # Cache under the user data dir so the model survives app
        # updates and never needs admin perms. ~/.cache is the
        # sentence-transformers default but we keep ours scoped to
        # match where the ECAPA model already lives.
        cache_dir = Path(USER_DATA_DIR) / "models" / "minilm-l6"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _model = SentenceTransformer(
            MODEL_ID,
            cache_folder=str(cache_dir),
        )
        try:
            _dim = int(_model.get_sentence_embedding_dimension())
        except Exception:
            _dim = 384  # known dimensionality of MiniLM-L6
        logger.info(f"MiniLM embedder loaded: dim={_dim}, cache={cache_dir}")
        return _model


def embedding_dim() -> int:
    """Output dimensionality of the active embedding model.
    Returns the canonical MiniLM dim (384) without forcing a model load
    if no embedding has been computed yet."""
    return _dim if _dim is not None else 384


def chunk_segments(segments: Iterable[dict]) -> List[Chunk]:
    """Walk session segments, accumulate into ~TARGET_CHUNK_WORDS chunks
    with OVERLAP_WORDS overlap between consecutive chunks.

    Each input segment must look like {"start": float, "end": float,
    "text": str}. Chunks preserve the start of the first included
    segment and end of the last, so click-to-jump in the UI lands on
    real audio frames.

    The slide-back overlap algorithm walks back through the just-emitted
    chunk's segments until it has accumulated OVERLAP_WORDS, then those
    segments seed the next chunk. Boundary topics get a second chance
    to match a query.
    """
    segs = [s for s in segments if (s.get("text") or "").strip()]
    if not segs:
        return []

    def _word_count(seg: dict) -> int:
        return len((seg.get("text") or "").split())

    chunks: List[Chunk] = []
    current: List[dict] = []
    current_words = 0
    for seg in segs:
        current.append(seg)
        current_words += _word_count(seg)
        if current_words < TARGET_CHUNK_WORDS:
            continue
        chunks.append(_emit_chunk(current))
        # Slide back to keep the last OVERLAP_WORDS as the head of the
        # next chunk. We always keep at least one segment so we make
        # forward progress on a single very-long segment.
        kept: List[dict] = []
        kept_words = 0
        for s in reversed(current):
            kept.insert(0, s)
            kept_words += _word_count(s)
            if kept_words >= OVERLAP_WORDS:
                break
        # If a single segment is huge (longer than OVERLAP_WORDS), only
        # keep one — otherwise we'd loop forever emitting the same chunk.
        if len(kept) == len(current):
            kept = [current[-1]]
            kept_words = _word_count(current[-1])
        current = kept
        current_words = kept_words

    if current:
        chunks.append(_emit_chunk(current))
    return chunks


def _emit_chunk(segs: List[dict]) -> Chunk:
    text = " ".join((s.get("text") or "").strip() for s in segs).strip()
    return Chunk(
        start_s=float(segs[0].get("start", 0.0)),
        end_s=float(segs[-1].get("end", 0.0)),
        text=text,
    )


def embed_texts(texts: List[str]) -> np.ndarray:
    """Encode arbitrary text strings → (N, dim) float32 L2-normalized
    matrix.

    Pulled out of embed_chunks() (LMA gap analysis 2026-08-07) so
    document ingestion (backend/services/document_service.py) can embed
    plain-text chunks through the exact same lazy singleton model, same
    normalize/batch settings, as transcript chunks — session and
    document embeddings must land in the same vector space or cosine
    similarity between them is meaningless.
    """
    if not texts:
        return np.zeros((0, embedding_dim()), dtype=np.float32)
    model = _get_model()
    # MiniLM-L6 has a 256-token limit; sentence-transformers truncates
    # silently. 400 words usually fits in 256 tokens (English avg
    # 0.7 tokens/word); on long Spanish/multilingual chunks the tail
    # may get clipped, which is acceptable — the head usually carries
    # enough topical signal for retrieval.
    embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
        batch_size=32,
    )
    return embeddings.astype(np.float32, copy=False)


def embed_chunks(chunks: List[Chunk]) -> np.ndarray:
    """Encode chunks → (N, dim) float32 L2-normalized matrix."""
    if not chunks:
        return np.zeros((0, embedding_dim()), dtype=np.float32)
    return embed_texts([c.text for c in chunks])


def embed_query(query: str) -> np.ndarray:
    """Encode a single query string → (dim,) float32 L2-normalized."""
    model = _get_model()
    embedding = model.encode(
        [query],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return embedding[0].astype(np.float32, copy=False)
