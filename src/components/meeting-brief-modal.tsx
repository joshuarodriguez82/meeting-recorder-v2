"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api, type Meeting, type ReferencedDocument, type SessionSummary,
} from "@/lib/api";
import { toast } from "sonner";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Loader2, Sparkles, Copy, Mic, Clock, Users, Building2, RefreshCw,
  FileText,
} from "lucide-react";

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
//   4. The backend ALSO semantically retrieves excerpts from this
//      client's Knowledge Folder (SOWs, requirements, notes) and hands
//      them to Claude under their own header, cited inline as
//      `[DOC: <file name>]`. Those render below as document chips —
//      deliberately a different affordance from a session citation,
//      because "the SOW says the cutover is in October" and "they said
//      on the last call the cutover is in October" are different claims
//      with different authority. `referenced_documents` is the document
//      equivalent of `referenced_sessions`; it comes back empty for a
//      client with no Knowledge Folder, and everything below then
//      renders exactly as it did before.
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
  const [referencedDocs, setReferencedDocs] =
    useState<ReferencedDocument[]>([]);
  const [lastMeetingAt, setLastMeetingAt] = useState<string | null>(null);
  // Free-text the user types in to feed the LLM context the invite +
  // meeting history can't see (exec asks, recent emails, customer mood,
  // procurement redlines just received). Empty on initial generation
  // so first-load is one-click; clicking "Regenerate with context"
  // re-runs the brief with whatever's in the box.
  const [userContext, setUserContext] = useState("");

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

  // Generate (or regenerate) the brief. Extracted so the "Regenerate
  // with context" button can re-run after the user types into the
  // user-context box. extraContext defaults to the current state value
  // so the initial open-time generation runs without it.
  const generateBrief = async (extraContext: string = "") => {
    if (!meeting) return;
    setLoading(true);
    setError(null);
    // Pull the invite agenda/body first so Claude can tailor the
    // brief to what THIS meeting is actually about. Best-effort —
    // if it fails (no body, calendar hiccup), the brief still
    // generates from prior-meeting context exactly as before.
    let body = "";
    try {
      const d = await api.getMeetingDetail(meeting.subject, meeting.start);
      body = d.body || "";
    } catch {
      /* agenda is a bonus, not required */
    }
    try {
      const res = await api.prepBriefFromMeeting({
        subject: meeting.subject,
        attendees: meeting.attendees || [],
        scheduled_start_iso: meeting.start,
        scheduled_end_iso: meeting.end,
        client,
        project,
        body,
        user_context: extraContext,
      });
      setBrief(res.markdown || "");
      setReferenced(res.referenced_sessions || []);
      setReferencedDocs(res.referenced_documents || []);
      setLastMeetingAt(res.last_meeting_at);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  // Fetch the brief whenever the modal opens with a fresh meeting.
  // First open is always empty user_context — keeps the modal one-click.
  useEffect(() => {
    if (!open || !meeting) return;
    setBrief("");
    setReferenced([]);
    setReferencedDocs([]);
    setLastMeetingAt(null);
    setUserContext("");
    void generateBrief("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
              documentCount={referencedDocs.length}
            />
          )}

          {/* User-context box — visible on open (not hidden behind the
              first generation) so the SA can drop in what the invite +
              meeting history can't see BEFORE re-running the brief.
              The first brief still auto-generates with empty context
              on open so the modal stays one-click for the common
              case; the box gives a discoverable second pass. Same
              pattern as the Prep Brief tab. */}
          <div className="space-y-2 rounded-lg border bg-muted/20 p-3">
            <div className="flex items-baseline justify-between gap-2 flex-wrap">
              <label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Add context
              </label>
              <span className="text-[10px] text-muted-foreground italic">
                Optional — things the invite & meeting history can&apos;t see
              </span>
            </div>
            <Textarea
              value={userContext}
              onChange={(e) => setUserContext(e.target.value)}
              placeholder="e.g. Customer's CFO just joined this engagement and wants a 60-day plan. Procurement flagged the SLA section yesterday. Focus on the API integration timeline."
              rows={3}
              className="resize-y text-sm bg-background"
              disabled={loading}
            />
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => void generateBrief(userContext)}
                disabled={loading || !userContext.trim()}
              >
                <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${loading ? "animate-spin" : ""}`} />
                {brief ? "Regenerate with context" : "Generate with context"}
              </Button>
              {userContext.trim() && (
                <button
                  type="button"
                  onClick={() => setUserContext("")}
                  disabled={loading}
                  className="text-[11px] text-muted-foreground hover:text-foreground transition-colors"
                >
                  Clear
                </button>
              )}
            </div>
          </div>

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
            <>
              <BriefBody
                markdown={brief}
                referenced={referenced}
                onOpenSession={onOpenSession}
              />
              <SourceDocuments documents={referencedDocs} />
            </>
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
            Brief generated from {referenced.length} prior session{referenced.length === 1 ? "" : "s"}
            {referencedDocs.length > 0
              ? ` and ${referencedDocs.length} knowledge-folder document${referencedDocs.length === 1 ? "" : "s"}`
              : ""}.
          </span>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Meeting context header ──────────────────────────────────────────

function MeetingHeader({
  meeting, client, project, lastMeetingAt, referencedCount, documentCount,
}: {
  meeting: Meeting;
  client: string;
  project: string;
  lastMeetingAt: string | null;
  referencedCount: number;
  documentCount: number;
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
          Last meeting in scope: {lastMeetingDelta} · Briefing on {referencedCount} prior session{referencedCount === 1 ? "" : "s"}
          {documentCount > 0
            ? ` + ${documentCount} knowledge-folder document${documentCount === 1 ? "" : "s"}`
            : ""}.
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
//
// Two citation forms, rendered two different ways on purpose:
//
//   [ABC123]              a prior meeting     → clickable, jumps to it
//   [DOC: ACME SOW.docx]  a knowledge-folder  → file chip, names the
//                         document              document, not clickable
//                                               (there's no timestamp
//                                               to jump to)
//
// The visual split is the point: a claim sourced from a signed SOW and
// a claim sourced from something someone said on a call carry very
// different authority, and the reader has to be able to tell which is
// which without re-reading the sentence.

const CITATION_RE = /\[DOC:\s*([^\]]{1,120})\]|\[([A-Za-z0-9]{4,16})\]/g;

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
    const docName = match[1];
    const sid = match[2];
    // `[DOC: …]` is unambiguous syntax the prompt asks for explicitly,
    // so it renders as a document chip whether or not the exact file
    // name round-trips through referenced_documents.
    if (docName === undefined) {
      // Only treat it as a session citation if Claude actually
      // referenced a session we know about. Otherwise it's something
      // else in brackets (e.g. the action-item template's "[Owner]").
      if (!byId[sid]) continue;
    }
    if (match.index > lastIdx) parts.push(markdown.slice(lastIdx, match.index));
    if (docName !== undefined) {
      const name = docName.trim();
      parts.push(
        <span
          key={`doc-${name}-${match.index}`}
          className="inline-flex items-center gap-1 px-1.5 mx-0.5 rounded bg-amber-500/10 text-amber-700 dark:text-amber-400 text-[11px] font-medium transition-colors align-baseline"
          title={`From the client's Knowledge Folder: ${name}`}
        >
          <FileText className="h-2.5 w-2.5 shrink-0" />
          {name}
        </span>
      );
    } else {
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
    }
    lastIdx = match.index + match[0].length;
  }
  if (lastIdx < markdown.length) parts.push(markdown.slice(lastIdx));

  return (
    <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed bg-muted/30 rounded-lg p-4">
      {parts}
    </pre>
  );
}

// ── Knowledge-Folder provenance ─────────────────────────────────────
//
// The document counterpart of the "briefing on N prior sessions" line.
// Renders nothing at all when the client has no Knowledge Folder, so a
// client without one sees exactly what they saw before — no empty
// section, no "no documents found".

function SourceDocuments({ documents }: { documents: ReferencedDocument[] }) {
  if (documents.length === 0) return null;
  return (
    <div className="rounded-lg border bg-muted/20 p-3 space-y-2">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        Knowledge-folder documents used
      </div>
      <ul className="space-y-1">
        {documents.map((d) => (
          <li
            key={d.doc_path || d.doc_name}
            className="flex items-center gap-2 text-xs text-muted-foreground"
            title={d.doc_path || d.doc_name}
          >
            <FileText className="h-3 w-3 shrink-0 text-amber-600 dark:text-amber-400" />
            <span className="flex-1 truncate">{d.doc_name}</span>
            <span className="shrink-0 text-[10px] tabular-nums">
              {d.chunk_count} excerpt{d.chunk_count === 1 ? "" : "s"}
            </span>
          </li>
        ))}
      </ul>
    </div>
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
