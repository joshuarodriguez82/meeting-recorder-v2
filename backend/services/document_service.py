"""
Document ingestion for per-client Knowledge Folders.

A client can be pointed at a folder of existing documents (SOWs,
discovery notes, requirements docs). This module extracts plain text
from each supported file, chunks it, embeds the chunks with the SAME
local model the transcript search uses (see core.embeddings), and
persists one .npz/.json sidecar pair per document so SearchService can
fold them into the same in-memory search matrix as session transcript
chunks.

LMA gap analysis 2026-08-07: three separate field reports trace back to
silent skips — a file that couldn't be processed just vanished from the
count, which reads identically to "nothing in this folder matters."
Every skip here is therefore COUNTED and carries an actionable reason
(missing optional dependency, encrypted/corrupt file, empty
extraction) rather than raising or disappearing. extract_text() never
raises for a bad *input* file; it only raises for genuine programming
errors.

Sidecar schema (one .npz + one .json per document, under
<recordings_dir>/doc_index/):
    {
        "model_id": str,             # see core.embeddings.MODEL_ID
        "doc_path": str,             # resolved absolute path
        "doc_name": str,             # file basename, for display/citation
        "client": str,               # the client this document belongs to
        "file_mtime": float,         # source file's mtime at index time
        "chunks": [{"text": str}, ...],
        "embeddings": np.ndarray,    # (N, dim) float32, L2-normalized
    }
(embeddings live in the .npz; everything else in the .json — see
utils/embedding_store.py.)

This intentionally mirrors the session embeddings sidecar
(services/search_service.py) field-for-field where the concepts
overlap (model_id, chunks, embeddings) so SearchService can merge both
into one flat matrix with minimal branching.

Security note: this used to be a single pickle file per document, and
pickle.loads() on an attacker-controlled file is arbitrary code
execution. doc_index/ lives inside the recordings directory, which is
synced across machines via Google Drive / OneDrive — so these files are
not purely local, and a compromised cloud account or bad sync could
have handed an attacker RCE with no local access at all. See
utils/embedding_store.py for the full rationale. Legacy `doc_*.pkl`
files from before this migration are never read, not even to migrate
them — index_folder() treats one as "not indexed" and rebuilds the
document's chunks and embeddings straight from the source file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np

from core.embeddings import MODEL_ID
from utils.embedding_store import delete_payload, save_payload
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


def _doc_digest(doc_path: Path) -> str:
    return hashlib.sha1(str(doc_path.resolve()).encode("utf-8")).hexdigest()


def _doc_npz_path(recordings_dir, doc_path: Path) -> Path:
    """doc_<sha1(resolved path)>.npz — stable across reindex runs (same
    file always hashes to the same name), and collision-safe across
    clients/folders since the hash is over the full path."""
    return _doc_index_dir(recordings_dir) / f"doc_{_doc_digest(doc_path)}.npz"


def _doc_json_path(recordings_dir, doc_path: Path) -> Path:
    return _doc_index_dir(recordings_dir) / f"doc_{_doc_digest(doc_path)}.json"


def _doc_legacy_pkl_path(recordings_dir, doc_path: Path) -> Path:
    """Pre-migration pickle sidecar for this document. NEVER loaded —
    see module docstring and utils/embedding_store.py. Only ever checked
    for existence or unlinked."""
    return _doc_index_dir(recordings_dir) / f"doc_{_doc_digest(doc_path)}.pkl"


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
    caller, is what keeps the persisted sidecar matching the session
    embeddings sidecar's "L2-normalized float32" convention exactly, so
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
    `folder`, persisting one .npz/.json sidecar pair per document under
    <recordings_dir>/doc_index/.

    embed_fn is injected (callable: List[str] -> ndarray) so callers
    that can't/won't load sentence-transformers (all backend tests)
    can pass a fake embedder — this module never imports
    sentence-transformers itself.

    Re-embedding is skipped ("unchanged") when the existing sidecar is
    newer than the source file's mtime — a reindex of an unchanged
    folder should be near-instant. A document reachable only through a
    legacy .pkl sidecar (pre-migration, never loaded — see module
    docstring) is NOT "unchanged": it has no readable .npz/.json pair,
    so it's rebuilt from the source file same as an unindexed document,
    and the stale .pkl is removed once the rebuild succeeds.

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
        npz_path = _doc_npz_path(recordings_dir, path)
        json_path = _doc_json_path(recordings_dir, path)
        legacy_pkl_path = _doc_legacy_pkl_path(recordings_dir, path)
        try:
            file_mtime = path.stat().st_mtime
        except OSError as e:
            report["skipped"].append(
                {"file": str(path), "reason": f"could not stat file: {e}"})
            continue

        if npz_path.exists() and json_path.exists():
            try:
                sidecar_mtime = min(
                    npz_path.stat().st_mtime, json_path.stat().st_mtime)
                sidecar_is_current = sidecar_mtime >= file_mtime
            except OSError:
                sidecar_is_current = False
            if sidecar_is_current:
                report["unchanged"] += 1
                try:
                    meta = json.loads(json_path.read_text(encoding="utf-8"))
                    report["total_chunks"] += len(meta.get("chunks") or [])
                except (OSError, ValueError) as e:
                    logger.warning(
                        f"Unchanged sidecar {json_path.name} unreadable "
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

        meta = {
            "model_id": MODEL_ID,
            "doc_path": str(path.resolve()),
            "doc_name": path.name,
            "client": client,
            "file_mtime": file_mtime,
            "chunks": [{"text": t} for t in chunks],
        }
        save_payload(npz_path, json_path, embeddings=embeddings, meta=meta)
        if legacy_pkl_path.exists():
            # Freshly rebuilt from source — the legacy pickle for this
            # document is now redundant and, left in place, is a
            # standing load hazard if any future code path regressed to
            # reading it. Clean it up.
            delete_payload(legacy_pkl_path)
            logger.info(f"Removed legacy pickle sidecar {legacy_pkl_path.name}")

        report["indexed"] += 1
        report["total_chunks"] += len(chunks)
        logger.info(
            f"Indexed document {path.name}: {len(chunks)} chunks "
            f"({client}) -> {npz_path.name}")

    return report


def remove_stale(folder, client: str, recordings_dir) -> int:
    """Delete doc_index sidecars for `client` whose source file no
    longer exists on disk — the user deleted or moved a document out of
    the knowledge folder since the last reindex.

    Also opportunistically removes EVERY leftover legacy `doc_*.pkl`
    sidecar in doc_index/, regardless of client: those files are never
    loaded (see module docstring) and there's no safe way to read which
    client or source path one belongs to without unpickling it. They're
    pure clutter/hazard at this point — deleting one just means the
    owning document gets rebuilt from source next time its client's
    folder is reindexed, which is always safe (indexes rebuild lazily).

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

    for legacy in doc_dir.glob("doc_*.pkl"):
        logger.warning(
            f"Ignoring legacy pickle sidecar {legacy.name} "
            f"(never loaded — removing; owning document will be "
            f"rebuilt from source on next reindex)")
        try:
            legacy.unlink()
        except OSError as e:
            logger.warning(f"Could not delete {legacy}: {e}")

    removed = 0
    for json_path in doc_dir.glob("doc_*.json"):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read {json_path.name}: {e}")
            continue
        if (payload.get("client") or "") != client:
            continue
        doc_path_str = payload.get("doc_path") or ""
        if doc_path_str and Path(doc_path_str).exists():
            continue
        npz_path = json_path.with_suffix(".npz")
        if delete_payload(npz_path, json_path):
            removed += 1
            logger.info(
                f"Removed stale doc index {json_path.stem} "
                f"(source gone: {doc_path_str})")
    return removed
