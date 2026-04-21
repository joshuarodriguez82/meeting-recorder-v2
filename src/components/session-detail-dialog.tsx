"use client";

import { useEffect, useState } from "react";
import { api, type SessionFull, formatDuration } from "@/lib/api";
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
  Users, Save, X, Pencil, Check, StickyNote, Mail,
} from "lucide-react";

interface Props {
  sessionId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged?: () => void;
  initialTab?: string;
  existingClients?: string[];
  existingProjects?: string[];
}

const TEMPLATES = [
  "General",
  "Requirements Gathering",
  "Design Review",
  "Sprint Planning",
  "Stakeholder Update",
];

export function SessionDetailDialog({
  sessionId, open, onOpenChange, onChanged,
  initialTab = "overview", existingClients = [], existingProjects = [],
}: Props) {
  const [session, setSession] = useState<SessionFull | null>(null);
  const [loading, setLoading] = useState(false);
  const [processing, setProcessing] = useState<string | null>(null);
  const [processingStatus, setProcessingStatus] = useState("");
  const [tab, setTab] = useState(initialTab);

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
                <TabsTrigger value="summary" disabled={!session.summary}>Summary</TabsTrigger>
                <TabsTrigger value="actions" disabled={!session.action_items}>Actions</TabsTrigger>
                <TabsTrigger value="decisions" disabled={!session.decisions}>Decisions</TabsTrigger>
                <TabsTrigger value="requirements" disabled={!session.requirements}>Requirements</TabsTrigger>
              </TabsList>
            </div>

            <ScrollArea className="flex-1 min-h-0">
              <div className="p-6 min-w-0 max-w-full break-words">
                <TabsContent value="overview" className="mt-0 space-y-6">
                  {session.audio_path && (
                    <div className="space-y-2">
                      <Label className="text-xs uppercase tracking-wider text-muted-foreground">Recording</Label>
                      <audio
                        controls
                        preload="metadata"
                        className="w-full"
                        src={`http://127.0.0.1:17645/sessions/${sessionId}/audio`}
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
                        {TEMPLATES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
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
                        {existingProjects.map((p) => <option key={p} value={p} />)}
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
                      placeholder="e.g. Jane mentioned off-call that legal needs the SOW by Friday. I need to circle back with Ricoh on pricing next week."
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

function TranscriptView({ session }: { session: SessionFull }) {
  if (!session.segments || session.segments.length === 0) {
    return <p className="text-sm text-muted-foreground text-center py-8">No transcript yet. Run Process.</p>;
  }
  return (
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
            speakerId={sp.speaker_id}
            displayName={sp.display_name}
            segmentCount={count}
            onRenamed={onRenamed}
          />
        );
      })}
    </div>
  );
}

function SpeakerRow({
  sessionId, speakerId, displayName, segmentCount, onRenamed,
}: {
  sessionId: string;
  speakerId: string;
  displayName: string;
  segmentCount: number;
  onRenamed: () => void | Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(displayName);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setValue(displayName); }, [displayName]);

  const save = async () => {
    const next = value.trim();
    if (!next || next === displayName) {
      setEditing(false);
      setValue(displayName);
      return;
    }
    setSaving(true);
    try {
      await api.renameSpeaker(sessionId, speakerId, next);
      toast.success(`Renamed "${displayName}" to "${next}"`);
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
          <button
            onClick={() => setEditing(true)}
            className="group flex items-center gap-2 text-left w-full min-w-0"
          >
            <span className="text-sm font-medium truncate">{displayName || speakerId}</span>
            <Pencil className="h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 shrink-0" />
          </button>
        )}
        <div className="text-xs text-muted-foreground">
          {speakerId} · {segmentCount} segments
        </div>
      </div>
    </div>
  );
}

function MarkdownBlock({ content }: { content: string }) {
  if (!content) {
    return <p className="text-sm text-muted-foreground text-center py-8">Nothing here yet.</p>;
  }
  return (
    <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed bg-muted/40 rounded-lg p-5 max-w-full overflow-x-hidden">
      {content}
    </pre>
  );
}

function formatTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
