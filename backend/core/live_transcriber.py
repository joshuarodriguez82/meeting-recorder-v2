"""
Live transcription pipeline.

Wraps the existing TranscriptionEngine in a streaming, windowed harness:

  AudioCapture._on_chunk  →  recording_service  →  LiveTranscriber.push_audio
                                                          │
                                          buffer until 15s accumulated
                                                          │
                                                          ▼
                              worker thread pulls each window
                              and calls faster-whisper synchronously
                                                          │
                                                          ▼
                              segments published onto every consumer queue
                                  (SSE clients drain via subscribe())

Design choices:

- 15-second non-overlapping windows. Simple, no overlap-dedup
  bookkeeping. Boundary words occasionally get split between two
  windows — minor cosmetic issue we accept for v1.

- Whisper runs on the worker thread synchronously. We bypass the
  TranscriptionEngine.transcribe() async wrapper because that wrapper
  itself just runs the same blocking call in asyncio.to_thread, and we
  already have our own thread.

- Pub/sub via thread-safe Queues. Every SSE connection subscribes for
  its own Queue and unsubscribes on disconnect; pushes use put_nowait
  so a slow consumer drops segments rather than stalls the worker.

- Model load is the caller's responsibility (recording_service should
  pre-warm via Services.ensure_models_loaded). If the engine isn't
  ready when push_audio fires, audio queues up but nothing gets
  transcribed until the engine arrives via set_engine().

CPU notes (Apple Silicon CPU runs faster-whisper at 2-3x realtime on
`base`, 6-10x on `tiny`). The 15s window therefore takes 5-7s to
transcribe on `base`, easily faster than realtime, so the queue stays
bounded. On `medium`/`large` the worker can fall behind realtime; that
manifests as live text trailing the recording. The eventual final
processing pass at /sessions/{id}/process produces the canonical
transcript, so live falling behind is purely a UX inconvenience.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Iterable, List, Optional

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)


WINDOW_SECONDS = 15.0
TARGET_SR = 16000
# Min remaining audio at stop time before we bother running Whisper on
# the tail. Sub-second tails are typically silence + a fade-out and
# produce empty segments.
MIN_TAIL_SECONDS = 1.0


def _mix(mic: np.ndarray, loopback: np.ndarray, target_len: int) -> np.ndarray:
    """Mix mic + loopback into a single mono float32 window.

    The mic length is the source of truth — loopback gets truncated or
    zero-padded to match. We zero-pad rather than stretching so an
    underrun (loopback briefly behind) inserts silence in that span
    instead of altering the speaker's pitch.

    Both inputs are expected mono float32 in [-1, 1]. The sum can
    exceed [-1, 1] when both speakers are loud simultaneously, so we
    clip — clipping distorts loud peaks but Whisper's robust to it,
    and it's better than silently letting MKL emit NaNs from an
    out-of-range float.
    """
    if len(mic) != target_len:
        if len(mic) > target_len:
            mic = mic[:target_len]
        else:
            pad = np.zeros(target_len - len(mic), dtype=np.float32)
            mic = np.concatenate([mic, pad])
    if len(loopback) > target_len:
        loopback = loopback[:target_len]
    elif len(loopback) < target_len:
        pad = np.zeros(target_len - len(loopback), dtype=np.float32)
        loopback = np.concatenate([loopback, pad])
    mixed = mic + loopback
    np.clip(mixed, -1.0, 1.0, out=mixed)
    return mixed.astype(np.float32, copy=False)


class LiveTranscriber:
    """Streams audio into a windowed transcription pipeline.

    The recording service feeds mic audio in via push_audio(). A
    background worker drains windows of WINDOW_SECONDS and runs Whisper
    on each, publishing segments to subscribed Queues.
    """

    def __init__(
        self,
        engine_provider,
        samplerate: int = TARGET_SR,
    ):
        # Indirection so we can construct LiveTranscriber before models
        # have loaded. engine_provider() returns the current
        # TranscriptionEngine or None — checked on every window.
        self._engine_provider = engine_provider
        self._sr = samplerate
        self._window_samples = int(self._sr * WINDOW_SECONDS)
        # Two parallel ring-style buffers — one for the mic, one for the
        # system-audio loopback (if any). We drain the mic buffer to
        # decide when a window is ready (mic always provides samples at
        # wall-clock rate, even silence), then drain whatever loopback
        # audio overlaps the same time range and mix the two before
        # transcription. Mic is the timing source of truth; loopback
        # drift is handled by zero-padding to match the mic length.
        self._mic_buffer: List[np.ndarray] = []
        self._mic_buffer_samples = 0
        self._loopback_buffer: List[np.ndarray] = []
        self._loopback_buffer_samples = 0
        self._buffer_lock = threading.Lock()
        # Global timestamp where the next window starts (seconds since
        # recording start). Whisper hands back window-relative timings;
        # we offset to global before publishing.
        self._next_window_start: float = 0.0
        self._consumers: List[queue.Queue] = []
        self._consumers_lock = threading.Lock()
        self._running = False
        self._worker: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, samplerate: int) -> None:
        """Reset state and spawn the worker. Idempotent if already running."""
        if self._running:
            return
        self._sr = samplerate
        self._window_samples = int(self._sr * WINDOW_SECONDS)
        with self._buffer_lock:
            self._mic_buffer.clear()
            self._mic_buffer_samples = 0
            self._loopback_buffer.clear()
            self._loopback_buffer_samples = 0
        self._next_window_start = 0.0
        self._running = True
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="live-transcriber",
        )
        self._worker.start()
        logger.info(
            f"LiveTranscriber started — window={WINDOW_SECONDS}s, "
            f"sr={self._sr}Hz, samples_per_window={self._window_samples}")

    def stop(self) -> None:
        """Stop accepting new audio, drain remaining buffer, signal SSE
        clients with a None sentinel so they close their stream."""
        if not self._running:
            return
        self._running = False
        if self._worker is not None:
            self._worker.join(timeout=10.0)
            self._worker = None
        # Send done sentinel to every consumer so SSE handlers exit cleanly.
        with self._consumers_lock:
            for q in self._consumers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._consumers.clear()
        logger.info("LiveTranscriber stopped")

    def push_audio(self, chunk: np.ndarray) -> None:
        """Append mic audio (mono float32 at self._sr) to the mic buffer.

        Called from the audio capture callback thread. Drops the chunk
        if we're not running so a late callback after stop() can't leak
        into the next session.
        """
        if not self._running:
            return
        if chunk.ndim > 1:
            chunk = chunk.mean(axis=1)
        block = np.ascontiguousarray(chunk, dtype=np.float32)
        with self._buffer_lock:
            self._mic_buffer.append(block)
            self._mic_buffer_samples += len(block)

    def push_loopback(self, chunk: np.ndarray) -> None:
        """Append system-audio loopback (mono float32 at self._sr) to
        the loopback buffer.

        Called from AudioCapture's loopback reader thread (Win) or
        writer thread (Mac), via recording_service. Same drop-when-
        stopped policy as push_audio.

        Mic and loopback come from independent OS audio paths and arrive
        on different threads at slightly different cadences. We don't try
        to sample-align them here — _drain_window mixes whatever loopback
        we have against each mic window, zero-padding any tail mismatch.
        Sub-frame drift is inaudible to Whisper at speech-recognition
        fidelity.
        """
        if not self._running:
            return
        if chunk.ndim > 1:
            chunk = chunk.mean(axis=1)
        block = np.ascontiguousarray(chunk, dtype=np.float32)
        with self._buffer_lock:
            self._loopback_buffer.append(block)
            self._loopback_buffer_samples += len(block)

    def subscribe(self, max_pending: int = 256) -> queue.Queue:
        """Return a Queue that will receive published segment dicts.

        A None value on the queue means the recording stopped — drain
        and close. SSE handler should call unsubscribe(q) on disconnect.
        """
        q: queue.Queue = queue.Queue(maxsize=max_pending)
        with self._consumers_lock:
            self._consumers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._consumers_lock:
            try:
                self._consumers.remove(q)
            except ValueError:
                pass

    # ── Worker internals ─────────────────────────────────────────────

    def _drain_window(self) -> Optional[np.ndarray]:
        """Pull the next full window off the buffers and return mic+loopback
        mixed into a single mono float32 array. Returns None when the mic
        buffer hasn't accumulated a full window yet (mic is the timing
        source of truth — it always provides samples at wall-clock rate)."""
        with self._buffer_lock:
            if self._mic_buffer_samples < self._window_samples:
                return None
            mic_audio = (np.concatenate(self._mic_buffer)
                         if self._mic_buffer else np.zeros(0, dtype=np.float32))
            mic_window = mic_audio[:self._window_samples].copy()
            mic_tail = mic_audio[self._window_samples:]
            self._mic_buffer = [mic_tail] if len(mic_tail) else []
            self._mic_buffer_samples = len(mic_tail)

            # Drain up to one window of loopback. Loopback often has less
            # data than mic (different OS audio path, may have started a
            # touch later, may be empty entirely if no system audio is
            # selected). Take whatever's there.
            lb_audio = (np.concatenate(self._loopback_buffer)
                        if self._loopback_buffer
                        else np.zeros(0, dtype=np.float32))
            lb_window = lb_audio[:self._window_samples]
            lb_tail = lb_audio[self._window_samples:]
            self._loopback_buffer = [lb_tail] if len(lb_tail) else []
            self._loopback_buffer_samples = len(lb_tail)

        return _mix(mic_window, lb_window, self._window_samples)

    def _drain_tail(self) -> Optional[np.ndarray]:
        """At stop time, return whatever's left in both buffers as a mixed
        window. Returns None if the mic tail is too short to bother
        (sub-second tails are usually silence + a fade-out)."""
        with self._buffer_lock:
            if self._mic_buffer_samples < int(self._sr * MIN_TAIL_SECONDS):
                self._mic_buffer = []
                self._mic_buffer_samples = 0
                self._loopback_buffer = []
                self._loopback_buffer_samples = 0
                return None
            mic_audio = (np.concatenate(self._mic_buffer)
                         if self._mic_buffer else np.zeros(0, dtype=np.float32))
            lb_audio = (np.concatenate(self._loopback_buffer)
                        if self._loopback_buffer
                        else np.zeros(0, dtype=np.float32))
            self._mic_buffer = []
            self._mic_buffer_samples = 0
            self._loopback_buffer = []
            self._loopback_buffer_samples = 0
        return _mix(mic_audio, lb_audio, len(mic_audio))

    def _publish(self, segment: dict) -> None:
        """Best-effort fan-out to every consumer. A slow client gets its
        new segments dropped (queue.Full) rather than backpressuring
        the worker — the canonical transcript at /process catches them."""
        with self._consumers_lock:
            consumers = list(self._consumers)
        for q in consumers:
            try:
                q.put_nowait(segment)
            except queue.Full:
                logger.debug("Live segment dropped — consumer too slow")

    def _transcribe_window(self, audio: np.ndarray, window_start: float) -> int:
        """Run faster-whisper on one window of audio. Returns # of
        non-empty segments published."""
        engine = self._engine_provider()
        if engine is None:
            # Engine not loaded yet — drop the window. We could re-buffer,
            # but the user gets a much better signal from "silence" than
            # from a 60-second wall of buffered audio that suddenly
            # appears later. The eventual /process pass will produce the
            # canonical transcript anyway.
            logger.debug("Live window dropped — TranscriptionEngine not ready")
            return 0
        # Bypass the async wrapper; call the underlying WhisperModel
        # directly. faster-whisper's transcribe() accepts a numpy array
        # at 16 kHz mono float32 — exactly what we have here.
        try:
            segments_iter, _ = engine._model.transcribe(
                audio, language="en", vad_filter=True,
            )
        except Exception as e:
            logger.exception(f"Live transcribe call raised: {e}")
            return 0
        published = 0
        for s in segments_iter:
            text = (s.text or "").strip()
            if not text:
                continue
            self._publish({
                "start": float(s.start) + window_start,
                "end": float(s.end) + window_start,
                "text": text,
            })
            published += 1
        return published

    def _run(self) -> None:
        """Worker loop. Pulls windows, runs Whisper, publishes segments.
        On stop() this loop exits and the tail flush runs once."""
        try:
            while self._running:
                window = self._drain_window()
                if window is None:
                    # Buffer not full yet — short sleep and re-check.
                    # Half a second is short enough to feel responsive,
                    # long enough to avoid a busy-wait spin.
                    time.sleep(0.5)
                    continue
                start = self._next_window_start
                self._next_window_start += WINDOW_SECONDS
                t0 = time.time()
                count = self._transcribe_window(window, start)
                elapsed = time.time() - t0
                logger.info(
                    f"Live window @ {start:.1f}s → {count} segments "
                    f"in {elapsed:.1f}s")
        finally:
            tail = self._drain_tail()
            if tail is not None:
                start = self._next_window_start
                logger.info(f"Live tail flush @ {start:.1f}s "
                            f"({len(tail)/self._sr:.1f}s of audio)")
                try:
                    self._transcribe_window(tail, start)
                except Exception as e:
                    logger.exception(f"Tail transcribe failed: {e}")


def serialize_segment_sse(segment: dict) -> str:
    """Format a segment dict as a Server-Sent Events `data:` line."""
    return f"data: {json.dumps(segment)}\n\n"
