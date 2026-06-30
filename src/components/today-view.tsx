"use client";

/**
 * Today view — daily briefing dashboard.
 *
 * The user runs a Microsoft 365 Copilot scheduled prompt every morning
 * that produces a free-form briefing (top priority, today's agenda,
 * items needing response, FYI). M365 Copilot has no API surface for
 * scheduled-prompt output, so the integration is intentionally manual:
 * user clicks "Import briefing" → pastes Copilot output → backend
 * LLM-parses to structured JSON → this view renders it.
 *
 * Mirrors design/v2.10-today-mockup.html. Re-importing the same date
 * preserves any action items already checked off this morning.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  api,
  ApiError,
  type DailyBriefing,
  type BriefingAgendaItem,
  type RecordingStatus,
} from "@/lib/api";
import {
  Card, CardContent, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  Loader2, ClipboardPaste, RefreshCw, CheckCircle2, Circle,
  Square, CheckSquare, Calendar as CalendarIcon, Mic,
  AlertCircle, Sparkles, Info, X, CloudDownload, LogIn,
} from "lucide-react";

// Tailwind class fragments for meeting-type colors. Mirrors the token
// system in v2.10-today-mockup.html — light surface + darker text.
const TYPE_CLS: Record<string, string> = {
  discovery: "bg-cyan-50 text-cyan-800 border-cyan-200",
  sow: "bg-rose-50 text-rose-800 border-rose-200",
  status: "bg-green-50 text-green-800 border-green-200",
  technical: "bg-violet-50 text-violet-800 border-violet-200",
  demo: "bg-orange-50 text-orange-800 border-orange-200",
  internal: "bg-slate-50 text-slate-800 border-slate-200",
  general: "bg-zinc-50 text-zinc-700 border-zinc-200",
};

const ROLE_CLS: Record<string, string> = {
  host: "bg-blue-50 text-blue-800 border-blue-200",
  attendee: "bg-zinc-50 text-zinc-700 border-zinc-200",
  optional: "bg-zinc-50 text-zinc-500 border-zinc-200",
};

function timeOfDay(): string {
  const h = new Date().getHours();
  if (h < 12) return "morning";
  if (h < 18) return "afternoon";
  return "evening";
}

function todayPretty(): string {
  // "Wednesday, May 27"
  return new Date().toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric",
  });
}

function timeNow(): string {
  return new Date().toLocaleTimeString(undefined, {
    hour: "numeric", minute: "2-digit",
  });
}

interface Props {
  onNavigate?: (id: string) => void;
}

export function TodayView({ onNavigate }: Props) {
  const [briefing, setBriefing] = useState<DailyBriefing | null>(null);
  const [loading, setLoading] = useState(true);
  const [importOpen, setImportOpen] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [recordingStatus, setRecordingStatus] = useState<RecordingStatus | null>(null);
  const [nowTick, setNowTick] = useState(0);
  // Outlook Web sync state. `syncing` covers the headless scrape +
  // LLM parse round-trip (typically ~10-25s end-to-end). `signingIn`
  // covers the BLOCKING headed-Chrome window — the backend doesn't
  // return until the user closes it, so this stays true potentially
  // for minutes. `authExpired` is the "session cookies are stale,
  // user needs to re-MFA" signal — surfaced after a Sync Now hits 423.
  const [syncing, setSyncing] = useState(false);
  const [signingIn, setSigningIn] = useState(false);
  const [authExpired, setAuthExpired] = useState(false);

  // Re-render clock every 30s so "Right now" / time-of-day greeting
  // stay fresh without the data-fetch overhead.
  useEffect(() => {
    const t = setInterval(() => setNowTick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);

  const refreshBriefing = useCallback(async () => {
    try {
      const data = await api.getTodayBriefing();
      // Backend returns {} when no briefing exists for today.
      if (data && typeof data === "object" && "date" in (data as object)) {
        setBriefing(data as DailyBriefing);
      } else {
        setBriefing(null);
      }
    } catch (e) {
      console.warn("Briefing fetch failed", e);
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshRecording = useCallback(async () => {
    try {
      const s = await api.recordingStatus();
      setRecordingStatus(s);
    } catch {
      /* recording status is optional context — silent fail */
    }
  }, []);

  useEffect(() => {
    refreshBriefing();
    refreshRecording();
    const t = setInterval(refreshRecording, 5_000);
    return () => clearInterval(t);
  }, [refreshBriefing, refreshRecording]);

  // The dialog used to auto-pull the system clipboard on open, intending
  // to save the user a Ctrl+V if they'd just copied from M365 Copilot.
  // But it silently dumped WHATEVER was on the clipboard (a stray gh
  // command, a code snippet, a URL — anything copied recently) into the
  // textarea, which is the kind of surprising side-effect that costs
  // trust. Removed. Users paste with Ctrl+V like every other app.

  const handleImport = async () => {
    const txt = pasteText.trim();
    if (!txt) {
      setImportError("Paste the briefing text first.");
      return;
    }
    setImporting(true);
    setImportError(null);
    try {
      const parsed = await api.importBriefing(txt);
      setBriefing(parsed);
      setImportOpen(false);
      setPasteText("");
      toast.success("Briefing imported");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setImportError(msg);
      toast.error("Import failed");
    } finally {
      setImporting(false);
    }
  };

  const handleSyncFromOutlook = useCallback(async () => {
    if (syncing || signingIn) return;
    setSyncing(true);
    try {
      const result = await api.syncBriefingFromOutlookWeb();
      setBriefing(result);
      setAuthExpired(false);
      toast.success("Briefing synced from Outlook");
    } catch (e: unknown) {
      if (e instanceof ApiError && e.status === 423) {
        // 423 LOCKED = session cookies are stale. Surface the banner
        // rather than a transient toast so the user can act on it.
        setAuthExpired(true);
        toast.error("Microsoft 365 session expired — sign in again");
      } else if (e instanceof ApiError && e.status === 503) {
        toast.error(
          e.message || "Sync unavailable — is Chrome installed?",
          { duration: 8000 });
      } else {
        const msg = e instanceof Error ? e.message : String(e);
        toast.error(`Sync failed: ${msg}`, { duration: 8000 });
      }
    } finally {
      setSyncing(false);
    }
  }, [syncing, signingIn]);

  const handleSignInToOutlook = useCallback(async () => {
    if (signingIn || syncing) return;
    setSigningIn(true);
    // Show a stable toast so the user knows what to do — the request
    // BLOCKS until the Chrome window closes, which could be a couple
    // minutes if they're chasing down the Authenticator app.
    const t = toast.loading(
      "Chrome window opening — sign in, then close the window",
      { duration: Infinity });
    try {
      await api.signInToOutlookWeb();
      setAuthExpired(false);
      // Dismiss the loading toast (which has duration: Infinity) and
      // show a fresh success so the success doesn't inherit infinity.
      // sonner merges options on update; passing only {id} on success
      // keeps the original Infinity duration so the toast never goes
      // away — that's the "signed-in notification stuck on screen" bug.
      toast.dismiss(t);
      toast.success("Signed in. Click Sync now to pull today's brief.",
                     { duration: 5000 });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.dismiss(t);
      toast.error(`Sign-in failed: ${msg}`, { duration: 8000 });
    } finally {
      setSigningIn(false);
    }
  }, [syncing, signingIn]);

  const toggleAction = async (actionId: string, currentlyDone: boolean) => {
    if (!briefing) return;
    // Optimistic update — flip locally first, roll back on error.
    const previous = briefing;
    setBriefing({
      ...briefing,
      needs_response: briefing.needs_response.map((a) =>
        a.id === actionId
          ? { ...a, done_at: currentlyDone ? null : new Date().toISOString() }
          : a,
      ),
    });
    try {
      const updated = await api.setBriefingActionDone(
        briefing.date, actionId, !currentlyDone);
      setBriefing(updated);
    } catch (e: unknown) {
      setBriefing(previous);
      toast.error("Couldn't update — try again");
    }
  };

  // ──────────────────────────────────────────────────────────────────
  // Derived UI state
  // ──────────────────────────────────────────────────────────────────

  const openActions = useMemo(
    () => (briefing?.needs_response || []).filter((a) => !a.done_at),
    [briefing],
  );
  const doneActions = useMemo(
    () => (briefing?.needs_response || []).filter((a) => !!a.done_at),
    [briefing],
  );

  const importedAtPretty = useMemo(() => {
    if (!briefing?.imported_at) return "";
    try {
      const d = new Date(briefing.imported_at);
      return d.toLocaleTimeString(undefined, {
        hour: "numeric", minute: "2-digit",
      });
    } catch {
      return "";
    }
  }, [briefing]);

  // Suppress unused-tick warning — used implicitly for re-render cadence.
  void nowTick;

  // ──────────────────────────────────────────────────────────────────
  // Rendering
  // ──────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Loading today
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Greeting header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">
            Good {timeOfDay()}, Joshua
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {todayPretty()} · {timeNow()}
            {briefing && (
              <>
                {" · "}
                <span className="text-green-700">
                  Briefing imported {importedAtPretty}
                </span>
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap justify-end">
          {/* v1.1 of the Chrome extension is the canonical data path
              now — Sync Now + Sign in to Microsoft buttons that drove
              the doomed Playwright path (services/outlook_web_scraper)
              are gone. The extension's toolbar icon → Capture & Send
              replaces them. Import briefing stays for the manual
              fallback paste flow. */}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setImportOpen(true)}
            className="gap-2"
          >
            <ClipboardPaste className="h-4 w-4" />
            {briefing ? "Re-import briefing" : "Import briefing"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={refreshBriefing}
            title="Refresh"
          >
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {authExpired && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 flex items-start gap-3">
          <AlertCircle className="h-5 w-5 mt-0.5 shrink-0" />
          <div className="flex-1">
            <div className="font-medium">
              Microsoft 365 sign-in expired
            </div>
            <div className="mt-0.5 text-amber-800">
              The persistent Chrome profile's session cookies are no longer
              valid. Sign in again and the next Sync now will succeed.
            </div>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={handleSignInToOutlook}
            disabled={signingIn || syncing}
            className="bg-white"
          >
            Sign in
          </Button>
        </div>
      )}

      {!briefing && <EmptyState onImport={() => setImportOpen(true)} />}

      {briefing && (
        <>
          {/* Hero row: Top Priority + Right Now */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="lg:col-span-2">
              <TopPriorityCard top={briefing.top_priority} />
            </div>
            <div>
              <RightNowCard
                status={recordingStatus}
                nextMeeting={briefing.agenda.find(
                  (a) => a.status === "now" || a.status === "scheduled")
                }
                onGoToRecord={() => onNavigate?.("record")}
              />
            </div>
          </div>

          {/* Needs response — show the section even when empty so
              the user always knows where the check-offable items
              would appear if there were any. Empty state is a
              friendly placeholder, not dead white space. */}
          <section className="space-y-3">
            <div className="flex items-baseline justify-between">
              <h2 className="text-base font-semibold tracking-tight">
                Needs your response
              </h2>
              <span className="text-xs text-muted-foreground">
                {openActions.length > 0
                  ? `${openActions.length} open${doneActions.length > 0 ? ` · ${doneActions.length} done today` : ""}`
                  : "Nothing waiting on you"}
              </span>
            </div>
            {(openActions.length > 0 || doneActions.length > 0) ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {openActions.map((a) => (
                  <ActionCard
                    key={a.id}
                    action={a}
                    onToggle={() => toggleAction(a.id, false)}
                  />
                ))}
                {doneActions.map((a) => (
                  <ActionCard
                    key={a.id}
                    action={a}
                    onToggle={() => toggleAction(a.id, true)}
                    done
                  />
                ))}
              </div>
            ) : (
              <Card>
                <CardContent className="py-6 text-sm text-muted-foreground text-center">
                  No emails, Teams chats, or @mentions waiting on a reply right now.
                </CardContent>
              </Card>
            )}
          </section>

          {/* Today's agenda */}
          {briefing.agenda.length > 0 && (
            <section className="space-y-3">
              <h2 className="text-base font-semibold tracking-tight">
                Today's agenda
              </h2>
              <div className="space-y-2">
                {briefing.agenda.map((m) => (
                  <AgendaCard key={m.id} meeting={m} />
                ))}
              </div>
            </section>
          )}

          {/* Schedule notes + FYI */}
          {(briefing.schedule_notes.length > 0 || briefing.fyi.length > 0) && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {briefing.schedule_notes.length > 0 && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-semibold flex items-center gap-2">
                      <CalendarIcon className="h-4 w-4 text-muted-foreground" />
                      Schedule notes
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <ul className="space-y-2 text-sm text-foreground">
                      {briefing.schedule_notes.map((n, i) => (
                        <li key={i} className="flex gap-2">
                          <span className="text-muted-foreground">·</span>
                          <span>{n}</span>
                        </li>
                      ))}
                    </ul>
                  </CardContent>
                </Card>
              )}
              {briefing.fyi.length > 0 && (
                <Card>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-semibold flex items-center gap-2">
                      <Info className="h-4 w-4 text-muted-foreground" />
                      FYI
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {briefing.fyi.map((f) => (
                      <div key={f.id} className="text-sm">
                        <div className="flex items-center gap-2">
                          {f.category && (
                            <Badge variant="secondary" className="text-[10px] uppercase tracking-wide">
                              {f.category}
                            </Badge>
                          )}
                          <span className="font-medium text-foreground">
                            {f.title}
                          </span>
                        </div>
                        {f.detail && (
                          <p className="mt-1 text-muted-foreground">{f.detail}</p>
                        )}
                      </div>
                    ))}
                  </CardContent>
                </Card>
              )}
            </div>
          )}
        </>
      )}

      {/* Import dialog */}
      <ImportDialog
        open={importOpen}
        onOpenChange={(v) => {
          setImportOpen(v);
          if (!v) setImportError(null);
        }}
        text={pasteText}
        onText={setPasteText}
        onImport={handleImport}
        importing={importing}
        error={importError}
      />
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// Sub-components
// ────────────────────────────────────────────────────────────────────

function EmptyState({ onImport }: { onImport: () => void }) {
  return (
    <Card className="border-dashed">
      <CardContent className="py-12 text-center space-y-4">
        <Sparkles className="h-10 w-10 text-muted-foreground/60 mx-auto" />
        <div>
          <h3 className="text-base font-semibold">No briefing imported yet</h3>
          <p className="mt-1 text-sm text-muted-foreground max-w-md mx-auto">
            Run your Microsoft 365 Copilot scheduled prompt, copy the output,
            then click Import below. Meeting Recorder will parse it into a
            structured Today view.
          </p>
        </div>
        <Button onClick={onImport} className="gap-2">
          <ClipboardPaste className="h-4 w-4" /> Import briefing
        </Button>
      </CardContent>
    </Card>
  );
}

function TopPriorityCard({
  top,
}: { top: DailyBriefing["top_priority"] }) {
  if (!top) {
    return (
      <Card className="bg-muted/30 h-full">
        <CardContent className="py-8 text-sm text-muted-foreground">
          No standout priority called out in today's briefing.
        </CardContent>
      </Card>
    );
  }
  return (
    <Card className="bg-gradient-to-br from-blue-50 to-white border-blue-200 h-full">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-blue-700">
          <Sparkles className="h-3.5 w-3.5" />
          Top priority today
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <h3 className="text-lg font-semibold text-foreground leading-tight">
          {top.title}
        </h3>
        {top.detail && (
          <p className="text-sm text-foreground/80">{top.detail}</p>
        )}
        {top.why && (
          <p className="text-xs text-muted-foreground italic">
            Why today: {top.why}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function RightNowCard({
  status, nextMeeting, onGoToRecord,
}: {
  status: RecordingStatus | null;
  nextMeeting: BriefingAgendaItem | undefined;
  onGoToRecord: () => void;
}) {
  const isRecording = !!status?.is_recording;
  return (
    <Card className="h-full">
      <CardHeader className="pb-2">
        <CardTitle className="text-xs font-semibold uppercase tracking-wide text-muted-foreground flex items-center gap-1.5">
          <Circle className={
            isRecording ? "h-2 w-2 fill-red-500 text-red-500 animate-pulse"
                        : "h-2 w-2 fill-zinc-300 text-zinc-300"
          } />
          Right now
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {isRecording ? (
          <>
            <div className="flex items-center gap-2 text-sm">
              <Mic className="h-4 w-4 text-red-500" />
              <span className="font-medium text-foreground">
                Recording in progress
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              {status?.auto_record_subject || status?.current_status || "Untitled meeting"}
            </p>
            <Button size="sm" variant="outline" onClick={onGoToRecord} className="w-full mt-2">
              Open Record view
            </Button>
          </>
        ) : nextMeeting ? (
          <>
            <p className="text-xs text-muted-foreground">Next up</p>
            <p className="text-sm font-medium text-foreground leading-tight">
              {nextMeeting.title}
            </p>
            <p className="text-xs text-muted-foreground">
              {nextMeeting.time}
              {nextMeeting.duration && ` · ${nextMeeting.duration}`}
            </p>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">Nothing on the calendar.</p>
        )}
      </CardContent>
    </Card>
  );
}

function ActionCard({
  action, onToggle, done = false,
}: {
  action: DailyBriefing["needs_response"][number];
  onToggle: () => void;
  done?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={
        "text-left w-full rounded-lg border bg-card px-4 py-3 transition-colors " +
        "hover:bg-accent/30 focus:outline-none focus:ring-2 focus:ring-ring " +
        (done ? "opacity-60" : "")
      }
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 text-primary">
          {done ? (
            <CheckSquare className="h-5 w-5" />
          ) : (
            <Square className="h-5 w-5 text-muted-foreground" />
          )}
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <div className={
            "text-sm font-medium leading-snug " +
            (done ? "line-through text-muted-foreground" : "text-foreground")
          }>
            {action.title}
          </div>
          {action.detail && (
            <div className="text-xs text-muted-foreground leading-relaxed">
              {action.detail}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 pt-1 text-[11px] text-muted-foreground">
            {action.who && <span>From {action.who}</span>}
            {action.due && (
              <Badge variant="outline" className="text-[10px] font-normal">
                Due {action.due}
              </Badge>
            )}
            {action.source && (
              <span className="text-muted-foreground/70">· {action.source}</span>
            )}
          </div>
        </div>
      </div>
    </button>
  );
}

function AgendaCard({ meeting }: { meeting: BriefingAgendaItem }) {
  const cancelled = meeting.status === "cancelled";
  const isNow = meeting.status === "now";

  const typeKey = meeting.meeting_type || "general";
  const typeCls = TYPE_CLS[typeKey] || TYPE_CLS.general;
  const roleCls = meeting.role ? ROLE_CLS[meeting.role] : "";

  return (
    <div className={
      "flex gap-4 rounded-lg border bg-card px-4 py-3 " +
      (cancelled ? "opacity-60" : "") +
      (isNow ? " border-blue-300 bg-blue-50/50" : "")
    }>
      <div className="w-20 shrink-0 text-right">
        <div className="text-sm font-semibold text-foreground">
          {meeting.time || "—"}
        </div>
        {meeting.duration && (
          <div className="text-[11px] text-muted-foreground">
            {meeting.duration}
          </div>
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-start gap-2">
          <h4 className={
            "text-sm font-medium leading-snug " +
            (cancelled ? "line-through text-muted-foreground" : "text-foreground")
          }>
            {meeting.title}
            {cancelled && (
              <span className="ml-2 text-[11px] uppercase font-semibold tracking-wide text-red-600">
                cancelled
              </span>
            )}
            {isNow && (
              <span className="ml-2 text-[11px] uppercase font-semibold tracking-wide text-blue-700 inline-flex items-center gap-1">
                <Circle className="h-1.5 w-1.5 fill-blue-600 text-blue-600 animate-pulse" />
                now
              </span>
            )}
          </h4>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
          {meeting.meeting_type && (
            <span className={
              "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide " +
              typeCls
            }>
              {meeting.meeting_type}
            </span>
          )}
          {meeting.role && (
            <span className={
              "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium " +
              roleCls
            }>
              {meeting.role}
            </span>
          )}
          {meeting.client && (
            <span className="text-[11px] text-muted-foreground">
              {meeting.client}
            </span>
          )}
        </div>
        {meeting.notes && (
          <p className="mt-2 text-xs text-muted-foreground leading-relaxed">
            {meeting.notes}
          </p>
        )}
      </div>
    </div>
  );
}

function ImportDialog({
  open, onOpenChange, text, onText, onImport, importing, error,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  text: string;
  onText: (v: string) => void;
  onImport: () => void;
  importing: boolean;
  error: string | null;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
        Constrained-height layout: header (auto) + body (1fr, scrolls)
        + footer (auto). v2.10.0 shipped without a max-height so a
        long briefing pasted into the textarea blew the modal off-
        screen and hid the Parse button. The textarea also caps its
        own height + scrolls internally so the SURROUNDING modal
        never grows beyond the viewport.
      */}
      <DialogContent
        className={
          "!max-w-3xl w-[min(900px,calc(100vw-2rem))] " +
          "h-[min(720px,calc(100vh-4rem))] " +
          "flex flex-col gap-0 p-0 overflow-hidden"
        }
      >
        <DialogHeader className="p-4 pb-3 border-b shrink-0">
          <DialogTitle>Import daily briefing</DialogTitle>
          <DialogDescription>
            Paste the output from your Microsoft 365 Copilot scheduled
            prompt. Claude will parse it into priorities, agenda, action
            items, and FYI sections.
          </DialogDescription>
        </DialogHeader>
        <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-2">
          <Textarea
            value={text}
            onChange={(e) => onText(e.target.value)}
            placeholder="Paste your M365 Copilot briefing here (Ctrl+V)…"
            className={
              "h-[420px] min-h-[280px] max-h-[55vh] " +
              "resize-none overflow-y-auto font-mono text-xs"
            }
            autoFocus
          />
          <p className="text-[11px] text-muted-foreground">
            {text.length.toLocaleString()} characters · re-importing today
            keeps any action items you've already checked off
          </p>
          {error && (
            <div className="flex items-start gap-2 text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
              <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}
        </div>
        <DialogFooter className="shrink-0 m-0 rounded-b-xl">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={importing}>
            <X className="h-4 w-4 mr-1" /> Cancel
          </Button>
          <Button onClick={onImport} disabled={importing || !text.trim()}>
            {importing ? (
              <><Loader2 className="h-4 w-4 mr-1.5 animate-spin" /> Parsing</>
            ) : (
              <><CheckCircle2 className="h-4 w-4 mr-1.5" /> Parse and import</>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
