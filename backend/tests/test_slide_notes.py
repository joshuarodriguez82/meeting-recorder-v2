"""
Slide-by-slide notes: each slide from an imported video, with what was
said while it was up.

Sessions are built from the real Session / Segment / Speaker types, and
slide paths use the exact names core/video_slides writes.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from core import slide_notes as sn
from models.segment import Segment
from models.session import Session
from models.speaker import Speaker


def _session(slide_times, segments, extra_shots=()):
    s = Session(session_id="SLIDES01")
    s.display_name = "Hooli kickoff"
    s.speakers = {"SPEAKER_00": Speaker("SPEAKER_00", "Jane Doe"),
                  "SPEAKER_01": Speaker("SPEAKER_01", "Rob Poe")}
    s.screenshots = list(extra_shots) + [
        f"/rec/screenshots/session_SLIDES01/slide_{int(t) // 60:02d}m{int(t) % 60:02d}s.png"
        for t in slide_times]
    s.segments = [Segment(sp, a, b, text) for sp, a, b, text in segments]
    return s


SEGMENTS = [
    ("SPEAKER_00", 0.0, 4.0, "Welcome everyone."),
    ("SPEAKER_00", 10.0, 14.0, "This is the timeline."),
    ("SPEAKER_01", 30.0, 36.0, "When does the pilot start?"),
    ("SPEAKER_00", 61.0, 66.0, "Pricing is per agent."),
    ("SPEAKER_01", 70.0, 74.0, "I will send the quote by Friday."),
    ("SPEAKER_00", 125.0, 130.0, "Thanks all."),
]


def test_slide_time_from_the_still_name():
    assert sn.slide_time("x/slide_12m04s.png") == 724
    assert sn.slide_time("slide_105m00s.png") == 6300      # over 99 min
    assert sn.slide_time("screenshot_2026-10-02.png") is None


def test_each_slide_gets_what_was_said_while_it_was_up():
    # Slides kept at 0:12 and 1:03 → they appeared ~3 s earlier.
    s = _session([12, 63], SEGMENTS)
    secs = sn.slide_sections(s)
    assert [x["slide"] for x in secs] == [1, 2]
    # Slide 1 takes everything before slide 2 appeared (including the
    # welcome before slide 1, which has nowhere else to go).
    assert secs[0]["text"].splitlines() == [
        "Jane Doe: Welcome everyone.",
        "Jane Doe: This is the timeline.",
        "Rob Poe: When does the pilot start?",
    ]
    assert secs[1]["text"].splitlines() == [
        "Jane Doe: Pricing is per agent.",
        "Rob Poe: I will send the quote by Friday.",
        "Jane Doe: Thanks all.",
    ]


def test_screenshots_taken_while_recording_are_not_slides():
    s = _session([], SEGMENTS,
                 extra_shots=["/rec/screenshots/session_X/shot_001.png"])
    assert sn.slide_sections(s) == []


def test_a_long_discussion_is_trimmed():
    long = [("SPEAKER_00", float(i), float(i) + 0.9, "word " * 40)
            for i in range(400)]
    secs = sn.slide_sections(_session([3], long))
    assert len(secs[0]["text"]) <= sn.MAX_CHARS_PER_SLIDE + 5
    assert secs[0]["text"].endswith("[…]")


def test_replies_are_cleaned_and_unknown_slides_ignored():
    secs = sn.slide_sections(_session([12, 63], SEGMENTS))
    notes = sn.apply_notes(secs, {
        1: {"title": "  Timeline  ", "summary": "The plan.",
            "points": ["a", "", None, "b"] + [f"x{i}" for i in range(10)],
            "questions": "not a list", "actions": ["Rob: send quote"]},
        7: {"title": "No such slide"},
    })
    assert [n["slide"] for n in notes] == [1, 2]
    assert notes[0]["title"] == "Timeline"
    assert notes[0]["points"][:2] == ["a", "b"]
    assert len(notes[0]["points"]) == sn.MAX_ITEMS
    assert notes[0]["questions"] == []
    assert notes[1]["title"] == "" and notes[1]["discussed"] is True


def test_the_document():
    s = _session([12, 63], SEGMENTS)
    s.slide_notes = sn.apply_notes(sn.slide_sections(s), {
        1: {"title": "Timeline", "summary": "When the pilot starts.",
            "questions": ["When does the pilot start?"]},
        2: {"title": "Pricing", "actions": ["Rob Poe: send the quote by Friday"]},
    })
    md = sn.render_markdown(s)
    assert md.startswith("# Slide notes — Hooli kickoff")
    assert "## Slide 1 · 0:12 — Timeline" in md
    assert "**Questions raised**\n- When does the pilot start?" in md
    assert "## Slide 2 · 1:03 — Pricing" in md
    assert "**Actions**\n- Rob Poe: send the quote by Friday" in md
    assert "Written by AI" in md
    assert sn.render_markdown(_session([], SEGMENTS)) == ""


def test_an_undiscussed_slide_says_so():
    s = _session([12, 200], SEGMENTS[:3])
    s.slide_notes = sn.apply_notes(sn.slide_sections(s), {})
    assert "_Shown without discussion._" in sn.render_markdown(s)


# ── the model call ───────────────────────────────────────────────────


def _summarizer(monkeypatch, replies):
    if "anthropic" not in sys.modules:
        monkeypatch.setitem(sys.modules, "anthropic", MagicMock())
    from core import summarizer
    obj = summarizer.Summarizer.__new__(summarizer.Summarizer)
    obj._provider = "anthropic"
    obj._model = "claude-haiku-4-5-20251001"
    obj._budget = lambda n: n
    calls = []

    async def _chat(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return replies[len(calls) - 1]
    obj._chat = _chat
    return obj, calls


def test_slides_go_in_order_with_their_images_in_batches(monkeypatch):
    many = [("SPEAKER_00", t * 10.0, t * 10.0 + 2, f"Point {t}.")
            for t in range(30)]
    s = _session([3 + 10 * i for i in range(25)], many)
    secs = sn.slide_sections(s)
    import json
    first = json.dumps({"slides": [{"slide": n, "title": f"T{n}"}
                                   for n in range(1, 21)]})
    second = json.dumps({"slides": [{"slide": n, "title": f"T{n}"}
                                    for n in range(21, 26)]})
    summ, calls = _summarizer(monkeypatch, [first, second])
    out = asyncio.run(summ.slide_notes(secs))

    assert len(calls) == 2                               # 20 + 5
    assert calls[0]["image_paths"] == [x["path"] for x in secs[:20]]
    assert calls[0]["image_limit"] == 20                 # not the 8-image cap
    assert "slides 1–20, in that order" in calls[0]["prompt"]
    assert "=== Slide 21 ===" in calls[1]["prompt"]
    assert sorted(out) == list(range(1, 26))
    assert out[25]["title"] == "T25"


def test_a_text_only_model_gets_no_images(monkeypatch):
    s = _session([12], SEGMENTS)
    summ, calls = _summarizer(monkeypatch, ['{"slides": []}'])
    summ._provider = "openai"
    asyncio.run(summ.slide_notes(sn.slide_sections(s)))
    assert calls[0]["image_paths"] is None
    assert "images above" not in calls[0]["prompt"]


def test_the_eight_image_cap_still_spreads_ordinary_screenshots(
        tmp_path, monkeypatch):
    if "anthropic" not in sys.modules:
        monkeypatch.setitem(sys.modules, "anthropic", MagicMock())
    from core import summarizer
    paths = []
    for i in range(12):
        p = tmp_path / f"s{i:02d}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]))
        paths.append(str(p))
    assert len(summarizer._image_blocks(paths)) == 8
    assert len(summarizer._image_blocks(paths, limit=12)) == 12


# ── processing ───────────────────────────────────────────────────────


def test_processing_writes_slide_notes_and_survives_a_failure(monkeypatch):
    from types import SimpleNamespace
    sys.modules.setdefault("dotenv", MagicMock())
    from _app_import import import_app
    import_app()
    import server

    s = _session([12, 63], SEGMENTS)

    async def _ok(sections):
        return {1: {"title": "Timeline"}, 2: {"title": "Pricing"}}
    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace(slide_notes=_ok))
    assert asyncio.run(server._make_slide_notes(s)) == "ok (2 slides)"
    assert [n["title"] for n in s.slide_notes] == ["Timeline", "Pricing"]

    async def _boom(sections):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace(slide_notes=_boom))
    assert asyncio.run(server._make_slide_notes(s)).startswith("failed")

    plain = _session([], SEGMENTS)
    assert asyncio.run(server._make_slide_notes(plain)) == "skipped (no slides)"
    assert plain.slide_notes is None


def test_slide_notes_round_trip_and_export(tmp_path):
    from services.export_service import ExportService
    from services.session_service import SessionService
    svc = SessionService(str(tmp_path / "rec"), index_enabled=False)
    s = _session([12, 63], SEGMENTS)
    s.slide_notes = sn.apply_notes(sn.slide_sections(s),
                                   {1: {"title": "Timeline"}})
    svc.save(s)
    loaded = svc.load_full("SLIDES01")
    assert loaded.slide_notes[0]["title"] == "Timeline"
    exp = ExportService(str(tmp_path / "out"))
    path = exp.export_slide_notes(loaded)
    assert path.endswith(".md") and "slide_notes_" in path
    assert "Timeline" in open(path, encoding="utf-8").read()
