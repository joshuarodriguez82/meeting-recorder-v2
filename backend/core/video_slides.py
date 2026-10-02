"""
Slides and shared screens, pulled out of an imported meeting video.

WHY
---
During a recording the user can take screenshots, and the summary reads
them alongside the transcript — "the timeline on slide 4" means
something. A meeting someone else recorded has the same shared screens
in it, but as video, so the summary only heard about them. This finds
the moments the shared screen changed and keeps one still of each, as
ordinary session screenshots: the Screenshots tab shows them and the
summarizer sends them with the transcript.

WHAT COUNTS AS A SLIDE
----------------------
Frames are sampled every few seconds and reduced to a small grayscale
grid of blocks. A sample is kept when it is

  * still     — almost nothing moved since the previous sample, which
                is what a slide or document on screen does and a
                talking head does not;
  * new       — a real share of the screen differs from the last one
                kept (a new slide, not the same slide with the cursor
                moved); and
  * slide-like — a fair share of it is flat (slide backgrounds, page
                margins), which camera images rarely are.

A meeting with no screen share (gallery view throughout) should yield
nothing, and if a video still produces more candidates than is useful,
an even spread is kept. Nothing here is ML; it is deliberately cheap
and predictable.

DECODING
--------
Keyframes only when they are dense enough (Teams and Zoom recordings
typically key every few seconds): an hour of 1080p costs seconds, not
minutes. With sparse keyframes every frame is decoded, and only the
sampled ones are converted. Decisions are made as frames stream past
and a kept still is written at once, so memory stays at a couple of
thumbnails however long the video is.

Stills are written as PNG through PyAV's own encoder (no Pillow needed;
text on a slide stays sharp), at most 1600 px wide so each stays well
under the image API's 5 MB limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

import numpy as np

SAMPLE_EVERY_S = 3.0
THUMB_W, THUMB_H = 160, 90
BLOCK = 10                       # 16 x 9 grid of 10 px blocks
BLOCK_DIFF = 10.0                # mean |Δ| (0-255) for a block to count as changed
STILL_MAX_CHANGED = 0.05         # ≤ 5 % of blocks moved since the last sample
NEW_MIN_CHANGED = 0.12           # ≥ 12 % of blocks differ from the last slide
FLAT_BLOCK_STD = 4.0
SLIDE_MIN_FLAT = 0.25            # ≥ 25 % of blocks are flat
BLANK_MAX_STD = 6.0              # a black/blank frame (lobby, fade) isn't a slide
MIN_GAP_S = 6.0
MAX_SLIDES = 40
MAX_WIDTH = 1600
SPARSE_KEYFRAMES_S = 8.0


@dataclass
class Slide:
    time_s: float
    path: str


def _blocks(thumb: np.ndarray) -> np.ndarray:
    h, w = thumb.shape
    return thumb[: h - h % BLOCK, : w - w % BLOCK].reshape(
        h // BLOCK, BLOCK, w // BLOCK, BLOCK).swapaxes(1, 2)


def changed_fraction(a: np.ndarray, b: np.ndarray) -> float:
    """Share of grid blocks whose mean absolute difference exceeds
    BLOCK_DIFF."""
    d = np.abs(a.astype(np.float32) - b.astype(np.float32))
    per_block = _blocks(d).mean(axis=(2, 3))
    return float((per_block > BLOCK_DIFF).mean())


def flat_fraction(thumb: np.ndarray) -> float:
    per_block = _blocks(thumb.astype(np.float32)).std(axis=(2, 3))
    return float((per_block < FLAT_BLOCK_STD).mean())


def is_blank(thumb: np.ndarray) -> bool:
    return float(thumb.astype(np.float32).std()) < BLANK_MAX_STD


class SlidePicker:
    """The whole keep-or-skip policy, fed one sample at a time so a long
    video never holds more than two thumbnails. Pure: thumbnails in,
    decisions out — testable without a video."""

    def __init__(self) -> None:
        self._prev: Optional[np.ndarray] = None
        self._last_kept: Optional[np.ndarray] = None
        self._last_t = -1e9

    def offer(self, t: float, thumb: np.ndarray) -> bool:
        prev, self._prev = self._prev, thumb
        if prev is None:
            return False
        if changed_fraction(prev, thumb) > STILL_MAX_CHANGED:
            return False                               # moving: not a slide
        if is_blank(thumb) or flat_fraction(thumb) < SLIDE_MIN_FLAT:
            return False
        if self._last_kept is not None and (
                changed_fraction(self._last_kept, thumb) < NEW_MIN_CHANGED
                or t - self._last_t < MIN_GAP_S):
            return False
        self._last_kept, self._last_t = thumb, t
        return True


def choose(samples: List[Tuple[float, np.ndarray]]) -> List[int]:
    """Indexes of ``samples`` a SlidePicker keeps, thinned to an even
    spread of at most MAX_SLIDES."""
    picker = SlidePicker()
    kept = [i for i, (t, thumb) in enumerate(samples) if picker.offer(t, thumb)]
    return thin(kept)


def thin(items: list) -> list:
    if len(items) <= MAX_SLIDES:
        return items
    step = len(items) / MAX_SLIDES
    return [items[int(k * step)] for k in range(MAX_SLIDES)]


def _thumb(frame) -> np.ndarray:
    return frame.reformat(width=THUMB_W, height=THUMB_H,
                          format="gray").to_ndarray()


def _iter_samples(path: Path, av, keyframes_only: bool,
                  progress: Optional[Callable[[float], None]]):
    """Yield (time_s, frame) roughly every SAMPLE_EVERY_S, preceded by
    one (duration_s, None)."""
    with av.open(str(path)) as container:
        if not container.streams.video:
            return
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        stream.codec_context.skip_frame = (
            "NONKEY" if keyframes_only else "DEFAULT")
        total = (float(container.duration / 1_000_000)
                 if container.duration else 0.0)
        yield total, None                  # the length, before any frame
        next_t = 0.0
        for frame in container.decode(stream):
            if frame.time is None:
                continue
            t = float(frame.time)
            if t + 1e-6 < next_t:
                continue
            next_t = t + SAMPLE_EVERY_S
            if progress and total > 0:
                progress(min(1.0, t / total))
            yield t, frame


def _write_png(frame, path: Path, av) -> None:
    w, h = frame.width, frame.height
    if w > MAX_WIDTH:
        h = int(round(h * MAX_WIDTH / w)) // 2 * 2
        w = MAX_WIDTH
    rgb = frame.reformat(width=w, height=h, format="rgb24")
    cc = av.CodecContext.create("png", "w")
    cc.width, cc.height, cc.pix_fmt = w, h, "rgb24"
    cc.time_base = Fraction(1, 1)
    data = b"".join(bytes(p) for p in cc.encode(rgb))
    data += b"".join(bytes(p) for p in cc.encode(None))
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def extract_slides(video, out_dir, *,
                   progress: Optional[Callable[[float], None]] = None,
                   av_module=None) -> List[Slide]:
    """Find the slides / shared screens in ``video`` and write one PNG
    per slide into ``out_dir``. Returns them in time order; an empty
    list for audio-only files and for meetings with no screen share.
    Raises only for a file that cannot be opened at all."""
    av = av_module
    if av is None:
        import av  # type: ignore[no-redef]
    video, out_dir = Path(video), Path(out_dir)

    def _pass(keyframes_only: bool) -> Tuple[List[Slide], float]:
        picker = SlidePicker()
        slides: List[Slide] = []
        total, count = 0.0, 0
        for t, frame in _iter_samples(video, av, keyframes_only, progress):
            if frame is None:
                total = t
                continue
            count += 1
            if picker.offer(t, _thumb(frame)):
                out_dir.mkdir(parents=True, exist_ok=True)
                mins, secs = divmod(int(t), 60)
                path = out_dir / f"slide_{mins:02d}m{secs:02d}s.png"
                _write_png(frame, path, av)
                slides.append(Slide(time_s=t, path=str(path)))
        # Average spacing over the whole video, not between the frames
        # seen: a video keyed once (or not at all) after the start
        # yields one sample, and its spacing is its length.
        gap = total / count if count else total
        return slides, gap

    slides, gap = _pass(keyframes_only=True)
    if gap > SPARSE_KEYFRAMES_S:
        # Too few keyframes to see every slide: decode everything.
        for s in slides:
            Path(s.path).unlink(missing_ok=True)
        slides, _ = _pass(keyframes_only=False)

    keep = thin(slides)
    for s in slides:
        if s not in keep:
            Path(s.path).unlink(missing_ok=True)
    if progress:
        progress(1.0)
    return keep
