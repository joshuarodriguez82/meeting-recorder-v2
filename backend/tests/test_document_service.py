"""
Per-client Knowledge Folder document ingestion.

LMA gap analysis 2026-08-07: every extraction failure mode (missing
optional dependency, corrupt/encrypted file, empty extraction,
unsupported extension) must be a COUNTED skip with an actionable
reason, never a silent drop or an uncaught exception — three earlier
field reports trace back to exactly that failure shape elsewhere in
this codebase.

No optional deps (pypdf, python-docx, sentence-transformers) are
installed in this test environment by design — the pypdf/docx
"missing dependency" tests rely on that being true, and every embed
call uses a small deterministic fake instead of the real model.
"""

import hashlib
import json
import os
import pickle
import time

import numpy as np

from core.embeddings import MODEL_ID
from services.document_service import (
    chunk_text,
    extract_text,
    index_folder,
    remove_stale,
)
from services.search_service import SearchService
from services.session_service import SessionService


# ── fake embedder: deterministic, hash-seeded, L2-normalized ──────────

_FAKE_DIM = 8


def fake_embed(texts):
    vecs = np.zeros((len(texts), _FAKE_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        digest = hashlib.sha256(t.encode("utf-8")).digest()
        vals = np.frombuffer(digest[: _FAKE_DIM * 4], dtype=np.uint32).astype(np.float32)
        norm = np.linalg.norm(vals)
        vecs[i] = vals / norm if norm else vals
    return vecs


# ── chunk_text ──────────────────────────────────────────────────────

def test_chunk_text_empty_text_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\t  ") == []


def test_chunk_text_short_text_is_a_single_chunk():
    text = "word " * 10
    chunks = chunk_text(text, target_words=350, overlap_words=50)
    assert len(chunks) == 1
    assert chunks[0].split() == text.split()


def test_chunk_text_sizes_and_overlap():
    words = [f"w{i}" for i in range(1000)]
    text = " ".join(words)
    chunks = chunk_text(text, target_words=100, overlap_words=20)
    assert len(chunks) > 1
    for c in chunks[:-1]:
        assert len(c.split()) == 100
    assert len(chunks[-1].split()) <= 100
    # Consecutive chunks share exactly overlap_words words at the seam.
    for i in range(len(chunks) - 1):
        tail = chunks[i].split()[-20:]
        head = chunks[i + 1].split()[:20]
        assert tail == head


def test_chunk_text_progresses_even_when_overlap_exceeds_target():
    # Pathological input must not infinite-loop.
    words = [f"w{i}" for i in range(50)]
    chunks = chunk_text(" ".join(words), target_words=10, overlap_words=999)
    assert len(chunks) >= 1
    assert sum(len(c.split()) for c in chunks) >= 50


# ── extract_text ────────────────────────────────────────────────────

def test_extract_text_txt_and_md(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello world", encoding="utf-8")
    text, reason = extract_text(txt)
    assert reason is None
    assert text == "hello world"

    md = tmp_path / "sow.md"
    md.write_text("# Title\n\nBody text here", encoding="utf-8")
    text2, reason2 = extract_text(md)
    assert reason2 is None
    assert "Body text here" in text2


def test_extract_text_unsupported_extension_is_skipped_with_reason(tmp_path):
    p = tmp_path / "notes.csv"
    p.write_text("a,b,c", encoding="utf-8")
    text, reason = extract_text(p)
    assert text == ""
    assert reason is not None
    assert "unsupported" in reason.lower()


def test_extract_text_empty_file_is_skipped_with_reason(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n\t  ", encoding="utf-8")
    text, reason = extract_text(p)
    assert text == ""
    assert "no extractable text" in reason


def test_extract_text_pdf_missing_dependency_names_pypdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 not a real pdf")
    text, reason = extract_text(p)
    assert text == ""
    assert reason is not None
    assert "pypdf" in reason.lower()
    assert "pip install" in reason.lower()


def test_extract_text_docx_missing_dependency_names_python_docx(tmp_path):
    p = tmp_path / "doc.docx"
    p.write_bytes(b"PK\x03\x04 not a real docx")
    text, reason = extract_text(p)
    assert text == ""
    assert reason is not None
    assert "python-docx" in reason.lower()
    assert "pip install" in reason.lower()


# ── index_folder / remove_stale ────────────────────────────────────

def test_index_folder_report_counts_and_skip_reasons(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "sow.txt").write_text("word " * 500, encoding="utf-8")
    (folder / "notes.md").write_text("meeting notes " * 5, encoding="utf-8")
    (folder / "ignore.csv").write_text("a,b", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    report = index_folder(folder, "Zorg", fake_embed, recordings)
    assert report["indexed"] == 2
    assert report["unchanged"] == 0
    assert len(report["skipped"]) == 1
    assert report["skipped"][0]["file"].endswith("ignore.csv")
    assert "unsupported" in report["skipped"][0]["reason"].lower()
    assert report["total_chunks"] > 0

    doc_dir = recordings / "doc_index"
    assert len(list(doc_dir.glob("doc_*.pkl"))) == 2


def test_index_folder_skips_hidden_dirs(tmp_path):
    folder = tmp_path / "docs"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "config.txt").write_text("secret", encoding="utf-8")
    (folder / "visible.txt").write_text("hello world content here", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    report = index_folder(folder, "Zorg", fake_embed, recordings)
    assert report["indexed"] == 1
    assert report["skipped"] == []


def test_index_folder_nonexistent_folder_is_reported_not_raised(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    report = index_folder(tmp_path / "does-not-exist", "Zorg", fake_embed, recordings)
    assert report["indexed"] == 0
    assert len(report["skipped"]) == 1
    assert "does not exist" in report["skipped"][0]["reason"]


def test_index_folder_unchanged_on_second_run(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("some content to embed", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    r1 = index_folder(folder, "Zorg", fake_embed, recordings)
    assert r1["indexed"] == 1
    assert r1["unchanged"] == 0

    r2 = index_folder(folder, "Zorg", fake_embed, recordings)
    assert r2["indexed"] == 0
    assert r2["unchanged"] == 1


def test_index_folder_reembeds_after_file_modified(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    p = folder / "a.txt"
    p.write_text("version one", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    r1 = index_folder(folder, "Zorg", fake_embed, recordings)
    assert r1["indexed"] == 1

    # Deterministic "modified later" via explicit mtime rather than a
    # real sleep — avoids flakiness from filesystem mtime resolution.
    future = time.time() + 10
    p.write_text("version two has different, longer content", encoding="utf-8")
    os.utime(p, (future, future))

    r2 = index_folder(folder, "Zorg", fake_embed, recordings)
    assert r2["indexed"] == 1
    assert r2["unchanged"] == 0


def test_remove_stale_removes_orphan_pickle(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    p = folder / "a.txt"
    p.write_text("content to be removed later", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    index_folder(folder, "Zorg", fake_embed, recordings)
    doc_dir = recordings / "doc_index"
    assert len(list(doc_dir.glob("doc_*.pkl"))) == 1

    p.unlink()
    removed = remove_stale(folder, "Zorg", recordings)
    assert removed == 1
    assert len(list(doc_dir.glob("doc_*.pkl"))) == 0


def test_remove_stale_leaves_other_clients_alone(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    p = folder / "a.txt"
    p.write_text("content for client A", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    index_folder(folder, "ClientA", fake_embed, recordings)
    p.unlink()
    removed = remove_stale(folder, "ClientB", recordings)
    assert removed == 0
    doc_dir = recordings / "doc_index"
    assert len(list(doc_dir.glob("doc_*.pkl"))) == 1


# ── SearchService merge ─────────────────────────────────────────────

def _write_session_json(recordings, sid, client, display_name):
    (recordings / f"session_{sid}.json").write_text(json.dumps({
        "session_id": sid,
        "display_name": display_name,
        "started_at": "2026-01-01T00:00:00",
        "client": client,
        "project": "",
    }), encoding="utf-8")


def test_search_merges_session_and_document_hits(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()

    sid = "SESS01"
    _write_session_json(recordings, sid, "Zorg", "Kickoff Call")

    query_vec = np.zeros(_FAKE_DIM, dtype=np.float32)
    query_vec[0] = 1.0

    sess_payload = {
        "model_id": MODEL_ID,
        "session_id": sid,
        "chunks": [{"start_s": 0.0, "end_s": 5.0, "text": "we discussed pricing on the call"}],
        "embeddings": query_vec.reshape(1, _FAKE_DIM).copy(),
    }
    (recordings / f"session_{sid}.embeddings.pkl").write_bytes(
        pickle.dumps(sess_payload, protocol=4))

    doc_dir = recordings / "doc_index"
    doc_dir.mkdir()
    doc_payload = {
        "model_id": MODEL_ID,
        "doc_path": str(tmp_path / "Zorg-SOW.docx"),
        "doc_name": "Zorg-SOW.docx",
        "client": "Zorg",
        "file_mtime": 0.0,
        "chunks": [{"text": "pricing is $50k per phase per the SOW"}],
        "embeddings": query_vec.reshape(1, _FAKE_DIM).copy(),
    }
    (doc_dir / "doc_abc123.pkl").write_bytes(pickle.dumps(doc_payload, protocol=4))

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)

    # No sentence-transformers in this test env by design — stub the
    # two functions search() touches instead of loading the real model.
    monkeypatch.setattr("core.embeddings.is_available", lambda: True)
    monkeypatch.setattr("core.embeddings.embed_query", lambda q: query_vec.copy())

    results = search.search("pricing", top_k=10)
    sources = {r["source"] for r in results}
    assert sources == {"session", "document"}

    doc_hits = [r for r in results if r["source"] == "document"]
    assert len(doc_hits) == 1
    assert doc_hits[0]["doc_name"] == "Zorg-SOW.docx"
    assert doc_hits[0]["client"] == "Zorg"
    assert "text" in doc_hits[0] and "similarity" in doc_hits[0]

    sess_hits = [r for r in results if r["source"] == "session"]
    assert len(sess_hits) == 1
    # Session hit schema is exactly what it was pre-knowledge-folders,
    # plus the additive "source" key — the frontend depends on this.
    for key in ("session_id", "display_name", "started_at", "client",
                "project", "start_s", "end_s", "text", "similarity"):
        assert key in sess_hits[0]
    assert sess_hits[0]["session_id"] == sid

    # Client filter narrows document hits too, not just session hits.
    results_match = search.search("pricing", top_k=10, client="Zorg")
    assert {r["source"] for r in results_match} == {"session", "document"}

    results_nomatch = search.search("pricing", top_k=10, client="SomeoneElse")
    assert results_nomatch == []

    # A project filter excludes documents outright — they have no
    # project concept to match against.
    results_project = search.search("pricing", top_k=10, project="whatever")
    assert all(r["source"] != "document" for r in results_project)
