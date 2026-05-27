"use client";

import { useEffect, useState } from "react";
import { api, type SessionFull, type Speaker, formatDuration } from "@/lib/api";
import { toast } from "sonner";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  Tabs, TabsList, TabsTrigger, TabsContent,
} from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Loader2, Cog, Sparkles, ClipboardList, FileText, Target,
  Users, Save, X, Pencil, Check, StickyNote, Mail, Image as ImageIcon,
  Copy,
} from "lucide-react";

interface Props {
  sessionId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged?: () => void;
  initialTab?: string;
  existingClients?: string[];
  // Map of normalized-client-name → list of projects tagged under that
  // client. When the user changes the Client field in the dialog, the
  // Project dropdown narrows to projects under THAT client rather than
  // listing projects from every customer.
  projectsByClient?: Record<string, string[]>;
}

// Fallback list used only when the backend is unreachable (dialog opens
// during a backend restart, etc.). Normally the real list comes from
// /templates and includes any custom templates the user has added.
const FALLBACK_TEMPLATES = [
  "General",
  "Requirements Gathering",
  "Design Review",
  "Sprint Planning",
  "Stakeholder Update",
];

export function SessionDetailDialog({
  sessionId, open, onOpenChange, onChanged,
  initialTab = "overview", existingClients = [], projectsByClient = {},
}: Props) {
  const [templateNames, setTemplateNames] = useState<string[]>(FALLBACK_TEMPLATES);
  useEffect(() => {
    if (!open) return;
    api.getTemplates()
      .then((ts) => setTemplateNames(ts.map((t) => t.name)))
      .catch(() => { /* keep fallback */ });
  }, [open]);
  const [session, setSession] = useState<SessionFull | null>(null);
  const [loading, setLoading] = useState(false);
  const [processing, setProcessing] = useState<string | null>(null);
  const [processingStatus, setProcessingStatus] = useState("");
  const [tab, setTab] = useState(initialTab);
  // Resolved backend origin for direct media URLs (audio player,
  // screenshots). MUST come from getBaseUrl() — the backend port is
  // OS-picked at app startup, so any hardcoded 127.0.0.1:17645 points
  // at a dead port in the packaged app.
  const [baseUrl, setBaseUrl] = useState("");
  useEffect(() => {
    api.getBaseUrl().then(setBaseUrl).catch(() => {});
  }, []);

  // While an async backend job is running (process / summarize / extract),
  // poll /recording/status so we can surface `current_status` strings like
  // "Transcribing…" / "Identifying speakers…" as a subtle status line
  // under the dialog header. Without this the user just sees a spinner
  // with no idea what step is running or whether the backend is alive.
  useEffect(() => {
    if (!processing) {
      setProcessingStatus("");
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await api.recordingStatus();
        if (!cancelled) setProcessingStatus(s.current_status ?? "");
      } catch {
        if (!cancelled) setProcessingStatus("");
      }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => { cancelled = true; clearInterval(id); };
  }, [processing]);

  // Editable state
  const [displayName, setDisplayName] = useState("");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [template, setTemplate] = useState("General");
  const [notes, setNotes] = useState("");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!open || !sessionId) return;
    setLoading(true);
    // Only blank out the session if we're switching to a different id —
    // when the dialog just re-opens or `reload()` runs, keep the last
    // payload visible so the user doesn't see a flash of empty state.
    setSession((prev) => (prev && prev.session_id === sessionId ? prev : null));
    api.getSessionFull(sessionId)
      .then((s) => {
        setSession(s);
        setDisplayName(s.display_name || "");
        setClient(s.client || "");
        setProject(s.project || "");
        setTemplate(s.template || "General");
        setNotes(s.notes || "");
        setDirty(false);
      })
      .catch((e) => toast.error(`Could not load session: ${e}`))
      .finally(() => setLoading(false));
    // Only refetch when session id changes or dialog opens — NOT on tab changes

  }, [sessionId, open]);

  useEffect(() => {
    // Sync tab selection when caller changes initialTab while dialog is open
    if (open) setTab(initialTab);
  }, [initialTab, open]);

  const reload = async () => {
    if (!sessionId) return;
    const s = await api.getSessionFull(sessionId);
    setSession(s);
  };

  const runProcess = async () => {
    if (!sessionId) return;
    setProcessing("process");
    try {
      await api.processSession(sessionId);
      await reload();
      onChanged?.();
      toast.success("Transcribed + speakers identified");
      setTab("transcript");
    } catch (e) {
      toast.error(`Process failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setProcessing(null);
    }
  };

  const runSummarize = async () => {
    if (!sessionId) return;
    setProcessing("summarize");
    try {
      await api.summarize(sessionId, template);
      await reload();
      onChanged?.();
      toast.success("Summary ready");
      setTab("summary");
    } catch (e) {
      toast.error(`Summarize failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setProcessing(null);
    }
  };

  const runExtract = async (
    kind: "action_items" | "requirements" | "decisions",
    label: string,
    targetTab: string
  ) => {
    if (!sessionId) return;
    setProcessing(kind);
    try {
      const fn = kind === "action_items" ? api.actionItems
        : kind === "requirements" ? api.requirements
        : api.decisions;
      await fn(sessionId);
      await reload();
      onChanged?.();
      toast.success(`${label} extracted`);
      setTab(targetTab);
    } catch (e) {
      toast.error(`${label} failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setProcessing(null);
    }
  };

  const saveTags = async () => {
    if (!sessionId) return;
    try {
      await api.patchSession(sessionId, {
        display_name: displayName,
        client, project, template, notes,
      });
      // If the user typed a brand-new client name (one not already in
      // the existingClients list passed down from the page), persist it
      // to client_configs.json so it shows up alongside the configured
      // clients and syncs across devices. Fire-and-forget; the tag
      // itself is already saved on the session JSON above.
      const trimmedClient = (client || "").trim();
      if (trimmedClient) {
        const knownKeys = new Set(
          existingClients.map((c) => c.trim().toLowerCase()).filter(Boolean));
        if (!knownKeys.has(trimmedClient.toLowerCase())) {
          api.setClientConfig(trimmedClient, { export_folder: "" })
            .catch((e) => console.warn("Could not persist new client", trimmedClient, e));
        }
      }
      toast.success("Saved");
      setDirty(false);
      await reload();
      onChanged?.();
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const runFollowUpDrafts = async () => {
    if (!sessionId || !session) return;
    setProcessing("follow_up_drafts");
    const toastId = toast.loading("Preparing follow-up drafts…");
    try {
      // If action items haven't been extracted yet, do that first — the
      // drafter parses per-owner tasks from the action_items markdown, so
      // it can't produce anything useful without them. Running it inline
      // means one click does the whole thing.
      if (!session.action_items) {
        toast.loading("Extracting action items…", { id: toastId });
        await api.actionItems(sessionId);
        await reload();
        onChanged?.();
      }

      toast.loading("Drafting emails with Claude + creating Outlook drafts…",
                    { id: toastId });
      const r = await api.followUpDrafts(sessionId);
      if (r.drafts_created > 0) {
        toast.success(
          `${r.drafts_created} Outlook draft${r.drafts_created === 1 ? "" : "s"} created`,
          { id: toastId, description: "Check your Drafts folder in Classic Outlook" },
        );
      } else {
        toast.info("No owner-attributed action items to draft from", {
          id: toastId,
          description: "Claude didn't attribute any items to a specific person",
        });
      }
    } catch (e) {
      toast.error(
        `Follow-up drafts failed: ${e instanceof Error ? e.message : e}`,
        { id: toastId },
      );
    } finally {
      setProcessing(null);
    }
  };

  const hasTranscript = session && session.segments && session.segments.length > 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-5xl w-[95vw] h-[85vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-6 py-4 border-b">
          <DialogTitle className="text-lg">
            {loading ? <Loader2 className="h-5 w-5 animate-spin inline" />
              : session?.display_name || "Session"}
          </DialogTitle>
          {session && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground mt-1">
              <span>
                {session.started_at ? new Date(session.started_at).toLocaleString() : "—"}
              </span>
              {session.client && <><span>·</span><Badge variant="outline" className="text-[10px]">{session.client}</Badge></>}
              {session.project && <><span>·</span><Badge variant="outline" className="text-[10px]">{session.project}</Badge></>}
              <span>·</span>
              <span>
                {formatDuration(
                  session.started_at && session.ended_at
                    ? Math.round((new Date(session.ended_at).getTime() -
                        new Date(session.started_at).getTime()) / 1000)
                    : 0
                )}
              </span>
            </div>
          )}
        </DialogHeader>

        {loading && !session ? (
          <div className="flex-1 flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : session && (
          <Tabs value={tab} onValueChange={setTab} className="flex-1 flex flex-col min-h-0">
            <div className="px-6 pt-3 border-b">
              <TabsList className="bg-transparent p-0 h-auto">
                <TabsTrigger value="overview" className="data-[state=active]:bg-accent">Overview</TabsTrigger>
                <TabsTrigger value="notes">
                  <StickyNote className="h-3.5 w-3.5 mr-1" />
                  Notes {notes && <span className="ml-1 text-[10px] text-muted-foreground">•</span>}
                </TabsTrigger>
                <TabsTrigger value="transcript" disabled={!hasTranscript}>
                  Transcript {hasTranscript && <span className="ml-1 text-muted-foreground">({session.segments.length})</span>}
                </TabsTrigger>
                <TabsTrigger value="speakers" disabled={Object.keys(session.speakers).length === 0}>
                  Speakers {Object.keys(session.speakers).length > 0 && <span className="ml-1 text-muted-foreground">({Object.keys(session.speakers).length})</span>}
                </TabsTrigger>
                <TabsTrigger
                  value="screenshots"
                  disabled={(session.screenshots?.length ?? 0) === 0}
                >
                  <ImageIcon className="h-3.5 w-3.5 mr-1" />
                  Screenshots {(session.screenshots?.length ?? 0) > 0 && (
                    <span className="ml-1 text-muted-foreground">
                      ({session.screenshots?.length})
                    </span>
                  )}
                </TabsTrigger>
                <TabsTrigger
                  value="copilot"
                  disabled={(session.copilot_ticks?.length ?? 0) === 0}
                >
                  <Sparkles className="h-3.5 w-3.5 mr-1" />
                  Co-Pilot {(session.copilot_ticks?.length ?? 0) > 0 && (
                    <span className="ml-1 text-muted-foreground">
                      ({session.copilot_ticks?.length})
                    </span>
                  )}
                </TabsTrigger>
                <TabsTrigger value="summary" disabled={!session.summary}>Summary</TabsTrigger>
                <TabsTrigger value="actions" disabled={!session.action_items}>Actions</TabsTrigger>
                <TabsTrigger value="decisions" disabled={!session.decisions}>Decisions</TabsTrigger>
                <TabsTrigger value="requirements" disabled={!session.requirements}>Requirements</TabsTrigger>
              </TabsList>
            </div>

            <ScrollArea className="flex-1 min-h-0">
              <div className="p-6 min-w-0 max-w-full break-words">
                <TabsContent value="overview" className="mt-0 space-y-6">
                  {session.audio_path && baseUrl && (
                    <div className="space-y-2">
                      <Label className="text-xs uppercase tracking-wider text-muted-foreground">Recording</Label>
                      <audio
                        controls
                        preload="metadata"
                        className="w-full"
                        src={`${baseUrl}/sessions/${sessionId}/audio`}
                      >
                        Your browser doesn&apos;t support audio playback.
                      </audio>
                    </div>
                  )}
                  <div className="space-y-2">
                    <Label>Meeting Name</Label>
                    <Input
                      value={displayName}
                      onChange={(e) => { setDisplayName(e.target.value); setDirty(true); }}
                      autoComplete="off"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>Template</Label>
                    <Select value={template} onValueChange={(v) => { if (v) { setTemplate(v); setDirty(true); } }}>
                      <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {templateNames.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-2">
                      <Label>Client</Label>
                      <Input
                        list="detail-clients-list"
                        value={client}
                        onChange={(e) => { setClient(e.target.value); setDirty(true); }}
                        autoComplete="off"
                      />
                      <datalist id="detail-clients-list">
                        {existingClients.map((c) => <option key={c} value={c} />)}
                      </datalist>
                    </div>
                    <div className="space-y-2">
                      <Label>Project</Label>
                      <Input
                        list="detail-projects-list"
                        value={project}
                        onChange={(e) => { setProject(e.target.value); setDirty(true); }}
                        autoComplete="off"
                      />
                      <datalist id="detail-projects-list">
                        {(projectsByClient[client.trim().toLowerCase()] || []).map((p) => (
                          <option key={p} value={p} />
                        ))}
                      </datalist>
                    </div>
                  </div>
                  {dirty && (
                    <Button onClick={saveTags}>
                      <Save className="h-4 w-4 mr-2" />
                      Save Changes
                    </Button>
                  )}

                  <div className="pt-4 border-t space-y-3">
                    <Label className="text-xs uppercase tracking-wider text-muted-foreground">AI Actions</Label>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant={hasTranscript ? "outline" : "default"}
                        size="sm"
                        onClick={runProcess}
                        disabled={processing !== null}
                      >
                        {processing === "process" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <Cog className="h-3.5 w-3.5 mr-2" />}
                        {hasTranscript ? "Re-process" : "Process"}
                      </Button>
                      <Button
                        variant="outline" size="sm"
                        onClick={runSummarize}
                        disabled={!hasTranscript || processing !== null}
                      >
                        {processing === "summarize" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <Sparkles className="h-3.5 w-3.5 mr-2" />}
                        Summarize
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => runExtract("action_items", "Action items", "actions")} disabled={!hasTranscript || processing !== null}>
                        {processing === "action_items" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <ClipboardList className="h-3.5 w-3.5 mr-2" />}
                        Action Items
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => runExtract("decisions", "Decisions", "decisions")} disabled={!hasTranscript || processing !== null}>
                        {processing === "decisions" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <Target className="h-3.5 w-3.5 mr-2" />}
                        Decisions
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => runExtract("requirements", "Requirements", "requirements")} disabled={!hasTranscript || processing !== null}>
                        {processing === "requirements" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <FileText className="h-3.5 w-3.5 mr-2" />}
                        Requirements
                      </Button>
                      <Button
                        variant="outline" size="sm"
                        onClick={runFollowUpDrafts}
                        disabled={!hasTranscript || processing !== null}
                        title={hasTranscript
                          ? (session.action_items
                              ? "Create an Outlook draft email per attendee with their action items"
                              : "Extract action items + create Outlook drafts (one click)")
                          : "Run Process first — need a transcript before drafting emails"}
                      >
                        {processing === "follow_up_drafts"
                          ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" />
                          : <Mail className="h-3.5 w-3.5 mr-2" />}
                        Draft follow-up emails
                      </Button>
                    </div>
                    {processing && (
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <Loader2 className="h-3 w-3 animate-spin" />
                        <span>
                          {processingStatus || "Working…"}
                        </span>
                      </div>
                    )}
                  </div>
                </TabsContent>

                <TabsContent value="notes" className="mt-0 space-y-3">
                  <div className="space-y-2">
                    <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                      Your session notes
                    </Label>
                    <p className="text-xs text-muted-foreground">
                      Things the transcript doesn&apos;t capture — hallway context,
                      reminders to yourself, commitments you made off-mic, follow-ups
                      you don&apos;t want to forget. Claude reads these when it generates
                      the summary, action items, decisions, and requirements. Re-run
                      any extraction to pick up edits.
                    </p>
                    <textarea
                      value={notes}
                      onChange={(e) => { setNotes(e.target.value); setDirty(true); }}
                      placeholder="e.g. Jane mentioned off-call that legal needs the SOW by Friday. I need to circle back with Hooli on pricing next week."
                      className="w-full min-h-[320px] rounded-md border border-input bg-background px-3 py-2 text-sm font-mono leading-relaxed focus:outline-none focus:ring-1 focus:ring-ring resize-y"
                    />
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>{notes.length.toLocaleString()} characters</span>
                      {dirty && (
                        <Button size="sm" onClick={saveTags}>
                          <Save className="h-3.5 w-3.5 mr-2" />
                          Save notes
                        </Button>
                      )}
                    </div>
                  </div>
                </TabsContent>

                <TabsContent value="transcript" className="mt-0">
                  <TranscriptView session={session} />
                </TabsContent>

                <TabsContent value="speakers" className="mt-0">
                  <SpeakersView
                    session={session}
                    onRenamed={async () => { await reload(); onChanged?.(); }}
                  />
                </TabsContent>

                <TabsContent value="screenshots" className="mt-0">
                  <ScreenshotsView session={session} />
                </TabsContent>

                <TabsContent value="copilot" className="mt-0">
                  <CoPilotTicksView session={session} />
                </TabsContent>

                <TabsContent value="summary" className="mt-0">
                  <MarkdownBlock content={session.summary || ""} />
                </TabsContent>

                <TabsContent value="actions" className="mt-0">
                  <MarkdownBlock content={session.action_items || ""} />
                </TabsContent>

                <TabsContent value="decisions" className="mt-0">
                  <MarkdownBlock content={session.decisions || ""} />
                </TabsContent>

                <TabsContent value="requirements" className="mt-0">
                  <MarkdownBlock content={session.requirements || ""} />
                </TabsContent>
              </div>
            </ScrollArea>
          </Tabs>
        )}
      </DialogContent>
    </Dialog>
  );
}

function ScreenshotsView({ session }: { session: SessionFull }) {
  const shots = session.screenshots ?? [];
  const [baseUrl, setBaseUrl] = useState<string>("");
  // Index of the screenshot shown full-size in the lightbox; null = grid.
  const [zoomed, setZoomed] = useState<number | null>(null);

  useEffect(() => {
    // Images stream from the backend (dynamic port in production), same
    // approach as the audio player — never read local file paths from
    // the webview.
    api.getBaseUrl().then(setBaseUrl).catch(() => {});
  }, []);

  if (shots.length === 0) {
    return (
      <p className="text-sm text-muted-foreground text-center py-8">
        No screenshots were captured during this meeting.
      </p>
    );
  }

  const srcFor = (i: number) =>
    `${baseUrl}/sessions/${session.session_id}/screenshots/${i}`;

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        {shots.length} screenshot{shots.length !== 1 ? "s" : ""} captured during
        this meeting. These are included as visual context when generating the
        summary, and stay with the recording for future reference.
      </p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {shots.map((_, i) => (
          <button
            key={i}
            type="button"
            onClick={() => setZoomed(i)}
            className="group relative overflow-hidden rounded-lg border bg-muted/30 transition hover:ring-2 hover:ring-primary"
            title="Click to enlarge"
          >
            {baseUrl && (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={srcFor(i)}
                alt={`Screenshot ${i + 1}`}
                className="h-36 w-full object-cover"
                loading="lazy"
              />
            )}
            <span className="absolute bottom-1 right-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] text-white">
              {i + 1}
            </span>
          </button>
        ))}
      </div>

      {zoomed !== null && baseUrl && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6"
          onClick={() => setZoomed(null)}
          role="dialog"
          aria-modal="true"
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={srcFor(zoomed)}
            alt={`Screenshot ${zoomed + 1}`}
            className="max-h-full max-w-full rounded-lg object-contain shadow-2xl"
          />
          <button
            type="button"
            onClick={() => setZoomed(null)}
            className="absolute right-4 top-4 rounded-full bg-white/10 p-2 text-white hover:bg-white/20"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
      )}
    </div>
  );
}

// Saved Live Co-Pilot ticks for a finished session. Mirrors the live
// panel's layout but reads from `session.copilot_ticks` (persisted on
// every tick during recording) instead of polling. Newest first so the
// last coaching pass is what the user sees on open — matches the in-
// call presentation.
function CoPilotTicksView({ session }: { session: SessionFull }) {
  const ticks = session.copilot_ticks ?? [];
  if (ticks.length === 0) {
    return (
      <p className="text-sm text-muted-foreground text-center py-8">
        No Co-Pilot ticks were recorded during this meeting.
      </p>
    );
  }
  const newestFirst = [...ticks].reverse();

  // Build an "all-ticks" plain-text dump for the header Copy button —
  // headed by section + timestamp, ordered newest-first the same way
  // the cards render. Empty sections skipped so the clipboard isn't
  // padded with blank headers.
  const allText = newestFirst.map((t) => {
    const generated = t.generated_at
      ? new Date(t.generated_at).toLocaleString()
      : "";
    const blocks: string[] = [];
    if (generated) blocks.push(`=== ${generated} ===`);
    const sections: Array<[string, string[] | undefined]> = [
      ["Clarifying questions", t.clarifying_questions],
      ["Risks & assumptions", t.risks],
      ["Suggested follow-ups", t.follow_ups],
    ];
    for (const [title, items] of sections) {
      if (!items || items.length === 0) continue;
      blocks.push(`${title}:\n${items.map((s) => `  • ${s}`).join("\n")}`);
    }
    return blocks.join("\n\n");
  }).join("\n\n---\n\n");

  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <p className="text-xs text-muted-foreground flex-1">
          {ticks.length} coaching update{ticks.length === 1 ? "" : "s"} the
          Live Co-Pilot produced during this meeting, newest first. Each
          update reflected what had been said in the previous ~10 minutes
          at the time the tick fired.
        </p>
        <CopyButton text={allText} label="Copy all ticks" />
      </div>
      {newestFirst.map((t, i) => {
        const sections: Array<{
          title: string;
          items: string[] | undefined;
        }> = [
          { title: "Clarifying questions", items: t.clarifying_questions },
          { title: "Risks & assumptions", items: t.risks },
          { title: "Suggested follow-ups", items: t.follow_ups },
        ];
        const generated = t.generated_at
          ? new Date(t.generated_at).toLocaleString()
          : "";

        // Plain-text version of this single tick — for the per-card
        // Copy button. Same format as the all-ticks blob above so
        // pasted output is consistent regardless of which button was
        // used.
        const tickText = (() => {
          const blocks: string[] = [];
          if (generated) blocks.push(`=== ${generated} ===`);
          for (const { title, items } of sections) {
            if (!items || items.length === 0) continue;
            blocks.push(`${title}:\n${items.map((s) => `  • ${s}`).join("\n")}`);
          }
          return blocks.join("\n\n");
        })();

        return (
          <div
            key={(t.generated_at || "") + i}
            className="rounded-md border bg-muted/30 p-3 space-y-2"
          >
            <div className="flex items-center justify-between text-[10px] uppercase tracking-wide text-muted-foreground">
              <span>{generated}</span>
              <div className="flex items-center gap-2">
                {t.segment_count > 0 && (
                  <span>{t.segment_count} segments</span>
                )}
                <CopyButton text={tickText} label="Copy" />
              </div>
            </div>
            {sections.map(({ title, items }) => {
              if (!items || items.length === 0) return null;
              const sectionText =
                `${title}:\n${items.map((s) => `  • ${s}`).join("\n")}`;
              return (
                <div key={title} className="space-y-1">
                  <div className="flex items-center justify-between">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                      {title}
                    </p>
                    <CopyButton text={sectionText} label="Copy" />
                  </div>
                  <ul className="space-y-1">
                    {items.map((s, j) => (
                      <li
                        key={j}
                        className="text-sm leading-snug flex gap-2 group"
                      >
                        <span className="text-muted-foreground select-none">
                          •
                        </span>
                        <span className="flex-1">{s}</span>
                        <CopyButton
                          text={s}
                          label="Copy"
                          className="opacity-0 group-hover:opacity-100"
                        />
                      </li>
                    ))}
                  </ul>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

function TranscriptView({ session }: { session: SessionFull }) {
  if (!session.segments || session.segments.length === 0) {
    return <p className="text-sm text-muted-foreground text-center py-8">No transcript yet. Run Process.</p>;
  }
  // Build a copy-ready plain-text version with resolved speaker names
  // so the clipboard contents look the same as what's on screen.
  const plain = session.segments.map((seg) => {
    const name = session.speakers[seg.speaker_id]?.display_name || seg.speaker_id;
    return `[${formatTime(seg.start)}] ${name}: ${seg.text}`;
  }).join("\n");
  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <CopyButton text={plain} label="Copy transcript" />
      </div>
      <div className="space-y-1 font-mono text-sm leading-relaxed max-w-full">
        {session.segments.map((seg, i) => {
          const name = session.speakers[seg.speaker_id]?.display_name || seg.speaker_id;
          const start = formatTime(seg.start);
          return (
            <div key={i} className="flex gap-3 py-0.5 hover:bg-muted/30 rounded px-2 min-w-0">
              <span className="text-xs text-muted-foreground w-12 shrink-0 pt-0.5">{start}</span>
              <span className="font-semibold text-primary w-32 shrink-0 truncate">{name}</span>
              <span className="flex-1 min-w-0 break-words">{seg.text}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SpeakersView({
  session,
  onRenamed,
}: {
  session: SessionFull;
  onRenamed: () => void | Promise<void>;
}) {
  const speakers = Object.values(session.speakers);
  if (speakers.length === 0) {
    return <p className="text-sm text-muted-foreground text-center py-8">No speakers identified yet.</p>;
  }
  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground mb-3">
        Click a speaker&apos;s name to rename them. The new name flows into the transcript,
        summary, action items, and decisions (next time you regenerate them).
      </p>
      {speakers.map((sp) => {
        const count = session.segments.filter((s) => s.speaker_id === sp.speaker_id).length;
        return (
          <SpeakerRow
            key={sp.speaker_id}
            sessionId={session.session_id}
            speaker={sp}
            segmentCount={count}
            onRenamed={onRenamed}
          />
        );
      })}
    </div>
  );
}

function SpeakerRow({
  sessionId, speaker, segmentCount, onRenamed,
}: {
  sessionId: string;
  speaker: Speaker;
  segmentCount: number;
  onRenamed: () => void | Promise<void>;
}) {
  const speakerId = speaker.speaker_id;
  const displayName = speaker.display_name;
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(displayName);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setValue(displayName); }, [displayName]);

  // An auto-match is "pending review" when the backend linked the
  // speaker to a profile but the user hasn't confirmed or rejected yet.
  // The match_confidence number doubles as the badge text.
  const pendingMatch =
    !!speaker.profile_id
    && speaker.match_confidence != null
    && speaker.match_confirmed === false;
  const confidencePct = pendingMatch && speaker.match_confidence != null
    ? Math.round(speaker.match_confidence * 100)
    : null;

  const save = async () => {
    const next = value.trim();
    if (!next || next === displayName) {
      setEditing(false);
      setValue(displayName);
      return;
    }
    setSaving(true);
    try {
      const res = await api.renameSpeaker(sessionId, speakerId, next);
      // Honest toast: tell the user what actually happened with the
      // cross-session profile, not just that the display name changed.
      // Same-named speakers get linked silently — no need to brag — but
      // skipped fingerprinting needs a heads-up so the user doesn't
      // wonder why Settings → Known Speakers stays empty.
      switch (res.profile_action) {
        case "created":
          toast.success(`Saved voice profile for "${next}". Future meetings will auto-tag them.`);
          break;
        case "linked":
          toast.success(`Linked to existing "${next}" profile.`);
          break;
        case "refined":
          toast.success(`Refined "${next}" voice profile.`);
          break;
        case "skipped":
          toast.warning(
            `Renamed to "${next}", but no voice profile saved.`,
            { description: res.profile_skip_reason ?? undefined },
          );
          break;
      }
      setEditing(false);
      await onRenamed();
    } catch (e) {
      toast.error(`Rename failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setEditing(false);
    setValue(displayName);
  };

  const confirmMatch = async () => {
    if (!speaker.profile_id) return;
    setSaving(true);
    try {
      await api.confirmSpeakerMatch(sessionId, speakerId, speaker.profile_id);
      toast.success(`Confirmed: this is ${displayName}`);
      await onRenamed();
    } catch (e) {
      toast.error(`Confirm failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const rejectMatch = async () => {
    setSaving(true);
    try {
      await api.rejectSpeakerMatch(sessionId, speakerId);
      toast.info(`Reset to ${speakerId}. You can rename them manually now.`);
      await onRenamed();
    } catch (e) {
      toast.error(`Reject failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex items-center gap-4 rounded-lg border p-3">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-accent text-accent-foreground shrink-0">
        <Users className="h-4 w-4" />
      </div>
      <div className="flex-1 min-w-0">
        {editing ? (
          <div className="flex items-center gap-2">
            <Input
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") save();
                if (e.key === "Escape") cancel();
              }}
              disabled={saving}
              className="h-8"
            />
            <Button size="sm" onClick={save} disabled={saving} className="h-8">
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            </Button>
            <Button size="sm" variant="ghost" onClick={cancel} disabled={saving} className="h-8">
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => setEditing(true)}
              className="group flex items-center gap-2 text-left min-w-0"
            >
              <span className="text-sm font-medium truncate">{displayName || speakerId}</span>
              <Pencil className="h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 shrink-0" />
            </button>
            {pendingMatch && confidencePct != null && (
              <span
                className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary"
                title="The backend matched this voice to a saved profile. Confirm or reject below."
              >
                Likely match · {confidencePct}%
              </span>
            )}
          </div>
        )}
        <div className="text-xs text-muted-foreground">
          {speakerId} · {segmentCount} segments
          {speaker.match_confirmed && speaker.profile_id && (
            <span className="ml-2 text-primary/80">· profile linked</span>
          )}
        </div>
        {pendingMatch && !editing && (
          <div className="mt-2 flex items-center gap-2">
            <Button
              size="sm"
              onClick={confirmMatch}
              disabled={saving}
              className="h-7 px-3"
            >
              {saving ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Check className="h-3 w-3 mr-1" />}
              Yes, this is {displayName}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={rejectMatch}
              disabled={saving}
              className="h-7 px-3"
            >
              <X className="h-3 w-3 mr-1" />
              Not them
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

function MarkdownBlock({ content }: { content: string }) {
  if (!content) {
    return <p className="text-sm text-muted-foreground text-center py-8">Nothing here yet.</p>;
  }
  return (
    <div className="relative">
      <CopyButton text={content} className="absolute right-2 top-2 z-10" />
      <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed bg-muted/40 rounded-lg p-5 pr-12 max-w-full overflow-x-hidden">
        {content}
      </pre>
    </div>
  );
}

// Shared one-click clipboard copy. Lives next to the content it copies
// so the user doesn't have to select-all / right-click / paste — the
// pattern that motivated this in the first place. Falls back to a
// textarea + execCommand path when navigator.clipboard isn't available
// (older WebViews, non-secure contexts).
function CopyButton({
  text, label = "Copy", className = "",
}: { text: string; label?: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const onClick = async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      toast.success("Copied to clipboard");
      setTimeout(() => setCopied(false), 1500);
    } catch (e) {
      toast.error(`Copy failed: ${e instanceof Error ? e.message : e}`);
    }
  };
  return (
    <Button
      variant="ghost"
      size="sm"
      className={`h-7 text-xs ${className}`}
      onClick={onClick}
      title="Copy to clipboard"
    >
      {copied ? <Check className="h-3.5 w-3.5 mr-1" /> : <Copy className="h-3.5 w-3.5 mr-1" />}
      {copied ? "Copied" : label}
    </Button>
  );
}

function formatTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
