"""
Tests for utils/embedding_store.py — the safe .npz/.json replacement
for the pickle sidecars search_service.py and document_service.py used
to persist embeddings.

Covers the invariants the pickle -> npz/json migration exists to
guarantee:
  - round-trip save/load preserves arrays and metadata exactly
  - np.load is always called with allow_pickle=False (the line that
    stops arbitrary code execution on load)
  - a corrupt/truncated .npz degrades to "unreadable", never raises
  - delete_payload removes every sidecar path handed to it, including
    a legacy .pkl passed alongside the current-format files
"""

import numpy as np

from utils import embedding_store
from utils.embedding_store import delete_payload, load_payload, save_payload


# ── round-trip ──────────────────────────────────────────────────────

def test_save_then_load_round_trips_arrays_and_metadata(tmp_path):
    npz_path = tmp_path / "thing.npz"
    json_path = tmp_path / "thing.json"

    embeddings = np.arange(12, dtype=np.float32).reshape(3, 4)
    meta = {
        "model_id": "sentence-transformers/all-MiniLM-L6-v2",
        "session_id": "SESS01",
        "chunks": [
            {"start_s": 0.0, "end_s": 1.5, "text": "hello"},
            {"start_s": 1.5, "end_s": 3.0, "text": "world"},
            {"start_s": 3.0, "end_s": 4.2, "text": "again"},
        ],
    }

    save_payload(npz_path, json_path, embeddings=embeddings, meta=meta)
    assert npz_path.exists()
    assert json_path.exists()

    loaded = load_payload(npz_path, json_path)
    assert loaded is not None
    np.testing.assert_array_equal(loaded["embeddings"], embeddings)
    assert loaded["embeddings"].dtype == np.float32
    assert loaded["model_id"] == meta["model_id"]
    assert loaded["session_id"] == meta["session_id"]
    assert loaded["chunks"] == meta["chunks"]


def test_save_then_load_round_trips_empty_matrix(tmp_path):
    npz_path = tmp_path / "empty.npz"
    json_path = tmp_path / "empty.json"
    embeddings = np.zeros((0, 8), dtype=np.float32)

    save_payload(npz_path, json_path, embeddings=embeddings,
                 meta={"model_id": "m", "chunks": []})
    loaded = load_payload(npz_path, json_path)
    assert loaded is not None
    assert loaded["embeddings"].shape == (0, 8)


# ── allow_pickle=False is the load-time invariant ──────────────────

def test_load_payload_calls_np_load_with_allow_pickle_false(tmp_path, monkeypatch):
    npz_path = tmp_path / "thing.npz"
    json_path = tmp_path / "thing.json"
    save_payload(npz_path, json_path,
                 embeddings=np.ones((1, 4), dtype=np.float32),
                 meta={"model_id": "m", "chunks": [{"text": "x"}]})

    calls = []
    real_load = np.load

    def spy_load(*args, **kwargs):
        calls.append(kwargs)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(embedding_store.np, "load", spy_load)

    loaded = load_payload(npz_path, json_path)
    assert loaded is not None
    assert len(calls) == 1
    assert calls[0].get("allow_pickle") is False


# ── corrupt / truncated files degrade gracefully ────────────────────

def test_load_payload_missing_files_returns_none(tmp_path):
    assert load_payload(tmp_path / "nope.npz", tmp_path / "nope.json") is None


def test_load_payload_truncated_npz_returns_none_not_raises(tmp_path):
    npz_path = tmp_path / "thing.npz"
    json_path = tmp_path / "thing.json"
    save_payload(npz_path, json_path,
                 embeddings=np.ones((2, 4), dtype=np.float32),
                 meta={"model_id": "m", "chunks": [{"text": "a"}, {"text": "b"}]})

    # Truncate the npz (a zip archive) to simulate a torn write / a
    # partially-synced cloud file.
    data = npz_path.read_bytes()
    npz_path.write_bytes(data[: len(data) // 2])

    loaded = load_payload(npz_path, json_path)
    assert loaded is None


def test_load_payload_corrupt_json_returns_none_not_raises(tmp_path):
    npz_path = tmp_path / "thing.npz"
    json_path = tmp_path / "thing.json"
    save_payload(npz_path, json_path,
                 embeddings=np.ones((1, 4), dtype=np.float32),
                 meta={"model_id": "m", "chunks": [{"text": "a"}]})

    json_path.write_text("{not valid json", encoding="utf-8")

    loaded = load_payload(npz_path, json_path)
    assert loaded is None


def test_load_payload_json_not_an_object_returns_none(tmp_path):
    npz_path = tmp_path / "thing.npz"
    json_path = tmp_path / "thing.json"
    save_payload(npz_path, json_path,
                 embeddings=np.ones((1, 4), dtype=np.float32),
                 meta={"model_id": "m", "chunks": [{"text": "a"}]})
    json_path.write_text("[1, 2, 3]", encoding="utf-8")

    assert load_payload(npz_path, json_path) is None


# ── deletion ─────────────────────────────────────────────────────────

def test_delete_payload_removes_current_and_legacy_files(tmp_path):
    npz_path = tmp_path / "thing.npz"
    json_path = tmp_path / "thing.json"
    legacy_pkl = tmp_path / "thing.pkl"
    save_payload(npz_path, json_path,
                 embeddings=np.ones((1, 4), dtype=np.float32),
                 meta={"model_id": "m", "chunks": [{"text": "a"}]})
    legacy_pkl.write_bytes(b"not really a pickle, just a stray legacy file")

    removed = delete_payload(npz_path, json_path, legacy_pkl)
    assert removed is True
    assert not npz_path.exists()
    assert not json_path.exists()
    assert not legacy_pkl.exists()


def test_delete_payload_none_entries_are_ignored(tmp_path):
    npz_path = tmp_path / "thing.npz"
    json_path = tmp_path / "thing.json"
    save_payload(npz_path, json_path,
                 embeddings=np.ones((1, 4), dtype=np.float32),
                 meta={"model_id": "m", "chunks": [{"text": "a"}]})

    # No legacy pkl on disk — pass None for it, as callers do when the
    # legacy path was never checked for existence up front.
    removed = delete_payload(npz_path, json_path, None)
    assert removed is True


def test_delete_payload_nothing_present_returns_false(tmp_path):
    removed = delete_payload(tmp_path / "a.npz", tmp_path / "a.json",
                              tmp_path / "a.pkl")
    assert removed is False
