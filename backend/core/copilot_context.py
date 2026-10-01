"""
What the Co-Pilot model is told about the meeting it is coaching.

Two defects this module exists for:

* **The meeting had no name.** Both tick endpoints read
  ``session.meeting_name``, a field Session never had (the name lives in
  ``display_name``), so the "Meeting: …" line was empty on every tick
  ever sent. Client, project, organiser and attendees — all on the
  session from the calendar invite — were never sent at all.
* **Everyone else was "them".** Transcript lines were labelled with the
  stream (``[you]`` / ``[them]``), discarding the live speaker split
  ("Speaker 2", or a recognised name). The model could not tell the
  customer's architect from their project manager, which is most of
  what coaching a multi-party call needs.

Pure: session in, text out.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

MAX_ATTENDEES = 15
MAX_NOTES_CHARS = 800


def speaker_name(segment: dict) -> str:
    """The best label we have for who said a segment."""
    stream = (segment.get("speaker") or "").strip().lower()
    if stream == "you":
        return "You"
    if stream == "room":
        return "Room"
    label = (segment.get("speaker_label") or "").strip()
    if label:
        return label
    return "Other participant"


def format_transcript(segments: Iterable[dict], limit: int = 200) -> str:
    """Recent transcript as ``Name: text`` lines, consecutive lines from
    the same speaker joined so the model reads turns, not fragments."""
    turns: List[List[str]] = []
    for s in list(segments)[-limit:]:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        who = speaker_name(s)
        if turns and turns[-1][0] == who:
            turns[-1][1] += " " + text
        else:
            turns.append([who, text])
    return "\n".join(f"{who}: {text}" for who, text in turns)


def meeting_context(session: Optional[object]) -> str:
    """The meeting's own facts, from the session: name, client, project,
    organiser, attendees, and the user's notes so far."""
    if session is None:
        return ""
    lines = []
    name = (getattr(session, "display_name", "") or "").strip()
    if name:
        lines.append(f"Meeting: {name}")
    client = (getattr(session, "client", "") or "").strip()
    if client:
        lines.append(f"Client: {client}")
    project = (getattr(session, "project", "") or "").strip()
    if project:
        lines.append(f"Project: {project}")
    organizer = (getattr(session, "organizer", "") or "").strip()
    if organizer:
        lines.append(f"Organiser: {organizer}")
    attendees = [str(a).strip() for a in
                 (getattr(session, "attendees", None) or []) if str(a).strip()]
    if attendees:
        shown = attendees[:MAX_ATTENDEES]
        more = len(attendees) - len(shown)
        lines.append("Invited: " + ", ".join(shown)
                     + (f" (+{more} more)" if more > 0 else ""))
    notes = (getattr(session, "notes", "") or "").strip()
    if notes:
        if len(notes) > MAX_NOTES_CHARS:
            notes = notes[:MAX_NOTES_CHARS].rstrip() + " …"
        lines.append("The user's own notes so far:\n" + notes)
    return "\n".join(lines)
