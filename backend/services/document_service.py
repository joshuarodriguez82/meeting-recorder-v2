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

Follow-up field data (2026-08-14): a real client Knowledge Folder — a
user's actual working corpus of SOWs, estimates, and RFP responses —
hit 87 skipped files out of what should have been the bulk of the
folder. Two distinct bugs, not one: SUPPORTED_EXTENSIONS claimed .pdf
and .docx support that pypdf/python-docx never shipped (advice to
`pip install` them is also unactionable inside a packaged app with a
bootstrapped venv, so that's gone too), and .xlsx/.pptx/.html had no
extractor at all despite openpyxl already being bundled for a different
feature. See the dispatch table below (_EXTRACTORS) and
NON_TEXT_EXTENSIONS, which now separate "we can't read this yet" from
"this was never going to be readable" (images, diagrams, media,
archives) — the field reports specifically called out the latter
reading like a defect when it isn't one.

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
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from core.embeddings import MODEL_ID
from utils.embedding_store import delete_payload, save_payload
from utils.logger import get_logger

logger = get_logger(__name__)

# Field data (2026-08-14 LMA gap analysis): a real client Knowledge
# Folder — SOWs, estimates, RFP responses, architecture decks — hit 87
# skipped files, nearly the user's whole corpus. Root causes: pypdf and
# python-docx were declared supported but never shipped, and .xlsx/.pptx/
# .html had no extractor at all despite openpyxl already being a hard
# dependency for the Excel export feature. This set (and the dispatch
# table below it, _EXTRACTORS) is now the actual source of truth for
# what's readable — see the consistency test in
# tests/test_document_service.py that fails if they ever drift apart
# again.
#
# .txt/.md are handled inline (no library at all); every other
# extension here must have a matching entry in _EXTRACTORS with a lazy
# import and a skip-reason path — never a hard dependency for the rest
# of the app to import this module.
_PLAIN_TEXT_EXTENSIONS = {".txt", ".md"}

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
    return hashlib.sha1(str(doc_path.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()


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

    Deliberately NOT filtered by SUPPORTED_EXTENSIONS here: a .csv or
    .drawio the user dropped in the folder should show up in the
    reindex report as a skip with an "unsupported file type" (or "not a
    text document") reason (from extract_text), not vanish without a
    trace — the same silent-skip failure mode the module docstring
    calls out.
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

# Applied uniformly across every extractor, not just the newer ones —
# a single pathological source file (a hundred-sheet workbook, a
# thousand-page PDF) shouldn't be able to blow up chunk count/embedding
# time regardless of format. ~500k characters is roughly 85k words —
# generous for a real SOW/estimate/RFP response, a hard stop for
# anything absurd. Applied once, centrally, in extract_text() below
# rather than per-extractor so every format gets the same guarantee.
MAX_EXTRACTED_CHARS = 500_000

# Formats that are never going to hold extractable text — images and
# diagram/media/archive formats. Reported as "not a text document",
# never as a defect the user should go fix (see extract_text
# docstring). Values are the plural noun used in the skip message.
#
# .drawio is deliberately NOT here as an extraction target: modern
# draw.io files store each <diagram> as base64+deflate-compressed,
# URL-encoded XML (desktop-app default), while older/plain exports are
# uncompressed XML with mxCell value="..." labels — reliably telling
# the two apart and decoding the compressed variant needs a real test
# fixture from an actual draw.io export to get right, which we don't
# have here. That didn't "fall out cleanly," so .drawio is classified
# as a non-text diagram format below instead of guessing at a decoder.
NON_TEXT_EXTENSIONS: Dict[str, str] = {
    ".jpg": "images", ".jpeg": "images", ".png": "images", ".gif": "images",
    ".bmp": "images", ".tiff": "images", ".tif": "images", ".webp": "images",
    ".svg": "images", ".ico": "images", ".heic": "images",
    ".drawio": "diagrams", ".vsdx": "diagrams",
    ".mp3": "audio files", ".wav": "audio files", ".m4a": "audio files",
    ".mp4": "video files", ".mov": "video files",
    ".zip": "archives", ".rar": "archives", ".7z": "archives",
}


def extract_text(path: Path) -> Tuple[str, Optional[str]]:
    """Extract plain text from a supported document.

    Returns (text, skip_reason). Exactly one of the two is meaningful:
    on success `text` is non-empty and `skip_reason` is None; on any
    failure `text` is "" and `skip_reason` explains why in terms a user
    can act on:
      - a genuinely missing optional library ("pypdf isn't installed —
        this copy of the app is missing a component; reinstalling
        should fix it") — deliberately NOT "pip install X": inside a
        packaged desktop app with a bootstrapped venv the user has no
        pip to run, so that advice was actionable only for developers,
        misleading for everyone else.
      - a format that was never going to be text ("not a text document
        — images aren't indexed") — expected, not a problem, so it
        reads differently from a real failure.
      - an actual failure: corrupt/encrypted file, empty extraction,
        unsupported extension.

    Never raises for a bad input file — every failure mode observed in
    the field (missing optional dependency, corrupt/encrypted file,
    unsupported extension, empty extraction) is a returned reason, not
    an exception, so index_folder() can keep going and still report the
    skip instead of losing the whole reindex run to one bad file.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in _PLAIN_TEXT_EXTENSIONS:
            text = path.read_text(encoding="utf-8", errors="replace")
            reason = None
        elif suffix in _EXTRACTORS:
            text, reason = _EXTRACTORS[suffix](path)
        elif suffix in NON_TEXT_EXTENSIONS:
            kind = NON_TEXT_EXTENSIONS[suffix]
            return "", f"not a text document — {kind} aren't indexed"
        else:
            return "", f"unsupported file type: {suffix or '(no extension)'}"
    except OSError as e:
        return "", f"could not read file: {e}"

    if reason:
        return "", reason

    text = (text or "").strip()
    if not text:
        return "", "no extractable text (empty or image-only document)"
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS]
    return text, None


def _missing_dependency_reason(package: str) -> str:
    """Shared phrasing for every lazy-imported extractor library that
    turns out not to be installed. NOT "pip install X" — see
    extract_text docstring for why that's the wrong advice inside a
    packaged app."""
    return (
        f"{package} isn't installed — this copy of the app is missing "
        "a component; reinstalling should fix it"
    )


def _extract_pdf(path: Path) -> Tuple[str, Optional[str]]:
    try:
        import pypdf  # noqa: PLC0415  (deliberately lazy — see module docstring)
    except ImportError:
        return "", _missing_dependency_reason("pypdf")

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
        return "", _missing_dependency_reason("python-docx")

    try:
        document = docx.Document(str(path))
    except Exception as e:
        return "", f"corrupt or unreadable docx: {e}"

    paragraphs = [p.text for p in document.paragraphs]
    return "\n".join(paragraphs), None


def _extract_xlsx(path: Path) -> Tuple[str, Optional[str]]:
    """Sheet-by-sheet cell text, useful for semantic search: each
    sheet's name as a header, then its rows as pipe-joined cell text.
    Blank cells and blank rows are dropped so the extracted text isn't
    mostly whitespace for sparse workbooks.

    read_only + data_only load: read_only streams rows without loading
    charts/images into memory at all (they're simply never visited),
    and data_only returns each formula cell's last-calculated value
    instead of the formula string — "=SUM(B2:B40)" is useless for
    search, "142500" is not.
    """
    try:
        import openpyxl  # noqa: PLC0415  (deliberately lazy — see module docstring)
    except ImportError:
        return "", _missing_dependency_reason("openpyxl")

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        return "", f"corrupt or unreadable xlsx: {e}"

    parts: List[str] = []
    try:
        for ws in wb.worksheets:
            sheet_lines = [f"## {ws.title}"]
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    sheet_lines.append(" | ".join(cells))
            if len(sheet_lines) > 1:
                parts.append("\n".join(sheet_lines))
            # Bail out once well past the cap rather than fully reading
            # (and holding in memory) every sheet of a huge workbook
            # only to truncate it afterward — extract_text() re-applies
            # the exact cap on the joined result either way.
            if sum(len(p) for p in parts) > MAX_EXTRACTED_CHARS:
                break
    except Exception as e:
        return "", f"corrupt or unreadable xlsx: {e}"
    finally:
        wb.close()

    return "\n\n".join(parts), None


def _extract_pptx(path: Path) -> Tuple[str, Optional[str]]:
    """Per-slide shape text plus speaker notes. python-pptx pulls in
    Pillow and lxml transitively — a heavier addition than pypdf or
    python-docx (see requirements files for the measured install
    size) — so this stays behind the same lazy-import/skip-reason
    pattern as every other optional extractor: a missing library here
    degrades to a clear, counted skip, never an ImportError at module
    load time for the rest of the app.
    """
    try:
        from pptx import Presentation  # noqa: PLC0415  (python-pptx; deliberately lazy)
    except ImportError:
        return "", _missing_dependency_reason("python-pptx")

    try:
        prs = Presentation(str(path))
    except Exception as e:
        return "", f"corrupt or unreadable pptx: {e}"

    parts: List[str] = []
    try:
        for i, slide in enumerate(prs.slides, start=1):
            slide_lines = [f"## Slide {i}"]
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    t = (shape.text_frame.text or "").strip()
                    if t:
                        slide_lines.append(t)
                elif getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            slide_lines.append(" | ".join(cells))
            if getattr(slide, "has_notes_slide", False):
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
                if notes:
                    slide_lines.append(f"Speaker notes: {notes}")
            if len(slide_lines) > 1:
                parts.append("\n".join(slide_lines))
    except Exception as e:
        return "", f"corrupt or unreadable pptx: {e}"

    return "\n\n".join(parts), None


class _HTMLTextExtractor(HTMLParser):
    """Minimal tag-stripping text extractor — stdlib only, no new
    dependency. Drops <script>/<style> contents (never document
    prose); keeps everything else, one text run per line."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data.strip())

    def get_text(self) -> str:
        return "\n".join(self._parts)


def _extract_html(path: Path) -> Tuple[str, Optional[str]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception as e:
        return "", f"corrupt or unreadable html: {e}"
    return parser.get_text(), None


# Dispatch table: the single source of truth for "which extensions can
# actually be read" — SUPPORTED_EXTENSIONS below is derived from this
# plus the inline-handled plain-text extensions, so the two can never
# silently drift apart the way "unsupported file type: .xlsx" (despite
# openpyxl being bundled) drifted from the real capability of this
# module. See test_supported_extensions_and_dispatch_agree.
_EXTRACTORS: Dict[str, Callable[[Path], Tuple[str, Optional[str]]]] = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
    ".html": _extract_html,
    ".htm": _extract_html,
}

SUPPORTED_EXTENSIONS = _PLAIN_TEXT_EXTENSIONS | set(_EXTRACTORS)


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
             "skipped": [{"file": str, "reason": str, "expected": bool}, ...],
             "total_chunks": int} — total_chunks counts every chunk
    currently on disk for this run (both freshly indexed and unchanged
    documents), so the report reflects the whole current index rather
    than only this run's work.

    Each skip's "expected" flag distinguishes a format that was never
    going to be a text document (images, diagrams, media, archives —
    see NON_TEXT_EXTENSIONS) from a genuine failure (missing library,
    corrupt file, unsupported extension, empty extraction). The
    frontend uses this to avoid making the former read like a defect
    the user should go fix.
    """
    folder = Path(folder)
    doc_dir = _doc_index_dir(recordings_dir)
    report = {"indexed": 0, "unchanged": 0, "skipped": [], "total_chunks": 0}

    if not folder.is_dir():
        report["skipped"].append(
            {"file": str(folder), "reason": "folder does not exist",
             "expected": False})
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
                {"file": str(path), "reason": f"could not stat file: {e}",
                 "expected": False})
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
            report["skipped"].append({
                "file": str(path), "reason": reason,
                "expected": path.suffix.lower() in NON_TEXT_EXTENSIONS,
            })
            continue

        chunks = chunk_text(text)
        if not chunks:
            report["skipped"].append(
                {"file": str(path),
                 "reason": "no chunks produced from extracted text",
                 "expected": False})
            continue

        try:
            embeddings = embed_fn(chunks)
        except Exception as e:
            report["skipped"].append(
                {"file": str(path), "reason": f"embedding failed: {e}",
                 "expected": False})
            continue

        embeddings = _normalize_rows(embeddings)
        if embeddings.shape[0] != len(chunks):
            report["skipped"].append(
                {"file": str(path),
                 "reason": (
                     f"embed_fn returned {embeddings.shape[0]} vectors "
                     f"for {len(chunks)} chunks"),
                 "expected": False})
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
