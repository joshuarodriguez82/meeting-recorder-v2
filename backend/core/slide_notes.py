"""
Slide-by-slide notes for a meeting imported from video.

WHY
---
core/video_slides keeps a still of each slide or shared screen. The
summary already reads them, but it reads them as a set: it can say what
the deck covered, not what was said about slide 4. A deck-driven
meeting is reviewed slide by slide — "what did they say about the
pricing slide?" — so this pairs each slide with the part of the
transcript spoken while it was on screen, and asks for the points,
questions and actions raised on it.

TIMING
------
A slide's still is named by when it was kept (slide_12m04s.png). It is
kept a sample or two after it appears — the video is sampled every few
seconds and a slide must hold still for one — so each slide's stretch of
the transcript starts DETECTION_LAG_S earlier and runs until the next
slide's does. Screenshots the user took while recording carry no such
time and are not slides here.

This module is the pure part: which words belong to which slide, how a
reply is applied, and the exported document. The model call is
Summarizer.slide_notes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

#: core/video_slides keeps a still SAMPLE_EVERY_S after a change at the
#: earliest; start each slide's stretch that much before its still.
DETECTION_LAG_S = 3.0
#: One slide's share of the transcript sent to the model; a slide left
#: up for twenty minutes of discussion is trimmed to its first part.
MAX_CHARS_PER_SLIDE = 6000
MAX_ITEMS = 6
MAX_TEXT = 300

_SLIDE_NAME = re.compile(r"slide_(\d+)m(\d{2})s\.png$", re.IGNORECASE)


def slide_time(path: str) -> Optional[float]:
    """Seconds into the meeting a slide still was kept at, from its name;
    None for anything that isn't a slide from an imported video."""
    m = _SLIDE_NAME.search(Path(str(path)).name)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def slide_sections(session) -> List[dict]:
    """The slides of a session, in order, each with the transcript spoken
    while it was up: [{"slide", "time_s", "path", "text"}]. Empty when
    the session has no slides from a video."""
    slides = sorted(
        ((t, p) for p in (getattr(session, "screenshots", None) or [])
         if (t := slide_time(p)) is not None),
        key=lambda x: x[0])
    if not slides:
        return []
    names = {sid: sp.display_name for sid, sp in
             (getattr(session, "speakers", None) or {}).items()}
    starts = [max(0.0, t - DETECTION_LAG_S) for t, _ in slides]
    sections = []
    for i, (t, path) in enumerate(slides):
        lo = 0.0 if i == 0 else starts[i]
        hi = starts[i + 1] if i + 1 < len(slides) else float("inf")
        lines = []
        for seg in getattr(session, "segments", None) or []:
            mid = (float(seg.start) + float(seg.end)) / 2
            if lo <= mid < hi:
                who = names.get(seg.speaker_id, seg.speaker_id)
                lines.append(f"{who}: {seg.text.strip()}")
        text = "\n".join(lines)
        if len(text) > MAX_CHARS_PER_SLIDE:
            text = text[:MAX_CHARS_PER_SLIDE].rsplit("\n", 1)[0] + "\n[…]"
        sections.append({"slide": i + 1, "time_s": t, "path": path,
                         "text": text})
    return sections


def _clean_list(value) -> List[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = " ".join(str(v or "").split())
        if s:
            out.append(s[:MAX_TEXT])
    return out[:MAX_ITEMS]


def apply_notes(sections: List[dict], replies: Dict[int, dict]) -> List[dict]:
    """Each section with the model's notes for it. A slide the reply
    skipped keeps its place with empty notes; a reply for a slide that
    doesn't exist is ignored."""
    out = []
    for sec in sections:
        r = replies.get(sec["slide"]) or {}
        out.append({
            "slide": sec["slide"],
            "time_s": sec["time_s"],
            "path": sec["path"],
            "title": " ".join(str(r.get("title") or "").split())[:120],
            "summary": " ".join(str(r.get("summary") or "").split())[:600],
            "points": _clean_list(r.get("points")),
            "questions": _clean_list(r.get("questions")),
            "actions": _clean_list(r.get("actions")),
            "discussed": bool(sec["text"].strip()),
        })
    return out


def _clock(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def render_markdown(session, notes: Optional[List[dict]] = None) -> str:
    """The slide-notes document; "" when there are none."""
    notes = notes if notes is not None else (
        getattr(session, "slide_notes", None) or [])
    if not notes:
        return ""
    title = (getattr(session, "display_name", "") or "").strip()
    lines = [f"# Slide notes — {title}" if title else "# Slide notes", ""]
    lines.append(
        "_Each slide or shared screen from the recording, with what was "
        "said while it was up. Written by AI from the transcript — check "
        "before relying on it._")
    lines.append("")
    for n in notes:
        head = f"## Slide {n['slide']} · {_clock(n['time_s'])}"
        if n.get("title"):
            head += f" — {n['title']}"
        lines.append(head)
        lines.append("")
        if n.get("summary"):
            lines.append(n["summary"])
            lines.append("")
        elif not n.get("discussed"):
            lines.append("_Shown without discussion._")
            lines.append("")
        for label, key in (("Points", "points"), ("Questions raised", "questions"),
                           ("Actions", "actions")):
            if n.get(key):
                lines.append(f"**{label}**")
                lines += [f"- {x}" for x in n[key]]
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"
