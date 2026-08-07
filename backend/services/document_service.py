"""
Document ingestion for per-client Knowledge Folders.

A client can be pointed at a folder of existing documents (SOWs,
discovery notes, requirements docs). This module extracts plain text
from each supported file, chunks it, embeds the chunks with the SAME
local model the transcript search uses (see core.embeddings), and
persists one pickle per document so SearchService can fold them into
the same in-memory search matrix as session transcript chunks.

LMA gap analysis 2026-08-07: three separate field reports trace back to
silent skips — a file that couldn't be processed just vanished from the
count, which reads identically to "nothing in this folder matters."
Every skip here is therefore COUNTED and carries an actionable reason
(missing optional dependency, encrypted/corrupt file, empty
extraction) rather than raising or disappearing. extract_text() never
raises for a bad *input* file; it only raises for genuine programming
errors.

Pickle schema (one file per document, <recordings_dir>/doc_index/):
    {
        "model_id": str,             # see core.embeddings.MODEL_ID
        "doc_path": str,             # resolved absolute path
        "doc_name": str,             # file basename, for display/citation
        "client": str,               # the client this document belongs to
        "file_mtime": float,         # source file's mtime at index time
        "chunks": [{"text": str}, ...],
        "embeddings": np.ndarray,    # (N, dim) float32, L2-normalized
    }

This intentionally mirrors the session embeddings pickle
(services/search_service.py) field-for-field where the concepts
overlap (model_id, chunks, embeddings) so SearchService can merge both
into one flat matrix with minimal branching.
"""

from __future__ import annotations

import hashlib
import pickle
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np

from core.embeddings import MODEL_ID
from utils.logger import get_logger

logger = get_logger(__name__)

# Kept intentionally small and stdlib/lazy-import only. Anything add to
# this set needs a matching branch in extract_text() with a lazy import
# and a skip-reason path — never a hard dependency for the rest of the
# app to import this module.
SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}

# 350 words is a slightly tighter window than the 400-word transcript
# chunking in core.embeddings — document prose tends to be denser
# (no filler speech) so a smaller window keeps chunks topically
# coherent for retrieval.
TARGET_CHUNK_WORDS = 350
OVERLAP_WORDS = 50

DOC_INDEX_SUBDIR = "doc_index"


def _doc_index_dir(recordings_dir) -> Path:
    return Path(recordings_dir) / DOC_INDEX_SUBDIR


def _doc_pickle_path(recordings_dir, doc_path: Path) -> Path:
    """doc_<sha1(resolved path)>.pkl — stable across reindex runs (same
    file always hashes to the same pickle name), and collision-safe
    across clients/folders since the hash is over the full path."""
    digest = hashlib.sha1(str(doc_path.resolve()).encode("utf-8")).hexdigest()
    return _doc_index_dir(recordings_dir) / f"doc_{digest}.pkl"


def _iter_candidate_files(folder: Path):
    """Every real file under `folder`, recursively, skipping any path
    with a hidden (dot-prefixed) component — .git, .DS_Store
    companions, sync-client sentinel dirs, etc. are tooling noise, never
    a document a user meant to index, so they're excluded entirely
    rather than reported as a skip.

    Deliberately NOT filtered by SUPPORTED_EXTENSIONS here: a .pptx or
    .csv the user dropped in the folder should show up in the reindex
    report as a skip with an "unsupported file type" reason (from
    extract_text), not vanish without a trace — the same silent-skip
    failure mode the module docstring calls out.
    """
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(folder).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        yield path


# ── Text extraction ─────────────────────────────────────────────────

def extract_text(path: Path) -> Tuple[str, Optional[str]]:
    """Extract plain text from a supported document.

    Returns (text, skip_reason). Exactly one of the two is meaningful:
    on success `text` is non-empty and `skip_reason` is None; on any
    failure `text` is "" and `skip_reason` explains why in terms a user
    can act on ("pypdf not installed — pip install pypdf", "encrypted
    PDF (password protected)", "no extractable text (empty or
    image-only document)", ...).

    Never raises for a bad input file — every failure mode observed in
    the field (missing optional dependency, corrupt/encrypted file,
    unsupported extension, empty extraction) is a returned reason, not
    an exception, so index_folder() can keep going and still report the
    skip instead of losing the whole reindex run to one bad file.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            reason = None
        elif suffix == ".pdf":
            text, reason = _extract_pdf(path)
        elif suffix == ".docx":
            text, reason = _extract_docx(path)
        else:
            return "", f"unsupported file type: {suffix or '(no extension)'}"
    except OSError as e:
        return "", f"could not read file: {e}"

    if reason:
        return "", reason

    text = (text or "").strip()
    if not text:
        return "", "no extractable text (empty or image-only document)"
    return text, None


def _extract_pdf(path: Path) -> Tuple[str, Optional[str]]:
    try:
        import pypdf  # noqa: PLC0415  (deliberately lazy — see module docstring)
    except ImportError:
        return "", "pypdf not installed — pip install pypdf"

    try:
        reader = pypdf.PdfReader(str(path))
    except Exception as e:
        return "", f"corrupt or unreadable PDF: {e}"

    if reader.is_encrypted:
        try:
            # Some "encrypted" PDFs are only permission-restricted with
            # no real open password — an empty-password decrypt often
            # succeeds for those. A genuinely password-protected file
            # fails here and we report it rather than crash.
            if not reader.decrypt(""):
                return "", "encrypted PDF (password protected) — could not open"
        except Exception:
            return "", "encrypted PDF (password protected) — could not open"

    try:
        pages_text = [page.extract_text() or "" for page in reader.pages]
    except Exception as e:
        return "", f"corrupt or unreadable PDF: {e}"
    return "\n".join(pages_text), None


def _extract_docx(path: Path) -> Tuple[str, Optional[str]]:
    try:
        import docx  # noqa: PLC0415  (python-docx; deliberately lazy)
    except ImportError:
        return "", "python-docx not installed — pip install python-docx"

    try:
        document = docx.Document(str(path))
    except Exception as e:
        return "", f"corrupt or unreadable docx: {e}"

    paragraphs = [p.text for p in document.paragraphs]
    return "\n".join(paragraphs), None


# ── Chunking ─────────────────────────────────────────────────────────

def chunk_text(
    text: str,
    target_words: int = TARGET_CHUNK_WORDS,
    overlap_words: int = OVERLAP_WORDS,
) -> List[str]:
    """Split `text` into ~target_words chunks with overlap_words shared
    between consecutive chunks, so a topic straddling a chunk boundary
    still has a full match in at least one chunk.

    Pure function — no file or model access — so it's trivially
    unit-testable. Short text (<= target_words) comes back as a single
    chunk; empty/whitespace-only text comes back as an empty list.
    """
    words = text.split()
    if not words:
        return []
    if len(words) <= target_words:
        return [" ".join(words)]

    # Guarantee forward progress even if a caller passes
    # overlap_words >= target_words.
    step = max(1, target_words - overlap_words)
    chunks: List[str] = []
    start = 0
    n = len(words)
    while start < n:
        end = min(start + target_words, n)
        chunks.append(" ".join(words[start:end]))
        if end >= n:
            break
        start += step
    return chunks


# ── Indexing ─────────────────────────────────────────────────────────

def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize each row, defensively. embed_fn is caller-supplied
    (tests inject a fake) — normalizing here, not just trusting the
    caller, is what keeps the persisted pickle matching the session
    embeddings pickle's "L2-normalized float32" convention exactly, so
    SearchService can dot-product session and document vectors in the
    same matrix without a per-source special case."""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


def index_folder(
    folder,
    client: str,
    embed_fn: Callable[[List[str]], np.ndarray],
    recordings_dir,
) -> dict:
    """Extract, chunk, and embed every supported document under
    `folder`, persisting one pickle per document under
    <recordings_dir>/doc_index/.

    embed_fn is injected (callable: List[str] -> ndarray) so callers
    that can't/won't load sentence-transformers (all backend tests)
    can pass a fake embedder — this module never imports
    sentence-transformers itself.

    Re-embedding is skipped ("unchanged") when the existing pickle is
    newer than the source file's mtime — a reindex of an unchanged
    folder should be near-instant.

    Returns {"indexed": int, "unchanged": int,
             "skipped": [{"file": str, "reason": str}, ...],
             "total_chunks": int} — total_chunks counts every chunk
    currently on disk for this run (both freshly indexed and unchanged
    documents), so the report reflects the whole current index rather
    than only this run's work.
    """
    folder = Path(folder)
    doc_dir = _doc_index_dir(recordings_dir)
    report = {"indexed": 0, "unchanged": 0, "skipped": [], "total_chunks": 0}

    if not folder.is_dir():
        report["skipped"].append(
            {"file": str(folder), "reason": "folder does not exist"})
        return report

    doc_dir.mkdir(parents=True, exist_ok=True)

    for path in _iter_candidate_files(folder):
        pkl_path = _doc_pickle_path(recordings_dir, path)
        try:
            file_mtime = path.stat().st_mtime
        except OSError as e:
            report["skipped"].append(
                {"file": str(path), "reason": f"could not stat file: {e}"})
            continue

        if pkl_path.exists():
            try:
                pkl_is_current = pkl_path.stat().st_mtime >= file_mtime
            except OSError:
                pkl_is_current = False
            if pkl_is_current:
                report["unchanged"] += 1
                try:
                    existing = pickle.loads(pkl_path.read_bytes())
                    report["total_chunks"] += len(existing.get("chunks") or [])
                except Exception as e:
                    logger.warning(
                        f"Unchanged pickle {pkl_path.name} unreadable "
                        f"for chunk count ({e}); count omitted, file kept.")
                continue

        text, reason = extract_text(path)
        if reason:
            report["skipped"].append({"file": str(path), "reason": reason})
            continue

        chunks = chunk_text(text)
        if not chunks:
            report["skipped"].append(
                {"file": str(path),
                 "reason": "no chunks produced from extracted text"})
            continue

        try:
            embeddings = embed_fn(chunks)
        except Exception as e:
            report["skipped"].append(
                {"file": str(path), "reason": f"embedding failed: {e}"})
            continue

        embeddings = _normalize_rows(embeddings)
        if embeddings.shape[0] != len(chunks):
            report["skipped"].append(
                {"file": str(path),
                 "reason": (
                     f"embed_fn returned {embeddings.shape[0]} vectors "
                     f"for {len(chunks)} chunks")})
            continue

        payload = {
            "model_id": MODEL_ID,
            "doc_path": str(path.resolve()),
            "doc_name": path.name,
            "client": client,
            "file_mtime": file_mtime,
            "chunks": [{"text": t} for t in chunks],
            "embeddings": embeddings,
        }
        tmp_path = pkl_path.with_suffix(".pkl.tmp")
        tmp_path.write_bytes(pickle.dumps(payload, protocol=4))
        tmp_path.replace(pkl_path)

        report["indexed"] += 1
        report["total_chunks"] += len(chunks)
        logger.info(
            f"Indexed document {path.name}: {len(chunks)} chunks "
            f"({client}) -> {pkl_path.name}")

    return report


def remove_stale(folder, client: str, recordings_dir) -> int:
    """Delete doc_index pickles for `client` whose source file no
    longer exists on disk — the user deleted or moved a document out of
    the knowledge folder since the last reindex.

    `folder` isn't used to further scope the scan (client already
    disambiguates within recordings_dir) but is kept in the signature
    for symmetry with index_folder() and because a future version may
    want to distinguish "moved to a different knowledge folder" from
    "deleted".
    """
    del folder  # not needed for staleness (see docstring); kept for API symmetry
    doc_dir = _doc_index_dir(recordings_dir)
    if not doc_dir.is_dir():
        return 0

    removed = 0
    for pkl_path in doc_dir.glob("doc_*.pkl"):
        try:
            payload = pickle.loads(pkl_path.read_bytes())
        except Exception as e:
            logger.warning(f"Could not read {pkl_path.name}: {e}")
            continue
        if (payload.get("client") or "") != client:
            continue
        doc_path_str = payload.get("doc_path") or ""
        if doc_path_str and Path(doc_path_str).exists():
            continue
        try:
            pkl_path.unlink()
            removed += 1
            logger.info(
                f"Removed stale doc index {pkl_path.name} "
                f"(source gone: {doc_path_str})")
        except OSError as e:
            logger.warning(f"Could not delete {pkl_path}: {e}")
    return removed
