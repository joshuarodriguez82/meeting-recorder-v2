"""
Per-speaker voice fingerprint extraction via SpeechBrain's ECAPA-TDNN.

ECAPA produces a 192-dim embedding vector that captures the speaker's
voice characteristics independent of what they're saying. Two centroids
from the same person will have cosine similarity ~0.7-0.95; centroids
from different people typically score 0.3-0.5. We use that delta to
fingerprint speakers across meetings: after the user labels SPEAKER_00
as "John" once, the next meeting can auto-tag a matching centroid as
John without the user having to rename them again.

Why ECAPA / SpeechBrain rather than the embedding component pyannote
3.x bundles internally:
  - speechbrain is already a hard dependency (pyannote pulls it in).
  - The model (`speechbrain/spkrec-ecapa-voxceleb`) is freely available
    on Hugging Face — no gated-repo agreement, no extra HF token scope
    on top of what the user has already accepted for pyannote.
  - It's small (~16 MB) and downloaded once on first use.
  - Its output is already designed for the cosine-similarity comparison
    we want; no extra calibration step needed.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from utils.logger import get_logger
from utils.ml_memory import cleanup_ml_memory

logger = get_logger(__name__)

# Min total audio per speaker before we trust the centroid. ECAPA's
# embedding is noisy on very short snippets; below ~1.5s of speech we
# skip the speaker entirely rather than risk a bad match.
MIN_TOTAL_SECONDS = 1.5

# Min individual turn length to include in the centroid. Sub-250ms turns
# are usually backchannels ("yeah", "mm-hmm") whose embeddings are
# unstable and would drag the centroid toward noise.
MIN_TURN_SECONDS = 0.25

# Singleton encoder. ECAPA-TDNN model load is ~3 seconds and ~16 MB of
# RAM, so we do it once per process and reuse.
_MODEL_LOCK = threading.Lock()
_classifier = None


def _get_classifier():
    """Lazy-load the SpeechBrain ECAPA encoder."""
    global _classifier
    with _MODEL_LOCK:
        if _classifier is not None:
            return _classifier
        # Imports deferred so the module is importable even if speechbrain
        # is missing — callers can probe via `is_available()` first.
        from speechbrain.inference.speaker import EncoderClassifier
        import torch

        # Device pick: CUDA when present, else CPU. Apple Silicon MPS is
        # supported by speechbrain 1.x in principle but has historically
        # had odd numerical drift on this specific model — stick with CPU
        # on Mac until we have a chance to verify embeddings match across
        # devices. ECAPA is fast enough on CPU (~50ms per 1s of audio).
        device = "cuda" if torch.cuda.is_available() else "cpu"

        # Cache dir under the user data location so the download survives
        # app updates and never needs admin perms. ~/.cache is the
        # speechbrain default but it litters home; keep ours scoped.
        from config.settings import USER_DATA_DIR
        cache_dir = Path(USER_DATA_DIR) / "models" / "ecapa-voxceleb"
        cache_dir.mkdir(parents=True, exist_ok=True)

        _classifier = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(cache_dir),
            run_opts={"device": device},
        )
        logger.info(f"ECAPA speaker encoder loaded on {device}")
        return _classifier


def is_available() -> bool:
    """Probe whether the encoder can be loaded. Safe to call before
    diarization has run; returns False on any import error so callers
    can degrade gracefully (skip fingerprinting, keep the rest)."""
    try:
        import speechbrain  # noqa: F401
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
        return True
    except ImportError as e:
        logger.warning(f"Speaker fingerprinting unavailable: {e}")
        return False


def extract_speaker_centroids(
    audio_path: str,
    turns_by_speaker: Dict[str, List[Tuple[float, float]]],
    min_total_seconds: float = MIN_TOTAL_SECONDS,
) -> Dict[str, np.ndarray]:
    """
    Compute per-speaker centroid embeddings.

    Args:
        audio_path: 16 kHz mono WAV file (the session's final audio).
        turns_by_speaker: {speaker_label: [(start_s, end_s), ...]}.
            Speaker labels come from diarize() and are session-local
            (e.g. "SPEAKER_00", "SPEAKER_01"). Turns can be in any order;
            we just sample audio from each window.
        min_total_seconds: skip speakers with less total speech than this.

    Returns:
        {speaker_label: 192-dim float32 numpy array, L2-normalized}.
        Speakers with insufficient audio are silently omitted so the
        caller can fall back to "no fingerprint" for them.

    Raises:
        Nothing — embedding failures get logged and that speaker is
        skipped. The diarization result is still useful even if every
        fingerprint fails.
    """
    if not turns_by_speaker:
        return {}

    try:
        import torch
        import torchaudio
        classifier = _get_classifier()
    except Exception as e:
        logger.warning(f"Could not load ECAPA encoder: {e} — "
                       f"skipping speaker fingerprinting")
        return {}

    # Read the HEADER, not the audio (2026-09-02). This used to be a
    # plain `torchaudio.load(audio_path)`, pulling the entire recording
    # into RAM: sessions are written as 32-bit float mono at the capture
    # rate, so a one-hour meeting is roughly 690 MB — and this runs
    # immediately after the diarization pass, while pyannote's own
    # allocations are still being released. Only the turn spans are
    # ever read below, and every one of them is a fraction of the file.
    try:
        info = torchaudio.info(audio_path)
        fs = info.sample_rate
        total_frames = info.num_frames
    except Exception as e:
        logger.warning(f"Could not read audio header for embedding: {e}")
        return {}

    def _read_span(start_s: float, end_s: float):
        """Just this turn's samples, as mono.

        Returns None when the span is empty or unreadable — one bad turn
        must not cost the whole centroid, which is the same rule the
        ECAPA call below already follows.
        """
        start_frame = max(0, int(start_s * fs))
        end_frame = min(total_frames, int(end_s * fs))
        if end_frame <= start_frame:
            return None
        try:
            chunk, _ = torchaudio.load(
                audio_path, frame_offset=start_frame,
                num_frames=end_frame - start_frame)
        except Exception as e:
            logger.debug(f"Could not read {start_s}-{end_s}s: {e}")
            return None
        if chunk.shape[0] > 1:
            chunk = chunk.mean(dim=0, keepdim=True)
        return chunk

    centroids: Dict[str, np.ndarray] = {}
    try:
        for speaker, turns in turns_by_speaker.items():
            valid_turns = [(s, e) for (s, e) in turns
                           if (e - s) >= MIN_TURN_SECONDS]
            total = sum((e - s) for s, e in valid_turns)
            if total < min_total_seconds:
                logger.info(
                    f"Speaker {speaker}: only {total:.1f}s usable speech — "
                    f"skipping fingerprint (need >={min_total_seconds}s)")
                continue

            embeddings = []
            for start_s, end_s in valid_turns:
                segment = _read_span(start_s, end_s)
                if segment is None:
                    continue
                try:
                    with torch.no_grad():
                        emb = classifier.encode_batch(segment).squeeze().cpu().numpy()
                    embeddings.append(emb)
                except Exception as e:
                    # ECAPA can choke on very specific waveform edge cases
                    # (all-zeros, NaNs from a corrupt frame). Skip the turn,
                    # keep going — one bad turn shouldn't lose the centroid.
                    logger.debug(f"ECAPA failed on turn {start_s}-{end_s}: {e}")
                    continue

            if not embeddings:
                continue

            # Centroid = unweighted mean of per-turn embeddings, L2-normalized
            # so cosine == dot product downstream.
            centroid = np.mean(np.stack(embeddings), axis=0)
            norm = float(np.linalg.norm(centroid))
            if norm < 1e-8:
                continue  # degenerate — would produce NaN comparisons
            centroid = (centroid / norm).astype(np.float32)
            centroids[speaker] = centroid
    finally:
        # SESSION-BOUNDARY cleanup: this runs once per whole recording's
        # worth of speaker turns (called from recording_service.
        # _fingerprint_speakers after diarization), never per-utterance
        # — embed_utterance() below is the hot live-tracking sibling and
        # deliberately does NOT call this.
        cleanup_ml_memory()

    logger.info(
        f"Computed {len(centroids)}/{len(turns_by_speaker)} speaker "
        f"centroids from {audio_path}")
    return centroids


def embed_utterance(
    pcm: np.ndarray, samplerate: int = 16000,
) -> Optional[np.ndarray]:
    """Embed a single short in-memory utterance directly — no WAV file
    on disk required.

    Field report 2026-08-10 (Zoom notetaker parity): the live speaker
    tracker (core/live_speakers.py) needs to fingerprint "them"
    utterances within a second or two of them finishing, to split the
    live preview's single "Them" bucket into distinct Speaker N labels.
    extract_speaker_centroids() above is the batch/file-based sibling
    the canonical post-stop pipeline uses (it reads turns out of a WAV
    already on disk); this is the same underlying ECAPA call reshaped
    for a raw numpy array clipped straight out of the live audio buffer,
    with no intermediate file.

    `samplerate` is accepted for API clarity but not resampled against —
    callers must already be at whatever rate the ECAPA model expects
    (16 kHz; this matches TARGET_SR throughout the live-transcription
    path, so no caller needs to resample before calling this).

    Returns None (never raises) on any failure — the caller degrades to
    "no fingerprint for this utterance" rather than losing the live
    transcript over a transient encode error.
    """
    if pcm is None or len(pcm) == 0:
        return None
    try:
        import torch
        classifier = _get_classifier()
    except Exception as e:
        logger.debug(f"Could not load ECAPA encoder for live embed: {e}")
        return None
    try:
        tensor = torch.from_numpy(
            np.ascontiguousarray(pcm, dtype=np.float32)
        ).unsqueeze(0)
        with torch.no_grad():
            emb = classifier.encode_batch(tensor).squeeze().cpu().numpy()
    except Exception as e:
        logger.debug(f"Live utterance embed failed: {e}")
        return None
    norm = float(np.linalg.norm(emb))
    if norm < 1e-8:
        return None
    return (emb / norm).astype(np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two L2-normalized vectors. Both inputs
    are expected to come from extract_speaker_centroids() output, so the
    L2 norm is already 1 and the cosine reduces to a plain dot product."""
    return float(np.dot(a, b))
