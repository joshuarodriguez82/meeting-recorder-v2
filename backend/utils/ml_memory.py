"""Cross-cutting helper to release PyTorch's caching-allocator memory
after a batch of ML model work finishes.

Why this lives in utils/, not core/audio_utils.py: multiple core/*.py
modules use torch independently (diarization.py, speaker_embeddings.py,
embeddings.py), each behind its own lazy import — a shared, import-safe
helper avoids duplicating the "is torch even here" guard in every one
of them. audio_utils.py was suggested by an earlier audit, but it's WAV
/ resampling code with no ML model involvement at all; landing a torch
helper there would just be the next file to grep past, not a fix.

Across a single working day the user runs many ~100-minute sessions
back-to-back. Without an explicit release, PyTorch's CUDA/MPS caching
allocator keeps every scratch buffer it has ever allocated resident,
so process memory climbs steadily until the app is restarted. This
does NOT touch model weights — the singleton model caches in
core/diarization.py, core/speaker_embeddings.py and core/embeddings.py
are meant to stay resident between calls; this only releases the
allocator's reusable scratch-buffer pool.
"""

from __future__ import annotations

import gc

try:
    import torch
except Exception:  # pragma: no cover - absence is the whole point of the guard
    # Import-safe when torch is absent (requirements-cpu.txt installs,
    # or any environment that never pulled in the ML extras): a missing
    # or broken torch install must make this module a silent no-op at
    # import time, never an ImportError that takes down whichever
    # backend/core module imports us.
    torch = None  # type: ignore[assignment]


def cleanup_ml_memory() -> None:
    """Best-effort release of ML scratch memory after a batch of model
    work. Safe to call whether or not torch is installed — with no
    torch, this degrades to a plain ``gc.collect()`` and never raises.

    Call this at SESSION-BOUNDARY / POST-BATCH points only: once after
    a whole file's transcription, once after one diarization pass, once
    after one batch of speaker centroids or chunk embeddings. Do NOT
    call this inside a per-chunk/per-segment hot loop (e.g. the live
    transcription path in core/live_transcriber.py, or the per-
    utterance embed in core/speaker_embeddings.embed_utterance) — a
    gc.collect() on every live window would add real, user-visible
    latency to a path where near-real-time captions are the whole
    point.
    """
    gc.collect()
    if torch is None:
        return
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            # ipc_collect() only matters for CUDA inter-process tensor
            # sharing — a cheap no-op otherwise, kept inside the CUDA
            # branch specifically so it's never reached on CPU/MPS.
            torch.cuda.ipc_collect()
        elif getattr(torch.backends, "mps", None) is not None \
                and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        # A driver hiccup or unexpected torch build quirk here must
        # never propagate out of a caller's `finally:` block — worst
        # case we just skip freeing this round.
        pass
