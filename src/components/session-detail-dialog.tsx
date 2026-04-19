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
  Users, Save, X,
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
  const [tab, setTab] = useState(initialTab);

  // Editable state
  const [displayName, setDisplayName] = useState("");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [template, setTemplate] = useState("General");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!open || !sessionId) return;
    setLoading(true);
    setTab(initialTab);
    api.getSessionFull(sessionId)
      .then((s) => {
        setSession(s);
        setDisplayName(s.display_name || "");
        setClient(s.client || "");
        setProject(s.project || "");
        setTemplate(s.template || "General");
        setDirty(false);
      })
      .catch((e) => toast.error(`Could not load session: ${e}`))
      .finally(() => setLoading(false));
  }, [sessionId, open, initialTab]);

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
        client, project, template,
      });
      toast.success("Saved");
      setDirty(false);
      await reload();
      onChanged?.();
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const hasTranscript = session && session.segments && session.segments.length > 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl max-h-[85vh] flex flex-col p-0 gap-0">
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

            <ScrollArea className="flex-1">
              <div className="p-6">
                <TabsContent value="overview" className="mt-0 space-y-6">
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
                    </div>
                  </div>
                </TabsContent>

                <TabsContent value="transcript" className="mt-0">
                  <TranscriptView session={session} />
                </TabsContent>

                <TabsContent value="speakers" className="mt-0">
                  <SpeakersView session={session} />
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
    <div className="space-y-1 font-mono text-sm leading-relaxed">
      {session.segments.map((seg, i) => {
        const name = session.speakers[seg.speaker_id]?.display_name || seg.speaker_id;
        const start = formatTime(seg.start);
        return (
          <div key={i} className="flex gap-3 py-0.5 hover:bg-muted/30 rounded px-2">
            <span className="text-xs text-muted-foreground w-12 shrink-0 pt-0.5">{start}</span>
            <span className="font-semibold text-primary w-32 shrink-0 truncate">{name}</span>
            <span className="flex-1">{seg.text}</span>
          </div>
        );
      })}
    </div>
  );
}

function SpeakersView({ session }: { session: SessionFull }) {
  const speakers = Object.values(session.speakers);
  if (speakers.length === 0) {
    return <p className="text-sm text-muted-foreground text-center py-8">No speakers identified yet.</p>;
  }
  return (
    <div className="space-y-2">
      {speakers.map((sp) => {
        const count = session.segments.filter((s) => s.speaker_id === sp.speaker_id).length;
        return (
          <div key={sp.speaker_id} className="flex items-center gap-4 rounded-lg border p-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-accent text-accent-foreground">
              <Users className="h-4 w-4" />
            </div>
            <div className="flex-1">
              <div className="text-sm font-medium">{sp.display_name}</div>
              <div className="text-xs text-muted-foreground">
                {sp.speaker_id} · {count} segments
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function MarkdownBlock({ content }: { content: string }) {
  if (!content) {
    return <p className="text-sm text-muted-foreground text-center py-8">Nothing here yet.</p>;
  }
  return (
    <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed bg-muted/40 rounded-lg p-5">
      {content}
    </pre>
  );
}

function formatTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
