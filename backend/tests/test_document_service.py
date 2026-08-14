"""
Per-client Knowledge Folder document ingestion.

LMA gap analysis 2026-08-07: every extraction failure mode (missing
optional dependency, corrupt/encrypted file, empty extraction,
unsupported extension) must be a COUNTED skip with an actionable
reason, never a silent drop or an uncaught exception — three earlier
field reports trace back to exactly that failure shape elsewhere in
this codebase.

No optional deps (pypdf, python-docx, openpyxl, python-pptx,
sentence-transformers) are installed in this test environment by
design — the "missing dependency" tests below rely on that being
true, and every embed call uses a small deterministic fake instead of
the real model.
"""

import hashlib
import json
import os
import pickle
import time

import numpy as np
import pytest

from core.embeddings import MODEL_ID
from services.document_service import (
    SUPPORTED_EXTENSIONS,
    chunk_text,
    extract_text,
    index_folder,
    remove_stale,
)
from services.search_service import SearchService
from services.session_service import SessionService
from utils.embedding_store import save_payload


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
    # "pip install" is unactionable advice inside a packaged desktop
    # app with a bootstrapped venv — the user has no pip to run. The
    # message should point at reinstalling the app instead.
    assert "pip install" not in reason.lower()
    assert "reinstall" in reason.lower()


def test_extract_text_docx_missing_dependency_names_python_docx(tmp_path):
    p = tmp_path / "doc.docx"
    p.write_bytes(b"PK\x03\x04 not a real docx")
    text, reason = extract_text(p)
    assert text == ""
    assert reason is not None
    assert "python-docx" in reason.lower()
    assert "pip install" not in reason.lower()
    assert "reinstall" in reason.lower()


def test_extract_text_xlsx_missing_dependency_names_openpyxl(tmp_path):
    # openpyxl IS a hard dependency of the app (Excel export uses it),
    # but the extractor still lazily imports it and must degrade
    # cleanly if it's ever unavailable at runtime — exactly like pypdf
    # and python-docx. Not installed in this CI venv by design, so this
    # exercises the real ImportError path, no mocking needed.
    p = tmp_path / "estimate.xlsx"
    p.write_bytes(b"PK\x03\x04 not a real xlsx")
    text, reason = extract_text(p)
    assert text == ""
    assert reason is not None
    assert "openpyxl" in reason.lower()
    assert "pip install" not in reason.lower()
    assert "reinstall" in reason.lower()


def test_extract_text_pptx_missing_dependency_names_python_pptx(tmp_path):
    p = tmp_path / "deck.pptx"
    p.write_bytes(b"PK\x03\x04 not a real pptx")
    text, reason = extract_text(p)
    assert text == ""
    assert reason is not None
    assert "python-pptx" in reason.lower()
    assert "pip install" not in reason.lower()
    assert "reinstall" in reason.lower()


def test_extract_text_html_needs_no_optional_dependency(tmp_path):
    # Stdlib html.parser only — always available, unlike the other new
    # extractors.
    p = tmp_path / "architecture.html"
    p.write_text(
        "<html><head><style>body{color:red}</style>"
        "<script>alert('x')</script></head>"
        "<body><h1>Architecture</h1><p>Contact Center design.</p></body>"
        "</html>",
        encoding="utf-8",
    )
    text, reason = extract_text(p)
    assert reason is None
    assert "Architecture" in text
    assert "Contact Center design." in text
    # script/style contents must never leak into extracted text.
    assert "alert" not in text
    assert "color:red" not in text


# ── non-text formats: expected skip, not a defect ───────────────────

@pytest.mark.parametrize("filename", ["photo.jpg", "diagram.png", "chart.drawio"])
def test_extract_text_images_and_diagrams_are_non_text_not_unsupported(tmp_path, filename):
    p = tmp_path / filename
    p.write_bytes(b"\x00\x01binary junk")
    text, reason = extract_text(p)
    assert text == ""
    assert reason is not None
    # These must read as "expected, not indexed" rather than
    # "unsupported file type" — the latter reads like a defect the
    # user should go fix, which images/diagrams are not.
    assert "not a text document" in reason.lower()
    assert "unsupported" not in reason.lower()


def test_index_folder_flags_non_text_skips_as_expected(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello world", encoding="utf-8")
    (folder / "photo.jpg").write_bytes(b"\xff\xd8\xff not a real jpeg")
    (folder / "weird.xyz").write_bytes(b"nonsense")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    report = index_folder(folder, "Zorg", fake_embed, recordings)
    by_file = {s["file"]: s for s in report["skipped"]}
    jpg_skip = next(s for f, s in by_file.items() if f.endswith("photo.jpg"))
    xyz_skip = next(s for f, s in by_file.items() if f.endswith("weird.xyz"))

    assert jpg_skip["expected"] is True
    assert "not a text document" in jpg_skip["reason"].lower()
    # A genuinely unsupported/unknown extension is a real (if minor)
    # gap, not an expected non-text format.
    assert xyz_skip["expected"] is False


# ── character cap ────────────────────────────────────────────────────

def test_extract_text_enforces_character_cap(tmp_path):
    from services.document_service import MAX_EXTRACTED_CHARS
    p = tmp_path / "huge.txt"
    p.write_text("x" * (MAX_EXTRACTED_CHARS + 10_000), encoding="utf-8")
    text, reason = extract_text(p)
    assert reason is None
    assert len(text) == MAX_EXTRACTED_CHARS


# ── SUPPORTED_EXTENSIONS / dispatch agreement ────────────────────────

def test_supported_extensions_and_dispatch_agree(tmp_path):
    """The exact bug that triggered this whole rework: SUPPORTED_EXTENSIONS
    claimed .docx/.pdf were supported when the libraries weren't shipped,
    and claimed .xlsx was unsupported when openpyxl was already bundled
    and only the extractor was missing. Guard both directions so "we say
    we support it" and "we can actually attempt to read it" can never
    silently drift apart again."""
    from services.document_service import _EXTRACTORS, _PLAIN_TEXT_EXTENSIONS

    dispatch_extensions = _PLAIN_TEXT_EXTENSIONS | set(_EXTRACTORS)
    assert dispatch_extensions == SUPPORTED_EXTENSIONS

    for ext in SUPPORTED_EXTENSIONS:
        p = tmp_path / f"probe{ext}"
        p.write_bytes(b"probe content, not necessarily a valid file")
        text, reason = extract_text(p)
        # A supported extension must never fall through to the
        # "unsupported file type" branch — whatever else goes wrong
        # (missing optional library, corrupt content), the dispatch
        # table must have a branch for it.
        if reason:
            assert "unsupported file type" not in reason.lower(), (
                f"{ext} is in SUPPORTED_EXTENSIONS but extract_text() "
                f"dispatched it as unsupported: {reason!r}")


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
    assert len(list(doc_dir.glob("doc_*.npz"))) == 2


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


def test_remove_stale_removes_orphan_sidecar(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    p = folder / "a.txt"
    p.write_text("content to be removed later", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    index_folder(folder, "Zorg", fake_embed, recordings)
    doc_dir = recordings / "doc_index"
    assert len(list(doc_dir.glob("doc_*.npz"))) == 1

    p.unlink()
    removed = remove_stale(folder, "Zorg", recordings)
    assert removed == 1
    assert len(list(doc_dir.glob("doc_*.npz"))) == 0


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
    assert len(list(doc_dir.glob("doc_*.npz"))) == 1


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

    sess_meta = {
        "model_id": MODEL_ID,
        "session_id": sid,
        "chunks": [{"start_s": 0.0, "end_s": 5.0, "text": "we discussed pricing on the call"}],
    }
    save_payload(
        recordings / f"session_{sid}.embeddings.npz",
        recordings / f"session_{sid}.embeddings.json",
        embeddings=query_vec.reshape(1, _FAKE_DIM).copy(),
        meta=sess_meta,
    )

    doc_dir = recordings / "doc_index"
    doc_dir.mkdir()
    doc_meta = {
        "model_id": MODEL_ID,
        "doc_path": str(tmp_path / "Zorg-SOW.docx"),
        "doc_name": "Zorg-SOW.docx",
        "client": "Zorg",
        "file_mtime": 0.0,
        "chunks": [{"text": "pricing is $50k per phase per the SOW"}],
    }
    save_payload(
        doc_dir / "doc_abc123.npz",
        doc_dir / "doc_abc123.json",
        embeddings=query_vec.reshape(1, _FAKE_DIM).copy(),
        meta=doc_meta,
    )

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


# ── legacy .pkl handling: never loaded, rebuilt from source instead ──

@pytest.fixture
def _spy_pickle_loads(monkeypatch):
    """Assert nothing in document_service's indexing/staleness paths
    ever calls pickle.loads — the whole point of the npz/json
    migration is that these files stop being unpickled."""
    calls = []
    real_loads = pickle.loads

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(pickle, "loads", spy)
    return calls


def test_index_folder_ignores_legacy_pickle_and_rebuilds_from_source(
    tmp_path, _spy_pickle_loads,
):
    """A doc_index/*.pkl left over from before the npz/json migration
    must never be read — index_folder() should treat the document as
    unindexed and re-extract + re-embed it from the source file."""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("some content to embed", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    doc_dir = recordings / "doc_index"
    doc_dir.mkdir()

    # Plant a legacy pickle at the exact path the real doc would hash
    # to, containing a payload that WOULD look "current" if read.
    from services.document_service import _doc_legacy_pkl_path
    legacy_path = _doc_legacy_pkl_path(recordings, folder / "a.txt")
    legacy_payload = {
        "model_id": MODEL_ID,
        "doc_path": str((folder / "a.txt").resolve()),
        "doc_name": "a.txt",
        "client": "Zorg",
        "file_mtime": 0.0,
        "chunks": [{"text": "stale legacy chunk"}],
        "embeddings": np.ones((1, _FAKE_DIM), dtype=np.float32),
    }
    legacy_path.write_bytes(pickle.dumps(legacy_payload, protocol=4))

    report = index_folder(folder, "Zorg", fake_embed, recordings)

    assert len(_spy_pickle_loads) == 0, "legacy pickle must never be unpickled"
    # Rebuilt from source, not read from the legacy pickle — indexed,
    # not unchanged, and the stale legacy file is cleaned up.
    assert report["indexed"] == 1
    assert report["unchanged"] == 0
    assert not legacy_path.exists()
    assert len(list(doc_dir.glob("doc_*.npz"))) == 1
    assert len(list(doc_dir.glob("doc_*.json"))) == 1


def test_remove_stale_purges_every_legacy_pickle_without_loading_it(
    tmp_path, _spy_pickle_loads,
):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    doc_dir = recordings / "doc_index"
    doc_dir.mkdir()

    # Two orphaned legacy pickles for two different (nonexistent, from
    # remove_stale's point of view) clients — remove_stale can't know
    # which client either belongs to without unpickling, so it purges
    # all of them regardless of the `client` argument.
    (doc_dir / "doc_aaaa.pkl").write_bytes(pickle.dumps(
        {"client": "ClientA", "doc_path": "", "chunks": []}, protocol=4))
    (doc_dir / "doc_bbbb.pkl").write_bytes(pickle.dumps(
        {"client": "ClientB", "doc_path": "", "chunks": []}, protocol=4))

    removed = remove_stale(tmp_path / "docs", "ClientA", recordings)

    assert len(_spy_pickle_loads) == 0, "legacy pickles must never be unpickled"
    assert len(list(doc_dir.glob("doc_*.pkl"))) == 0
    # remove_stale's return value counts current-format (.json) removals
    # driven by staleness, not legacy-pickle cleanup — there were none
    # of the former here.
    assert removed == 0


def test_index_folder_unchanged_skip_never_touches_pickle(
    tmp_path, _spy_pickle_loads,
):
    """The 'unchanged, read existing chunk count' path reads the JSON
    sidecar only — pickle.loads must never be invoked from it."""
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("some content to embed", encoding="utf-8")

    recordings = tmp_path / "recordings"
    recordings.mkdir()

    r1 = index_folder(folder, "Zorg", fake_embed, recordings)
    assert r1["indexed"] == 1

    r2 = index_folder(folder, "Zorg", fake_embed, recordings)
    assert r2["unchanged"] == 1
    assert r2["total_chunks"] == r1["total_chunks"]
    assert len(_spy_pickle_loads) == 0
