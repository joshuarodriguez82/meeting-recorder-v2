"""
Safe, structured on-disk storage for embedding sidecars.

Used by services/search_service.py (session transcript embeddings) and
services/document_service.py (per-document Knowledge Folder
embeddings) to persist their vector indexes.

## Why this replaced pickle

These sidecars used to be a single `pickle.dumps()`'d file per session
/ document. `pickle.loads()` on a file is arbitrary code execution if
that file is attacker-controlled — and here it genuinely can be: the
recordings directory (and its doc_index subfolder) is synced across
machines via Google Drive / OneDrive, with an archive folder explicitly
configured for cross-machine replication. A compromised cloud account,
a bad share, or a sync collision puts a crafted file on local disk with
*no local access required*. That is a real remote-code-execution path,
not a theoretical one — hence the migration to this module.

## Format

One `<name>.npz` holding the float32 embeddings matrix under the key
"embeddings" (`np.load(..., allow_pickle=False)` — passed explicitly,
never relying on the numpy default, which has changed across versions
and is exactly how this class of bug creeps back in), plus a sibling
`<name>.json` holding every other field (model_id, session_id/doc_path,
chunks, ...) as plain JSON. Neither format can execute code on load.

## Legacy pickles

Legacy `<name>.pkl` files from before this migration are NEVER read —
not even to migrate their contents, not even once. Callers that find
one (and no matching .npz/.json) treat it exactly like "not indexed
yet" and rebuild the index from the original source (the transcript or
source document). Rebuilding is safe by construction; unpickling to
migrate would preserve the exact vulnerability being removed, applied
to precisely the files most likely to have been tampered with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)


def save_payload(npz_path: Path, json_path: Path, *, embeddings: np.ndarray, meta: dict) -> None:
    """Atomically persist `embeddings` + `meta` as a matched .npz/.json
    pair.

    Each file is written tmp-then-rename independently (matches the
    previous pickle sidecar's atomic-write convention). The .npz is
    written and renamed into place first, so a process that dies
    mid-write never leaves a .json claiming a payload for which no
    array exists.
    """
    npz_path = Path(npz_path)
    json_path = Path(json_path)

    tmp_npz = npz_path.parent / (npz_path.name + ".tmp")
    # Passing an open file object (not a str/Path) means numpy won't
    # try to append a ".npz" extension of its own — it writes exactly
    # the bytes we ask for, to exactly the path we ask for.
    with open(tmp_npz, "wb") as fh:
        np.savez(fh, embeddings=np.asarray(embeddings, dtype=np.float32))
    tmp_npz.replace(npz_path)

    tmp_json = json_path.parent / (json_path.name + ".tmp")
    tmp_json.write_text(json.dumps(meta), encoding="utf-8")
    tmp_json.replace(json_path)


def load_payload(npz_path: Path, json_path: Path) -> Optional[dict]:
    """Load a matched .npz/.json pair back into one dict shaped like the
    legacy pickle payload (every JSON meta field plus "embeddings" as
    an np.ndarray).

    Returns None — never raises — if either file is missing, unreadable,
    or corrupt, so every caller treats "not indexed yet" and "index
    corrupt/truncated" the same way: skip it and let the index rebuild
    from source rather than crash on a bad cache file.

    `allow_pickle=False` is passed explicitly to `np.load`. This is the
    line that keeps a crafted .npz from being able to execute code on
    load — never omit it and never rely on the numpy default.
    """
    npz_path = Path(npz_path)
    json_path = Path(json_path)
    if not npz_path.exists() or not json_path.exists():
        return None
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read {json_path.name}: {e}")
        return None
    if not isinstance(meta, dict):
        logger.warning(f"{json_path.name} did not contain a JSON object — skipping")
        return None
    try:
        with np.load(npz_path, allow_pickle=False) as data:
            embeddings = np.array(data["embeddings"], dtype=np.float32)
    except Exception as e:
        # Covers a corrupt/truncated zip, a missing "embeddings" key, a
        # dtype numpy refuses without allow_pickle, etc. — all treated
        # as "this cache entry is unusable", never propagated.
        logger.warning(f"Could not read {npz_path.name}: {e}")
        return None

    meta["embeddings"] = embeddings
    return meta


def delete_payload(*paths: Optional[Path]) -> bool:
    """Best-effort delete of any number of sidecar paths (the .npz, the
    .json, and — when passed — a legacy .pkl left over from before this
    migration). Returns True if anything was actually removed.

    None entries are ignored so callers can pass an optional legacy
    path unconditionally, e.g. delete_payload(npz, json, legacy_pkl).
    """
    removed = False
    for p in paths:
        if p is None:
            continue
        p = Path(p)
        if p.exists():
            try:
                p.unlink()
                removed = True
            except OSError as e:
                logger.warning(f"Could not delete {p}: {e}")
    return removed
