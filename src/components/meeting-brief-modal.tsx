"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type Meeting, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Loader2, Sparkles, Copy, Mic, Clock, Users, Building2 } from "lucide-react";

// One-click pre-meeting brief, opened from a calendar tile in the
// Upcoming Meetings list.
//
// Backend pipeline:
//   1. We resolve client/project from the meeting's attendees using
//      the same domain-overlap heuristic the Use button does. (Logic
//      below — kept in lockstep with suggestClientFromAttendees in
//      record-view.tsx.)
//   2. POST /prep-brief/from-meeting with the resolved scope and
//      meeting context.
//   3. Backend filters sessions to that scope, builds prior-notes
//      blob with [session_id] headers, and asks Claude for sections:
//      The story so far / Hot topics / Open commitments / Suggested
//      questions. Inline `[id]` citations are rendered as click-to-jump
//      buttons by AnswerWithCitations in this file.
//
// The "Open commitments" section will be empty until the commitments
// tracker ships — Claude will dutifully write "None." which is the
// honest answer. Once commitments lands, we'll feed open ones into
// the prompt context as a third source alongside summaries +
// action_items.

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  meeting: Meeting | null;
  // Used to resolve client/project from attendee email domains.
  // Same algorithm as record-view's useMeeting auto-tag.
  allSessions: SessionSummary[];
  // Click-through to the source session for any inline `[id]` citation
  // Claude includes in the brief.
  onOpenSession: (id: string) => void;
  // "Pin to record" hands off the resolved meeting + scope to the
  // recording form. Same effect as clicking Use on the tile.
  onUseForRecording: (meeting: Meeting, client: string, project: string) => void;
}

export function MeetingBriefModal({
  open, onOpenChange, meeting, allSessions, onOpenSession, onUseForRecording,
}: Props) {
  const [brief, setBrief] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [referenced, setReferenced] = useState<Array<{
    session_id: string;
    display_name: string;
    started_at: string | null;
  }>>([]);
  const [lastMeetingAt, setLastMeetingAt] = useState<string | null>(null);

  // Resolve client + project from attendees once the modal mounts on
  // a meeting. Same algorithm as record-view's useMeeting auto-tag,
  // ported inline so we don't import private state from the parent.
  const { client, project } = useMemo(() => {
    if (!meeting) return { client: "", project: "" };
    const c = suggestClientFromAttendees(
      meeting.attendees || [], allSessions, "",
    ) ?? "";
    // Project = the most-recent project under that client, or empty
    // if the client just got resolved. The user can still edit before
    // recording.
    let p = "";
    if (c) {
      const recentMatch = [...allSessions]
        .filter((s) => s.client === c && s.project)
        .sort((a, b) =>
          (b.started_at || "").localeCompare(a.started_at || ""))
        [0];
      p = recentMatch?.project || "";
    }
    return { client: c, project: p };
  }, [meeting, allSessions]);

  // Fetch the brief whenever the modal opens with a fresh meeting.
  useEffect(() => {
    if (!open || !meeting) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setBrief("");
    setReferenced([]);
    setLastMeetingAt(null);
    api
      .prepBriefFromMeeting({
        subject: meeting.subject,
        attendees: meeting.attendees || [],
        scheduled_start_iso: meeting.start,
        scheduled_end_iso: meeting.end,
        client,
        project,
      })
      .then((res) => {
        if (cancelled) return;
        setBrief(res.markdown || "");
        setReferenced(res.referenced_sessions || []);
        setLastMeetingAt(res.last_meeting_at);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [open, meeting, client, project]);

  const copyToClipboard = () => {
    if (!brief) return;
    navigator.clipboard.writeText(brief).then(
      () => toast.success("Brief copied to clipboard"),
      () => toast.error("Couldn't copy — your OS may be blocking it"),
    );
  };

  const useForRecording = () => {
    if (!meeting) return;
    onUseForRecording(meeting, client, project);
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl w-[95vw] h-[85vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-6 pt-5 pb-3 border-b shrink-0">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Sparkles className="h-4 w-4 text-primary" />
            Pre-meeting brief
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {meeting && (
            <MeetingHeader
              meeting={meeting}
              client={client}
              project={project}
              lastMeetingAt={lastMeetingAt}
              referencedCount={referenced.length}
            />
          )}

          {loading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
              <Loader2 className="h-4 w-4 animate-spin" />
              Reading prior meetings and writing your brief…
            </div>
          )}

          {error && !loading && (
            <p className="text-sm text-destructive italic">{error}</p>
          )}

          {!loading && !error && brief && (
            <BriefBody
              markdown={brief}
              referenced={referenced}
              onOpenSession={onOpenSession}
            />
          )}
        </div>

        <div className="border-t bg-muted/30 px-6 py-3 flex items-center gap-2 shrink-0">
          <Button
            variant="outline"
            size="sm"
            onClick={copyToClipboard}
            disabled={!brief || loading}
          >
            <Copy className="h-3.5 w-3.5 mr-1.5" />
            Copy
          </Button>
          <Button size="sm" onClick={useForRecording} disabled={!meeting}>
            <Mic className="h-3.5 w-3.5 mr-1.5" />
            Use for recording
          </Button>
          <span className="text-[10px] text-muted-foreground ml-auto italic">
            Brief generated from {referenced.length} prior session{referenced.length === 1 ? "" : "s"}.
          </span>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Meeting context header ──────────────────────────────────────────

function MeetingHeader({
  meeting, client, project, lastMeetingAt, referencedCount,
}: {
  meeting: Meeting;
  client: string;
  project: string;
  lastMeetingAt: string | null;
  referencedCount: number;
}) {
  const start = new Date(meeting.start);
  const startStr = start.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
  const lastMeetingDelta = lastMeetingAt
    ? formatRelative(new Date(lastMeetingAt))
    : null;

  return (
    <div className="rounded-lg border bg-card p-4 space-y-2">
      <div className="text-base font-semibold">{meeting.subject}</div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <Clock className="h-3 w-3" />
          {startStr} ({meeting.duration} min)
        </span>
        {(meeting.attendees?.length ?? 0) > 0 && (
          <span className="flex items-center gap-1.5">
            <Users className="h-3 w-3" />
            {meeting.attendees.length} attendee{meeting.attendees.length === 1 ? "" : "s"}
          </span>
        )}
        {client && (
          <span className="flex items-center gap-1.5">
            <Building2 className="h-3 w-3" />
            {client}{project ? ` / ${project}` : ""}
          </span>
        )}
      </div>
      {lastMeetingDelta && (
        <div className="text-[11px] text-muted-foreground pt-1">
          Last meeting in scope: {lastMeetingDelta} · Briefing on {referencedCount} prior session{referencedCount === 1 ? "" : "s"}.
        </div>
      )}
    </div>
  );
}

// ── Markdown body with click-to-jump citations ──────────────────────
//
// Same `[id]` regex pattern the Q&A view uses for click-to-jump source
// citations. We render the brief as a single <pre whitespace-pre-wrap>
// block — markdown headings come through as plain text but the
// section structure is still legible. Adding a real markdown renderer
// would be ~10 KB of dependency and the existing app uses the same
// preformatted approach throughout, so we're staying consistent.

const CITATION_RE = /\[([A-Za-z0-9]{4,16})\]/g;

function BriefBody({
  markdown, referenced, onOpenSession,
}: {
  markdown: string;
  referenced: Array<{ session_id: string; display_name: string; started_at: string | null }>;
  onOpenSession: (id: string) => void;
}) {
  // Build a quick lookup so the citation button can show the actual
  // meeting title in its tooltip.
  const byId = useMemo(() => {
    const m: Record<string, { display_name: string; started_at: string | null }> = {};
    for (const r of referenced) {
      m[r.session_id] = { display_name: r.display_name, started_at: r.started_at };
    }
    return m;
  }, [referenced]);

  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  CITATION_RE.lastIndex = 0;
  while ((match = CITATION_RE.exec(markdown)) !== null) {
    const sid = match[1];
    // Only treat it as a citation if Claude actually referenced a
    // session we know about. Otherwise it's something else in
    // brackets (e.g. the original action-item template's "[Owner]").
    if (!byId[sid]) continue;
    if (match.index > lastIdx) parts.push(markdown.slice(lastIdx, match.index));
    const meta = byId[sid];
    parts.push(
      <button
        key={`${sid}-${match.index}`}
        onClick={() => onOpenSession(sid)}
        className="inline-flex items-center px-1.5 mx-0.5 rounded bg-primary/10 hover:bg-primary/20 text-primary text-[11px] font-medium tabular-nums transition-colors align-baseline"
        title={`Open ${meta.display_name}${meta.started_at ? ` · ${(meta.started_at || "").slice(0, 10)}` : ""}`}
      >
        {sid}
      </button>
    );
    lastIdx = match.index + match[0].length;
  }
  if (lastIdx < markdown.length) parts.push(markdown.slice(lastIdx));

  return (
    <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed bg-muted/30 rounded-lg p-4">
      {parts}
    </pre>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────

function formatRelative(d: Date): string {
  const now = Date.now();
  const ms = now - d.getTime();
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} minute${min === 1 ? "" : "s"} ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"} ago`;
  const day = Math.floor(hr / 24);
  if (day < 14) return `${day} day${day === 1 ? "" : "s"} ago`;
  const wk = Math.floor(day / 7);
  return `${wk} week${wk === 1 ? "" : "s"} ago`;
}

// Ported from record-view.tsx::suggestClientFromAttendees so the modal
// resolves scope identically without reaching into the parent's state.
// Detects the user's own domain (most-frequent across all sessions),
// then scores each known client by how many domains overlap with the
// upcoming meeting. Highest-scoring client wins the auto-tag, ties
// resolve to "" (let the user pick).
function suggestClientFromAttendees(
  meetingAttendees: string[],
  allSessions: SessionSummary[],
  currentClient: string,
): string | null {
  if (currentClient.trim()) return null;
  const meetingDomains = extractDomains(meetingAttendees);
  if (meetingDomains.size === 0) return null;

  const sessionsWithDomain = new Map<string, number>();
  for (const s of allSessions) {
    const d = extractDomains(s.attendees || []);
    for (const dom of d) {
      sessionsWithDomain.set(dom, (sessionsWithDomain.get(dom) ?? 0) + 1);
    }
  }
  const ownDomain = [...sessionsWithDomain.entries()]
    .sort((a, b) => b[1] - a[1])[0]?.[0];

  const scores = new Map<string, number>();
  for (const s of allSessions) {
    if (!s.client) continue;
    const sessionDomains = extractDomains(s.attendees || []);
    let overlap = 0;
    for (const d of meetingDomains) {
      if (d === ownDomain) continue;
      if (sessionDomains.has(d)) overlap++;
    }
    if (overlap > 0) {
      scores.set(s.client, (scores.get(s.client) ?? 0) + overlap);
    }
  }
  if (scores.size === 0) return null;
  const sorted = [...scores.entries()].sort((a, b) => b[1] - a[1]);
  if (sorted.length > 1 && sorted[0][1] === sorted[1][1]) return null;
  return sorted[0][0];
}

function extractDomains(addresses: string[]): Set<string> {
  const out = new Set<string>();
  for (const a of addresses) {
    if (typeof a !== "string") continue;
    const at = a.indexOf("@");
    if (at < 0) continue;
    const domain = a.slice(at + 1).trim().toLowerCase();
    if (domain) out.add(domain);
  }
  return out;
}
