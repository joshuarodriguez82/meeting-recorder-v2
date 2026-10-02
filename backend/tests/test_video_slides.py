"""
Slides pulled out of an imported meeting video.

The videos are real H.264 MP4s encoded with PyAV — the decoder the app
ships — in the shapes a Teams or Zoom recording takes: a shared deck
with the presenter's camera in a corner, a gallery of moving camera
tiles with nothing shared, and a deck whose encoder keyed only once a
minute. Only the pixels are synthetic.
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

av = pytest.importorskip("av")

from core import video_slides as vs  # noqa: E402

W, H, FPS = 640, 360, 5
SLIDE_S = 12


def _slide(k: int) -> np.ndarray:
    img = np.full((H, W, 3), 250, np.uint8)
    img[20:55, 40:450] = (30, 60, 140)                        # title bar
    r = np.random.default_rng(100 + k)
    for row in range(6):                                       # text lines
        y = 85 + row * 35
        img[y:y + 11, 50:50 + int(r.integers(150, 500))] = 40
    img[300:340, 40 + k * 60: 90 + k * 60] = (200, 40, 40)
    return img


def _with_camera(img: np.ndarray, t: float) -> np.ndarray:
    """The presenter's camera, bottom right: a textured room and a head
    that keeps moving — about a sixth of the frame."""
    x0, y0 = W - 200, H - 130
    xx = np.arange(200)[None, :, None]
    img[y0:, x0:] = (60 + (xx % 37) * 2).astype(np.uint8)
    cy = int(65 + 12 * np.sin(t * 1.7))
    cx = int(100 + 15 * np.cos(t * 1.1))
    img[y0 + max(0, cy - 40):y0 + cy + 40, x0 + cx - 30:x0 + cx + 30] = \
        (205, 165, 135)
    return img


def _gallery(t: float, rng=np.random.default_rng(1)) -> np.ndarray:
    img = np.zeros((H, W, 3), np.uint8)
    for gy in range(2):
        for gx in range(2):
            y0, x0 = gy * H // 2, gx * W // 2
            tile = rng.integers(40, 120, (H // 2, W // 2, 3), dtype=np.uint8)
            cy = int(H // 4 + 20 * np.sin(t * (1 + gx + gy)))
            cx = W // 4 + int(15 * np.cos(t * 0.7 * (1 + gy)))
            tile[cy - 45:cy + 45, cx - 35:cx + 35] = (200, 160, 130)
            img[y0:y0 + H // 2, x0:x0 + W // 2] = tile
    return img


def _video(path: Path, seconds: float, frame_at, gop: int = 10,
           audio: bool = False) -> Path:
    out = av.open(str(path), "w")
    v = out.add_stream("libx264", rate=FPS)
    v.width, v.height, v.pix_fmt = W, H, "yuv420p"
    v.codec_context.gop_size = gop
    v.options = {"g": str(gop), "keyint_min": str(gop), "sc_threshold": "0"}
    for i in range(int(seconds * FPS)):
        frame = av.VideoFrame.from_ndarray(frame_at(i / FPS), format="rgb24")
        frame.pts, frame.time_base = i, Fraction(1, FPS)
        for pkt in v.encode(frame):
            out.mux(pkt)
    for pkt in v.encode(None):
        out.mux(pkt)
    out.close()
    return path


def _deck(t: float) -> np.ndarray:
    return _with_camera(_slide(int(t // SLIDE_S)), t)


# ── on real video ────────────────────────────────────────────────────


def test_one_still_per_slide_despite_the_presenter_camera(tmp_path):
    mp4 = _video(tmp_path / "deck.mp4", 4 * SLIDE_S, _deck)
    slides = vs.extract_slides(mp4, tmp_path / "out")

    assert len(slides) == 4
    # One per slide, each inside its own slide's time.
    assert [int(s.time_s // SLIDE_S) for s in slides] == [0, 1, 2, 3]
    for s in slides:
        p = Path(s.path)
        assert p.suffix == ".png" and p.stat().st_size > 1000
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Named by time so they sort in meeting order.
    assert [Path(s.path).name for s in slides] == sorted(
        Path(s.path).name for s in slides)
    assert not list((tmp_path / "out").glob("*.part"))


def test_a_still_is_the_slide_at_full_resolution(tmp_path):
    mp4 = _video(tmp_path / "deck.mp4", 2 * SLIDE_S, _deck)
    slide = vs.extract_slides(mp4, tmp_path / "out")[1]
    with av.open(slide.path) as c:
        frame = next(c.decode(video=0))
        assert (frame.width, frame.height) == (W, H)
        img = frame.to_ndarray(format="rgb24")
    expected = _slide(1)
    # The slide area (not the camera) matches the source closely.
    diff = np.abs(img[:, :W - 200].astype(int) - expected[:, :W - 200]).mean()
    assert diff < 6


def test_a_gallery_with_nothing_shared_gives_no_slides(tmp_path):
    mp4 = _video(tmp_path / "gallery.mp4", 40, _gallery)
    assert vs.extract_slides(mp4, tmp_path / "out") == []
    assert not (tmp_path / "out").exists()


def test_sparse_keyframes_still_find_every_slide(tmp_path):
    """An encoder that keys once a minute: keyframes alone would miss
    slides, so every frame is decoded instead."""
    mp4 = _video(tmp_path / "sparse.mp4", 4 * SLIDE_S, _deck, gop=300)
    slides = vs.extract_slides(mp4, tmp_path / "out")
    assert [int(s.time_s // SLIDE_S) for s in slides] == [0, 1, 2, 3]
    # Nothing left over from the keyframe pass that was thrown away.
    assert len(list((tmp_path / "out").glob("*.png"))) == 4


def test_audio_only_files_have_no_slides(tmp_path):
    out = av.open(str(tmp_path / "call.m4a"), "w")
    a = out.add_stream("aac", rate=16000)
    a.layout = "mono"
    frame = av.AudioFrame.from_ndarray(
        np.zeros((1, 1024), np.float32), format="fltp", layout="mono")
    frame.sample_rate, frame.pts = 16000, 0
    for pkt in a.encode(frame):
        out.mux(pkt)
    for pkt in a.encode(None):
        out.mux(pkt)
    out.close()
    assert vs.extract_slides(tmp_path / "call.m4a", tmp_path / "o") == []


def test_too_many_slides_are_thinned_to_an_even_spread(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "MAX_SLIDES", 2)
    mp4 = _video(tmp_path / "deck.mp4", 4 * SLIDE_S, _deck)
    slides = vs.extract_slides(mp4, tmp_path / "out")
    assert [int(s.time_s // SLIDE_S) for s in slides] == [0, 2]
    assert len(list((tmp_path / "out").glob("*.png"))) == 2


# ── the policy, on thumbnails ────────────────────────────────────────


def _thumb(slide_k: int) -> np.ndarray:
    big = _slide(slide_k)[..., 0].astype(np.uint8)
    return big[::4, ::4][:vs.THUMB_H, :vs.THUMB_W]


def test_moving_frames_are_never_slides():
    rng = np.random.default_rng(0)
    frames = [(i * 3.0, rng.integers(0, 255, (vs.THUMB_H, vs.THUMB_W))
               .astype(np.uint8)) for i in range(20)]
    assert vs.choose(frames) == []


def test_the_same_slide_held_is_kept_once():
    frames = [(i * 3.0, _thumb(0)) for i in range(10)]
    assert vs.choose(frames) == [1]


def test_a_still_camera_picture_is_not_a_slide():
    """People sitting still in a gallery: nothing moves, but nothing is
    flat either — a room and faces have texture everywhere."""
    rng = np.random.default_rng(3)
    room = rng.integers(40, 200, (vs.THUMB_H, vs.THUMB_W)).astype(np.uint8)
    assert vs.flat_fraction(room) < vs.SLIDE_MIN_FLAT
    assert vs.choose([(i * 3.0, room) for i in range(6)]) == []


def test_a_blank_screen_is_not_a_slide():
    black = np.zeros((vs.THUMB_H, vs.THUMB_W), np.uint8)
    assert vs.choose([(i * 3.0, black) for i in range(5)]) == []


def test_slides_closer_than_the_minimum_gap_are_one():
    frames = [(0.0, _thumb(0)), (1.0, _thumb(0)),
              (2.0, _thumb(1)), (3.0, _thumb(1))]
    assert vs.choose(frames) == [1]


# ── what the summary sees ────────────────────────────────────────────


def test_more_than_eight_screenshots_are_sent_as_an_even_spread(
        tmp_path, monkeypatch):
    if "anthropic" not in sys.modules:
        monkeypatch.setitem(sys.modules, "anthropic", MagicMock())
    from core import summarizer
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    paths = []
    for i in range(24):
        p = tmp_path / f"slide_{i:02d}.png"
        p.write_bytes(png + bytes([i]))
        paths.append(str(p))
    blocks = summarizer._image_blocks(paths)
    assert len(blocks) == summarizer._MAX_SCREENSHOTS == 8
    import base64
    sent = [base64.b64decode(b["source"]["data"])[-1] for b in blocks]
    assert sent == [0, 3, 6, 9, 12, 15, 18, 21]
