"""
Tests for services/search_service.py's handling of the embeddings
sidecar format — specifically the pickle -> npz/json migration.

The core invariant under test: a legacy `.embeddings.pkl` (or a legacy
`doc_*.pkl`) sitting on disk must NEVER be unpickled, not even once,
not even to "just peek" at it. pickle.loads is spied on throughout so
any regression that reaches for it trips an assertion immediately.
"""

import json
import pickle

import numpy as np
import pytest

from core.embeddings import MODEL_ID
from services.search_service import SearchService
from services.session_service import SessionService
from utils.embedding_store import save_payload


_DIM = 8


def _write_session_json(recordings, sid, client="Aon", display_name="Call"):
    (recordings / f"session_{sid}.json").write_text(json.dumps({
        "session_id": sid,
        "display_name": display_name,
        "started_at": "2026-01-01T00:00:00",
        "client": client,
        "project": "",
    }), encoding="utf-8")


def _legacy_pickle_bytes(sid):
    """A byte-for-byte valid pickle of a payload that WOULD load fine
    if anything ever called pickle.loads on it — the point of these
    tests is proving nothing does."""
    payload = {
        "model_id": MODEL_ID,
        "session_id": sid,
        "chunks": [{"start_s": 0.0, "end_s": 1.0, "text": "legacy chunk"}],
        "embeddings": np.ones((1, _DIM), dtype=np.float32),
    }
    return pickle.dumps(payload, protocol=4)


@pytest.fixture(autouse=True)
def _spy_pickle_loads(monkeypatch):
    """Fail loudly the instant anything in the search path calls
    pickle.loads — this is the assertion that matters most in this
    file. Individual tests additionally check the spy's call count."""
    calls = []
    real_loads = pickle.loads

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(pickle, "loads", spy)
    return calls


# ── legacy .pkl sidecars are never loaded ───────────────────────────

def test_legacy_session_pickle_is_never_loaded_and_session_is_absent(
    tmp_path, monkeypatch, _spy_pickle_loads,
):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    sid = "LEGACY01"
    _write_session_json(recordings, sid)

    legacy = recordings / f"session_{sid}.embeddings.pkl"
    legacy.write_bytes(_legacy_pickle_bytes(sid))
    assert legacy.exists()

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)

    query_vec = np.zeros(_DIM, dtype=np.float32)
    query_vec[0] = 1.0
    monkeypatch.setattr("core.embeddings.is_available", lambda: True)
    monkeypatch.setattr("core.embeddings.embed_query", lambda q: query_vec.copy())

    results = search.search("anything", top_k=10)

    assert results == []  # legacy-only session contributes nothing
    assert len(_spy_pickle_loads) == 0, (
        "pickle.loads was called — a legacy sidecar must never be "
        "unpickled, not even once")


def test_legacy_session_pickle_is_removed_after_a_load_attempt(
    tmp_path, monkeypatch, _spy_pickle_loads,
):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    sid = "LEGACY02"
    _write_session_json(recordings, sid)
    legacy = recordings / f"session_{sid}.embeddings.pkl"
    legacy.write_bytes(_legacy_pickle_bytes(sid))

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)
    monkeypatch.setattr("core.embeddings.is_available", lambda: True)
    monkeypatch.setattr("core.embeddings.embed_query",
                         lambda q: np.zeros(_DIM, dtype=np.float32))

    search.search("anything", top_k=10)

    assert not legacy.exists(), (
        "legacy pickle should be cleaned up (best-effort) once "
        "encountered, so it stops being a standing load hazard")
    assert len(_spy_pickle_loads) == 0


def test_legacy_doc_pickle_is_never_loaded(tmp_path, monkeypatch, _spy_pickle_loads):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    doc_dir = recordings / "doc_index"
    doc_dir.mkdir()

    legacy_payload = {
        "model_id": MODEL_ID,
        "doc_path": "/tmp/whatever.docx",
        "doc_name": "whatever.docx",
        "client": "Aon",
        "file_mtime": 0.0,
        "chunks": [{"text": "legacy doc chunk"}],
        "embeddings": np.ones((1, _DIM), dtype=np.float32),
    }
    (doc_dir / "doc_deadbeef.pkl").write_bytes(
        pickle.dumps(legacy_payload, protocol=4))

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)
    monkeypatch.setattr("core.embeddings.is_available", lambda: True)
    monkeypatch.setattr("core.embeddings.embed_query",
                         lambda q: np.zeros(_DIM, dtype=np.float32))

    results = search.search("anything", top_k=10)

    assert results == []
    assert len(_spy_pickle_loads) == 0
    assert not (doc_dir / "doc_deadbeef.pkl").exists()


def test_index_status_does_not_count_legacy_pickle_as_indexed(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    sid = "LEGACY03"
    _write_session_json(recordings, sid)
    (recordings / f"session_{sid}.embeddings.pkl").write_bytes(
        _legacy_pickle_bytes(sid))

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)
    status = search.index_status()

    assert status["total_sessions"] == 1
    assert status["indexed_sessions"] == 0


# ── current-format .npz/.json sidecars round-trip through search ────

def test_current_format_session_sidecar_is_found_by_search(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    sid = "CUR01"
    _write_session_json(recordings, sid)

    query_vec = np.zeros(_DIM, dtype=np.float32)
    query_vec[0] = 1.0
    save_payload(
        recordings / f"session_{sid}.embeddings.npz",
        recordings / f"session_{sid}.embeddings.json",
        embeddings=query_vec.reshape(1, _DIM).copy(),
        meta={
            "model_id": MODEL_ID,
            "session_id": sid,
            "chunks": [{"start_s": 0.0, "end_s": 2.0, "text": "current format chunk"}],
        },
    )

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)
    monkeypatch.setattr("core.embeddings.is_available", lambda: True)
    monkeypatch.setattr("core.embeddings.embed_query", lambda q: query_vec.copy())

    results = search.search("current", top_k=10)
    assert len(results) == 1
    assert results[0]["session_id"] == sid


def test_corrupt_npz_is_skipped_not_raised(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    sid = "CORRUPT01"
    _write_session_json(recordings, sid)

    npz_path = recordings / f"session_{sid}.embeddings.npz"
    json_path = recordings / f"session_{sid}.embeddings.json"
    save_payload(
        npz_path, json_path,
        embeddings=np.ones((1, _DIM), dtype=np.float32),
        meta={"model_id": MODEL_ID, "session_id": sid,
              "chunks": [{"start_s": 0.0, "end_s": 1.0, "text": "x"}]},
    )
    # Truncate to simulate a torn write / partially-synced cloud file.
    data = npz_path.read_bytes()
    npz_path.write_bytes(data[: len(data) // 2])

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)
    monkeypatch.setattr("core.embeddings.is_available", lambda: True)
    monkeypatch.setattr("core.embeddings.embed_query",
                         lambda q: np.zeros(_DIM, dtype=np.float32))

    results = search.search("anything", top_k=10)  # must not raise
    assert results == []


# ── delete_session_index cleans up every sidecar extension ──────────

def test_delete_session_index_removes_current_and_legacy_files(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    sid = "DEL01"
    npz_path = recordings / f"session_{sid}.embeddings.npz"
    json_path = recordings / f"session_{sid}.embeddings.json"
    legacy_path = recordings / f"session_{sid}.embeddings.pkl"
    save_payload(npz_path, json_path,
                 embeddings=np.ones((1, _DIM), dtype=np.float32),
                 meta={"model_id": MODEL_ID, "session_id": sid, "chunks": []})
    legacy_path.write_bytes(b"stray legacy file")

    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)

    assert search.delete_session_index(sid) is True
    assert not npz_path.exists()
    assert not json_path.exists()
    assert not legacy_path.exists()


def test_delete_session_index_returns_false_when_nothing_to_delete(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    session_svc = SessionService(str(recordings))
    search = SearchService(session_svc)
    assert search.delete_session_index("NOTHING_HERE") is False
