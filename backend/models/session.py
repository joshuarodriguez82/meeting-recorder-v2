from __future__ import annotations
import datetime
from typing import Dict, List, Optional
from models.speaker import Speaker
from models.segment import Segment


class Session:

    def __init__(self, session_id: str):
        self.session_id: str = session_id
        self.display_name: str = ""
        self.started_at: datetime.datetime = datetime.datetime.now()
        self.ended_at: Optional[datetime.datetime] = None
        self.audio_path: Optional[str] = None
        self.speakers: Dict[str, Speaker] = {}
        self.segments: List[Segment] = []
        self.summary: Optional[str] = None
        self.action_items: Optional[str] = None
        self.requirements: Optional[str] = None
        self.template: str = "General"
        self.client: str = ""
        self.project: str = ""
        self.attendees: List[str] = []
        self.decisions: Optional[str] = None
        # Free-form notes the user adds to the session — personal reminders,
        # off-audio context, follow-ups they want to remember. Fed into the
        # summarizer prompt so AI extractions reflect the user's own
        # context, not just the transcript.
        self.notes: str = ""
        # Copies of the audio file that live outside recordings_dir (e.g.
        # the WAV we auto-copy into a client's Designated Folder on stop).
        # Tracked here so retention can clean them up alongside the main
        # file — without this list the copies would stick around forever.
        self.exported_audio_paths: List[str] = []

    def get_or_create_speaker(self, speaker_id: str) -> Speaker:
        if speaker_id not in self.speakers:
            self.speakers[speaker_id] = Speaker(speaker_id=speaker_id)
        return self.speakers[speaker_id]

    def rename_speaker(self, speaker_id: str, name: str) -> None:
        if speaker_id in self.speakers:
            self.speakers[speaker_id].display_name = name

    def full_transcript(self) -> str:
        if not self.segments:
            return ""
        lines = []
        for seg in self.segments:
            speaker = self.speakers.get(seg.speaker_id)
            name = speaker.display_name if speaker else seg.speaker_id
            start = _fmt_time(seg.start)
            end = _fmt_time(seg.end)
            lines.append(f"[{start} → {end}] {name}: {seg.text}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "display_name": self.display_name,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "audio_path": self.audio_path,
            "speakers": {k: v.to_dict() for k, v in self.speakers.items()},
            "segments": [s.to_dict() for s in self.segments],
            "summary": self.summary,
            "action_items": self.action_items,
            "requirements": self.requirements,
            "template": self.template,
            "client": self.client,
            "project": self.project,
            "attendees": self.attendees,
            "decisions": self.decisions,
            "notes": self.notes,
            "exported_audio_paths": list(self.exported_audio_paths),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Reconstruct a Session from its JSON dict."""
        session = cls(session_id=data.get("session_id", ""))
        session.display_name = data.get("display_name", "") or ""
        started = data.get("started_at")
        ended = data.get("ended_at")
        if started:
            try:
                session.started_at = datetime.datetime.fromisoformat(started)
            except ValueError:
                pass
        if ended:
            try:
                session.ended_at = datetime.datetime.fromisoformat(ended)
            except ValueError:
                pass
        session.audio_path = data.get("audio_path")
        session.summary = data.get("summary")
        session.action_items = data.get("action_items")
        session.requirements = data.get("requirements")
        session.template = data.get("template", "General") or "General"
        session.client = data.get("client", "") or ""
        session.project = data.get("project", "") or ""
        session.attendees = list(data.get("attendees") or [])
        session.decisions = data.get("decisions")
        session.notes = data.get("notes") or ""
        session.exported_audio_paths = list(data.get("exported_audio_paths") or [])

        # Rebuild speakers
        speakers_data = data.get("speakers") or {}
        for speaker_id, sdata in speakers_data.items():
            sp = Speaker(
                speaker_id=sdata.get("speaker_id", speaker_id),
                display_name=sdata.get("display_name", "") or "",
            )
            session.speakers[speaker_id] = sp

        # Rebuild segments
        for seg_data in data.get("segments") or []:
            session.segments.append(Segment(
                speaker_id=seg_data.get("speaker_id", "SPEAKER_UNKNOWN"),
                start=float(seg_data.get("start", 0.0)),
                end=float(seg_data.get("end", 0.0)),
                text=seg_data.get("text", "") or "",
            ))
        return session


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"
