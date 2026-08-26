from __future__ import annotations
import datetime
from typing import Dict, List, Optional
from models.speaker import Speaker
from models.segment import Segment
from models.extraction import (
    Requirement,
    Decision,
    ActionItem,
    OpenQuestion,
)


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
        # PROVENANCE for `client`. Set when the backend resolved the
        # client itself at record-start (services/client_resolution_
        # service.py) rather than the user typing it. Values:
        #   None            — the user set (or cleared) it by hand, or
        #                     the session predates auto-tagging.
        #   "subject_match" — the client's name was in the meeting title.
        #   "domain_history"— attendee email domains matched a client the
        #                     user has tagged before.
        #   "ambiguous"     — two different clients matched the title, so
        #                     NOTHING was tagged. `client` stays "".
        #   "none"          — resolution ran and found nothing.
        # The last two exist so the UI can explain why an auto-recorded
        # session came back untagged instead of looking broken.
        # `client_source_detail` is the human-readable why, shown next to
        # the client field — an auto-applied value with no explanation is
        # one the user has to re-verify by hand.
        self.client_source: Optional[str] = None
        self.client_source_detail: Optional[str] = None
        self.attendees: List[str] = []
        # WHO CALLED THE MEETING, from the calendar invite. A display
        # name ("Jane Doe", or the surname-first "Doe, Jane [EMEA]"
        # form Outlook produces) or an address — whatever the calendar
        # source hands over, passed through unparsed, exactly like the
        # entries in `attendees`. core/speaker_roster.py owns the
        # reading of both shapes.
        #
        # This field exists because `attendees` is ALWAYS EMPTY for one
        # whole class of user. The Chrome-extension calendar source
        # scrapes Outlook Web's grid, and the grid's aria-label carries
        # the organiser but not the attendee list (that lives in the
        # event detail pane, one click deeper — the same reason join
        # links have to be resolved separately). So a user whose entire
        # calendar is extension-sourced gets `attendees == []` on every
        # session, and the v2.40.0 speaker roster — built purely from
        # `attendees` — is inert for them. The organiser is the one
        # invite-derived name that path CAN supply, and
        # `speaker_roster.roster_names()` already accepts it and puts
        # it first (they're the likeliest person to be both present and
        # speaking).
        #
        # Set at record start from the calendar event, on BOTH paths —
        # server._auto_record_start and the Record tab's Use button.
        # Always a string: "" means "no organiser was resolved", a
        # normal non-error state that degrades to exactly the
        # pre-organiser behaviour (roster_names drops the empty entry).
        # Absent from every session written before this field existed,
        # which from_dict reads as "" rather than failing.
        self.organizer: str = ""
        self.decisions: Optional[str] = None
        # Structured counterparts to the markdown fields above. The
        # markdown stays the source of truth for the current per-session
        # UI; these typed records exist so an engagement (many sessions
        # for one client/project) can roll up, dedupe, and status-track.
        # Empty on legacy sessions until they're reprocessed — nothing
        # reads these as required, so old JSON loads unchanged.
        self.requirements_struct: List[Requirement] = []
        self.decisions_struct: List[Decision] = []
        self.action_items_struct: List[ActionItem] = []
        self.open_questions: List[OpenQuestion] = []
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
        # Absolute paths to screenshots the user captured during the
        # meeting. Stored alongside the recording so they can be fed to
        # the summarizer as visual context and reused later like any
        # other artifact.
        self.screenshots: List[str] = []
        # Fingerprint of the inputs the last successful extraction ran
        # on: transcript + notes + template + the extractor prompt
        # version. Reprocessing a session whose fingerprint is unchanged
        # would re-spend five LLM calls to produce byte-identical
        # output. The August 2026 token export made the cost concrete —
        # the reprocessing days ran 4-8x a normal day's spend, almost
        # all of it re-extracting sessions nothing had changed about.
        # Empty on every session recorded before this existed, which
        # correctly reads as "unknown, so run it".
        self.extraction_fingerprint: str = ""
        # Saved Live Co-Pilot ticks. Each entry is the dict the tick
        # endpoint returned — `generated_at`, `segment_count`,
        # `clarifying_questions`, `risks`, `follow_ups`. Kept with the
        # session so the bullets the model produced mid-call survive
        # past the recording. Treated as opaque dicts (rather than a
        # typed model) because the prompt schema may evolve before the
        # feature leaves beta.
        self.copilot_ticks: List[Dict] = []
        # Audio integrity: set at stop_recording when the actual WAV
        # duration significantly disagrees with (ended_at - started_at).
        # Causes include process collisions, OneDrive sync truncation,
        # mic device hand-off mid-recording, or any silent capture
        # failure. When set, the Sessions list surfaces a warning so the
        # user doesn't trust an incomplete recording. None = healthy.
        # The actual + expected durations (seconds) are stored so the
        # UI can show a precise "you got 30 min out of 60" message.
        self.audio_integrity_warning: Optional[str] = None
        self.audio_actual_duration_s: Optional[float] = None
        self.audio_expected_duration_s: Optional[float] = None
        # Auto-process outcome. Set when backend auto-processing exhausts
        # its retries so the failure is VISIBLE (the Sessions list badges
        # it) instead of the session silently sitting unprocessed. Holds a
        # short human-readable reason ("Claude rate-limited after 3
        # retries", "Ollama unreachable", ...). Cleared on any successful
        # process. None = no failure recorded.
        self.processing_error: Optional[str] = None
        # Crash-resilient auto-process marker. Stamped to DISK when
        # backend auto-processing starts, cleared on success/permanent
        # failure. If the backend dies mid-processing (the Windows
        # 0xC0000005 segfault class killed the process mid-transcribe,
        # so no exception handler ever ran), the startup resume pass
        # finds this marker and re-queues the session — previously it
        # just sat unprocessed forever after the UI said "Transcribing…".
        # Shape: {"resumes": int, "template": str, "follow_up": bool,
        # "started_at": iso}. None = nothing pending.
        self.auto_process_pending: Optional[dict] = None
        # Read-only sync-integrity finding from stop_recording: set when a
        # capture stream fell meaningfully behind wall-clock (dropped
        # frames / clock drift) or the mic + system-audio tracks diverged.
        # Measurement only — no audio is altered. Surfaced as a Sessions
        # chip so drift is visible; informs whether the heavier timestamp-
        # anchoring correction is worth building. None = clean.
        self.sync_warning: Optional[str] = None
        # How long the post-stop finalize subprocess (WAV merge, optional
        # AEC, resample) took, in seconds. Deliberately SEPARATE from
        # ``ended_at - started_at`` (the capture window) — folding this
        # into the recording window is exactly the bug that made a slow
        # AEC-enabled finalize (e.g. 278s) look like ~5 minutes of lost
        # audio: ``ended_at`` used to be stamped AFTER finalize returned,
        # so `expected_s` silently included the entire finalize duration.
        # ``ended_at`` is now stamped when capture actually stops, BEFORE
        # finalize is spawned; this field is where finalize's own cost
        # goes instead, so it stays visible without corrupting the
        # AUDIO_INTEGRITY / SYNC_INTEGRITY capture-window math. None =
        # finalize hasn't completed (or predates this field).
        self.finalize_duration_s: Optional[float] = None
        # Outcome of the offline AEC decision (utils/aec.py) for this
        # session's finalize, persisted so Settings.echo_cancellation_
        # enabled can be judged on real field numbers instead of staying
        # off forever "until there's evidence." Shape:
        #   {"requested": bool,
        #    "accepted": Optional[bool],   # None only in the "no
        #                                  # decision came back" case
        #    "reason": Optional[str],
        #    "erle_db": Optional[float],
        #    "residual_delay_ms": Optional[float]}
        # `requested=False` means the toggle was off — nothing ran.
        # `requested=True, accepted=None` means AEC was asked for but no
        # decision was recoverable (child crashed, or the subprocess
        # succeeded but didn't report one) — this is NOT the same as a
        # rejection and must never be displayed as one; see the finalize-
        # subprocess handling in recording_service.py for how each case
        # is produced. None = predates this field.
        self.aec_outcome: Optional[dict] = None
        # FINALIZE-IN-PROGRESS STATE (field repro 2026-08-14): before this
        # field existed, nothing distinguished "audio is still being
        # written by the finalize subprocess" from "audio is gone" — a
        # user who clicked Process 36s into a 192s AEC-enabled finalize
        # got told the WAV "may have been moved, deleted, or not yet
        # synced down from the cloud", all three of which were false. The
        # file didn't exist YET; no data was lost.
        #
        # Four states, explicit and persisted so they survive a backend
        # restart (see services/recovery_service.py's startup orphan
        # scan, which must resolve a crash-interrupted "finalizing" OR
        # "queued" session rather than leave it stuck forever):
        #   None         — no finalize in flight (either none has run
        #                  yet, or the last one succeeded).
        #   "queued"     — this session's finalize is waiting behind
        #                  ANOTHER finalize currently holding the
        #                  process-wide slot (see utils/finalize_gate.py
        #                  — at most one finalize subprocess runs at a
        #                  time, process-wide, so it can never outrank a
        #                  still-live recording for CPU). Set from
        #                  recording_service.stop_recording()'s
        #                  ``_mark_queued`` callback the moment the gate
        #                  is found contended; flips to "finalizing" the
        #                  instant the slot is actually acquired.
        #   "finalizing" — the finalize subprocess (WAV merge, optional
        #                  AEC, resample) is currently running (i.e. it
        #                  holds the process-wide gate). Set in
        #                  recording_service.stop_recording() BEFORE the
        #                  subprocess is spawned (and written to disk via
        #                  _write_session_stub before the blocking call),
        #                  cleared back to None the moment the subprocess
        #                  returns successfully.
        #   "failed"     — the finalize subprocess raised or crashed (or
        #                  the backend restarted while this session was
        #                  "finalizing"/"queued" with nothing recoverable
        #                  left on disk); ``finalize_error`` holds the
        #                  reason. Distinct from "genuinely missing
        #                  audio" — this is a known, explainable failure,
        #                  not silence.
        self.finalize_status: Optional[str] = None
        # Wall-clock time finalize started, so an in-flight check (e.g.
        # /sessions/{id}/process) can report how long it's been running.
        # Cleared alongside finalize_status on success; kept on failure so
        # the failure message can still say how long it ran before it
        # died (finalize_duration_s is the authoritative number for that,
        # but this is the raw timestamp it was computed from).
        # Whether echo cancellation is part of the finalize currently
        # running. Set before the finalize stub is written, so a UI
        # reading a session mid-finalize can describe what is actually
        # happening. `aec_outcome` cannot serve this: it exists only
        # after finalize returns, which is precisely when the progress
        # banner is no longer shown.
        self.finalize_aec_requested: bool = False
        self.finalize_started_at: Optional[datetime.datetime] = None
        # Human-readable reason the finalize subprocess failed. Only set
        # when finalize_status == "failed"; None otherwise.
        self.finalize_error: Optional[str] = None

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
            "client_source": self.client_source,
            "client_source_detail": self.client_source_detail,
            "attendees": self.attendees,
            "organizer": self.organizer,
            "decisions": self.decisions,
            "requirements_struct": [r.to_dict() for r in self.requirements_struct],
            "decisions_struct": [d.to_dict() for d in self.decisions_struct],
            "action_items_struct": [a.to_dict() for a in self.action_items_struct],
            "open_questions": [q.to_dict() for q in self.open_questions],
            "notes": self.notes,
            "exported_audio_paths": list(self.exported_audio_paths),
            "screenshots": list(self.screenshots),
            "copilot_ticks": list(self.copilot_ticks),
            "audio_integrity_warning": self.audio_integrity_warning,
            "audio_actual_duration_s": self.audio_actual_duration_s,
            "audio_expected_duration_s": self.audio_expected_duration_s,
            "processing_error": self.processing_error,
            "auto_process_pending": self.auto_process_pending,
            "sync_warning": self.sync_warning,
            "finalize_duration_s": self.finalize_duration_s,
            "aec_outcome": self.aec_outcome,
            "finalize_status": self.finalize_status,
            "finalize_aec_requested": self.finalize_aec_requested,
            "finalize_started_at": (
                self.finalize_started_at.isoformat()
                if self.finalize_started_at else None
            ),
            "finalize_error": self.finalize_error,
            "extraction_fingerprint": self.extraction_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Reconstruct a Session from its JSON dict."""
        session = cls(session_id=data.get("session_id", ""))
        session.display_name = data.get("display_name", "") or ""
        session.extraction_fingerprint = str(
            data.get("extraction_fingerprint") or "")
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
        # Absent on every session written before auto-tagging existed —
        # a missing key reads as "the user set this by hand", which is
        # exactly right for those.
        session.client_source = data.get("client_source") or None
        session.client_source_detail = data.get("client_source_detail") or None
        session.attendees = list(data.get("attendees") or [])
        # Missing on every session written before the organiser was
        # carried through, and on every ad-hoc recording that was never
        # started from a calendar entry. Both read as "" — the same
        # value a resolved-but-empty organiser produces — so a legacy
        # session loads unchanged rather than raising.
        session.organizer = str(data.get("organizer") or "")
        session.decisions = data.get("decisions")
        session.requirements_struct = [
            Requirement.from_dict(x) for x in (data.get("requirements_struct") or [])
        ]
        session.decisions_struct = [
            Decision.from_dict(x) for x in (data.get("decisions_struct") or [])
        ]
        session.action_items_struct = [
            ActionItem.from_dict(x) for x in (data.get("action_items_struct") or [])
        ]
        session.open_questions = [
            OpenQuestion.from_dict(x) for x in (data.get("open_questions") or [])
        ]
        session.notes = data.get("notes") or ""
        session.exported_audio_paths = list(data.get("exported_audio_paths") or [])
        session.screenshots = list(data.get("screenshots") or [])
        session.copilot_ticks = list(data.get("copilot_ticks") or [])
        session.audio_integrity_warning = data.get("audio_integrity_warning") or None
        session.audio_actual_duration_s = data.get("audio_actual_duration_s")
        session.audio_expected_duration_s = data.get("audio_expected_duration_s")
        session.processing_error = data.get("processing_error") or None
        session.auto_process_pending = data.get("auto_process_pending") or None
        session.sync_warning = data.get("sync_warning") or None
        session.finalize_duration_s = data.get("finalize_duration_s")
        session.aec_outcome = data.get("aec_outcome") or None
        session.finalize_status = data.get("finalize_status")
        # Absent on every session written before this field existed,
        # which reads as False — i.e. "do not claim AEC is running".
        # The safe direction: a banner that under-claims is a cosmetic
        # miss, one that over-claims is what produced the bug report.
        session.finalize_aec_requested = bool(
            data.get("finalize_aec_requested") or False) or None
        finalize_started = data.get("finalize_started_at")
        if finalize_started:
            try:
                session.finalize_started_at = datetime.datetime.fromisoformat(
                    finalize_started)
            except ValueError:
                pass
        session.finalize_error = data.get("finalize_error") or None

        # Rebuild speakers
        speakers_data = data.get("speakers") or {}
        for speaker_id, sdata in speakers_data.items():
            sp = Speaker(
                speaker_id=sdata.get("speaker_id", speaker_id),
                display_name=sdata.get("display_name", "") or "",
                profile_id=sdata.get("profile_id"),
                match_confidence=sdata.get("match_confidence"),
                match_confirmed=bool(sdata.get("match_confirmed", False)),
                embedding=list(sdata.get("embedding") or []),
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
