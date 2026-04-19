"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type AudioDevice, type Meeting, type SessionFull } from "@/lib/api";
import { toast } from "sonner";
import {
  Calendar as CalendarIcon,
  Sparkles,
  Loader2,
  Square,
  Play,
  Mic,
  Cog,
  ClipboardList,
  FileText,
  Target,
  Save,
  Mail,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";

interface Props {
  onSessionsChanged: () => void;
}

export function RecordView({ onSessionsChanged }: Props) {
  const [templates, setTemplates] = useState<string[]>([]);
  const [inputDevices, setInputDevices] = useState<AudioDevice[]>([]);
  const [outputDevices, setOutputDevices] = useState<AudioDevice[]>([]);
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [loadingCal, setLoadingCal] = useState(false);
  const [existingClients, setExistingClients] = useState<string[]>([]);
  const [existingProjects, setExistingProjects] = useState<string[]>([]);

  const [meetingName, setMeetingName] = useState("");
  const [template, setTemplate] = useState("General");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [micIdx, setMicIdx] = useState<number | null>(null);
  const [outIdx, setOutIdx] = useState<number | null>(null);
  const [attendees, setAttendees] = useState<string[]>([]);

  const [recording, setRecording] = useState(false);
  const [duration, setDuration] = useState(0);
  const [modelsReady, setModelsReady] = useState(false);
  const [modelsLoading, setModelsLoading] = useState(false);

  const [session, setSession] = useState<SessionFull | null>(null);
  const [processing, setProcessing] = useState<string | null>(null);

  // Load initial data
  useEffect(() => {
    (async () => {
      try {
        const [devices, tpls, cal, status, sessionsList] = await Promise.all([
          api.getAudioDevices(),
          api.getTemplates(),
          api.getUpcomingMeetings(36).catch(() => []),
          api.recordingStatus(),
          api.listSessions().catch(() => []),
        ]);
        setInputDevices(devices.input);
        setOutputDevices(devices.output);
        setTemplates(tpls);
        setMeetings(cal);
        // Gather unique clients and projects from existing sessions for autocomplete
        const clients = Array.from(new Set(
          sessionsList.map((s) => s.client).filter(Boolean)
        )).sort();
        const projects = Array.from(new Set(
          sessionsList.map((s) => s.project).filter(Boolean)
        )).sort();
        setExistingClients(clients);
        setExistingProjects(projects);
        if (devices.input.length > 0) setMicIdx(devices.input[0].index);
        setRecording(status.is_recording);
        setDuration(status.duration_s);
        setModelsReady(status.models_ready);
        setModelsLoading(status.models_loading);

        // Kick off background model load if not already done
        if (!status.models_ready && !status.models_loading) {
          api.loadModels().catch(() => {});
        }
      } catch (e) {
        console.error(e);
      }
    })();
  }, []);

  // Poll recording status while recording
  useEffect(() => {
    if (!recording) return;
    const t = setInterval(async () => {
      try {
        const s = await api.recordingStatus();
        setDuration(s.duration_s);
        if (!s.is_recording) setRecording(false);
      } catch {}
    }, 1000);
    return () => clearInterval(t);
  }, [recording]);

  // Poll for model readiness
  useEffect(() => {
    if (modelsReady) return;
    const t = setInterval(async () => {
      try {
        const s = await api.recordingStatus();
        setModelsReady(s.models_ready);
        setModelsLoading(s.models_loading);
        if (s.models_ready) clearInterval(t);
      } catch {}
    }, 3000);
    return () => clearInterval(t);
  }, [modelsReady]);

  const start = async () => {
    try {
      const res = await api.startRecording({
        mic_device_index: micIdx,
        output_device_index: outIdx,
        meeting_name: meetingName || new Date().toISOString().slice(0, 10) + " Meeting",
        template,
        client,
        project,
        attendees,
      });
      setRecording(true);
      setDuration(0);
      setSession(null);
      toast.success("Recording started", { description: `Session ${res.session_id}` });
    } catch (e) {
      toast.error(`Start failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const stop = async () => {
    try {
      const res = await api.stopRecording();
      setRecording(false);
      toast.success("Recording saved", { description: res.audio_path });
      // Reload the session into the UI
      const s = await api.getSessionFull(res.session_id);
      setSession(s);
      onSessionsChanged();
    } catch (e) {
      toast.error(`Stop failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const runProcess = async () => {
    if (!session) return;
    setProcessing("process");
    try {
      await api.processSession(session.session_id);
      const s = await api.getSessionFull(session.session_id);
      setSession(s);
      toast.success(`Transcribed (${s.segments.length} segments, ${Object.keys(s.speakers).length} speakers)`);
    } catch (e) {
      toast.error(`Process failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setProcessing(null);
    }
  };

  const runSummarize = async () => {
    if (!session) return;
    setProcessing("summarize");
    try {
      await api.summarize(session.session_id, template);
      const s = await api.getSessionFull(session.session_id);
      setSession(s);
      toast.success("Summary ready");
    } catch (e) {
      toast.error(`Summarize failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setProcessing(null);
    }
  };

  const runExtraction = async (
    kind: "action_items" | "requirements" | "decisions",
    label: string
  ) => {
    if (!session) return;
    setProcessing(kind);
    try {
      const fn = kind === "action_items" ? api.actionItems
        : kind === "requirements" ? api.requirements
        : api.decisions;
      await fn(session.session_id);
      const s = await api.getSessionFull(session.session_id);
      setSession(s);
      toast.success(`${label} extracted`);
    } catch (e) {
      toast.error(`${label} failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setProcessing(null);
    }
  };

  const refreshCal = async () => {
    setLoadingCal(true);
    try {
      const cal = await api.getUpcomingMeetings(36);
      setMeetings(cal);
      toast.success(`Loaded ${cal.length} upcoming meetings`);
    } catch (e) {
      toast.error(`Calendar: ${e instanceof Error ? e.message : e}`);
    } finally {
      setLoadingCal(false);
    }
  };

  const useMeeting = (m: Meeting) => {
    const date = new Date(m.start).toISOString().slice(0, 10);
    setMeetingName(`${m.subject} - ${date}`);
    setAttendees(m.attendees || []);
    toast.info(`Meeting loaded: ${m.subject}`);
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      {/* Recording bar — always visible when recording */}
      {recording && (
        <div className="flex items-center gap-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 dark:border-red-900 dark:bg-red-950/40">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-red-600 text-white">
            <div className="h-2.5 w-2.5 animate-pulse rounded-full bg-white" />
          </div>
          <div className="flex-1">
            <div className="text-sm font-medium text-red-900 dark:text-red-200">
              Recording in progress
            </div>
            <div className="text-xs text-red-700/80 dark:text-red-300/80">
              {formatDur(duration)} · {meetingName || "Untitled"}
            </div>
          </div>
          <Button variant="destructive" size="sm" onClick={stop}>
            <Square className="h-3.5 w-3.5 mr-2" />
            Stop
          </Button>
        </div>
      )}

      {/* Meeting details */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Sparkles className="h-4 w-4 text-primary" />
            Meeting Details
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Row 1: Meeting name (full width) */}
          <div className="space-y-2">
            <Label htmlFor="mtg-name">Meeting Name</Label>
            <Input
              id="mtg-name"
              value={meetingName}
              onChange={(e) => setMeetingName(e.target.value)}
              placeholder="e.g. Design Review — 2026-04-20"
              disabled={recording}
              autoComplete="off"
            />
          </div>

          {/* Row 2: Template (full width, since it's a key choice) */}
          <div className="space-y-2">
            <Label>Template</Label>
            <Select value={template} onValueChange={(v) => v && setTemplate(v)} disabled={recording}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {templates.map((t) => (
                  <SelectItem key={t} value={t}>{t}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Row 3: Client + Project side-by-side, equal width */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="client-input">Client</Label>
              <Input
                id="client-input"
                list="clients-list"
                value={client}
                onChange={(e) => setClient(e.target.value)}
                placeholder="e.g. SimpliSafe"
                disabled={recording}
                autoComplete="off"
              />
              <datalist id="clients-list">
                {existingClients.map((c) => (
                  <option key={c} value={c} />
                ))}
              </datalist>
            </div>
            <div className="space-y-2">
              <Label htmlFor="project-input">Project</Label>
              <Input
                id="project-input"
                list="projects-list"
                value={project}
                onChange={(e) => setProject(e.target.value)}
                placeholder="e.g. AWS Connect PoC"
                disabled={recording}
                autoComplete="off"
              />
              <datalist id="projects-list">
                {existingProjects.map((p) => (
                  <option key={p} value={p} />
                ))}
              </datalist>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Audio devices */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Mic className="h-4 w-4 text-primary" />
            Audio Devices
          </CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-2">
            <Label>Microphone</Label>
            <Select
              value={micIdx?.toString() ?? ""}
              onValueChange={(v: string | null) => setMicIdx(v ? parseInt(v) : null)}
              disabled={recording}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Select mic..." />
              </SelectTrigger>
              <SelectContent>
                {inputDevices.map((d) => (
                  <SelectItem key={d.index} value={d.index.toString()}>
                    [{d.index}] {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>System Audio (loopback)</Label>
            <Select
              value={outIdx?.toString() ?? "none"}
              onValueChange={(v: string | null) => setOutIdx(!v || v === "none" ? null : parseInt(v))}
              disabled={recording}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Skip" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">Skip — mic only</SelectItem>
                {outputDevices.map((d) => (
                  <SelectItem key={d.index} value={d.index.toString()}>
                    [{d.index}] {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Start button (when not recording) */}
      {!recording && !session && (
        <div className="flex justify-center">
          <Button size="lg" onClick={start} className="bg-red-600 hover:bg-red-700 text-white px-8 h-11">
            <Play className="h-4 w-4 mr-2 fill-current" />
            Start Recording
          </Button>
        </div>
      )}

      {/* Upcoming meetings */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <CalendarIcon className="h-4 w-4 text-primary" />
            Upcoming Meetings
          </CardTitle>
          <CardAction>
            <Button size="sm" variant="outline" onClick={refreshCal} disabled={loadingCal}>
              {loadingCal ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Refresh"}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          {meetings.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              No upcoming meetings in the next 36 hours, or Outlook not connected.
            </p>
          ) : (
            <div className="space-y-2">
              {meetings.map((m, i) => {
                const start = new Date(m.start);
                const end = new Date(m.end);
                const now = new Date();
                const live = start <= now && now <= end;
                const past = end < now;
                return (
                  <div
                    key={i}
                    className={`flex items-center gap-4 rounded-lg border p-3 transition-colors ${
                      live ? "bg-red-50 border-red-200 dark:bg-red-950/40 dark:border-red-900"
                        : past ? "opacity-60" : "hover:bg-muted/40"
                    }`}
                  >
                    <div className="flex flex-col items-start w-24 text-xs font-medium">
                      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                        {dayLabel(start)}
                      </span>
                      <span>{start.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                      <span className="text-muted-foreground">
                        {end.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                      </span>
                    </div>
                    {live && <Badge variant="destructive" className="text-[10px]">LIVE</Badge>}
                    <div className="flex-1 min-w-0">
                      <div className="truncate text-sm font-medium">{m.subject}</div>
                      {m.location && (
                        <div className="text-xs text-muted-foreground truncate">{m.location}</div>
                      )}
                    </div>
                    <span className="text-xs text-muted-foreground">{m.duration}m</span>
                    {!past && (
                      <Button size="sm" variant="outline" onClick={() => useMeeting(m)} disabled={recording}>
                        Use
                      </Button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Session view after recording */}
      {session && (
        <SessionPanel
          session={session}
          processing={processing}
          modelsReady={modelsReady}
          modelsLoading={modelsLoading}
          template={template}
          onProcess={runProcess}
          onSummarize={runSummarize}
          onExtract={runExtraction}
        />
      )}
    </div>
  );
}

function SessionPanel({
  session,
  processing,
  modelsReady,
  modelsLoading,
  template,
  onProcess,
  onSummarize,
  onExtract,
}: {
  session: SessionFull;
  processing: string | null;
  modelsReady: boolean;
  modelsLoading: boolean;
  template: string;
  onProcess: () => void;
  onSummarize: () => void;
  onExtract: (kind: "action_items" | "requirements" | "decisions", label: string) => void;
}) {
  const hasTranscript = session.segments && session.segments.length > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <FileText className="h-4 w-4 text-primary" />
          {session.display_name || `Session ${session.session_id}`}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Action buttons */}
        <div className="flex flex-wrap gap-2">
          <Button
            variant={hasTranscript ? "outline" : "default"}
            onClick={onProcess}
            disabled={processing !== null || !modelsReady}
          >
            {processing === "process" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <Cog className="h-3.5 w-3.5 mr-2" />}
            Process
          </Button>
          <Button variant="outline" onClick={onSummarize} disabled={!hasTranscript || processing !== null}>
            {processing === "summarize" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <Sparkles className="h-3.5 w-3.5 mr-2" />}
            Summarize
          </Button>
          <Button variant="outline" onClick={() => onExtract("action_items", "Action Items")} disabled={!hasTranscript || processing !== null}>
            {processing === "action_items" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <ClipboardList className="h-3.5 w-3.5 mr-2" />}
            Action Items
          </Button>
          <Button variant="outline" onClick={() => onExtract("requirements", "Requirements")} disabled={!hasTranscript || processing !== null}>
            {processing === "requirements" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <FileText className="h-3.5 w-3.5 mr-2" />}
            Requirements
          </Button>
          <Button variant="outline" onClick={() => onExtract("decisions", "Decisions")} disabled={!hasTranscript || processing !== null}>
            {processing === "decisions" ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <Target className="h-3.5 w-3.5 mr-2" />}
            Decisions
          </Button>
        </div>
        {!modelsReady && (
          <p className="text-xs text-muted-foreground">
            {modelsLoading ? "Loading AI models in background..." : "Models not loaded — check API keys in Settings."}
          </p>
        )}

        <Separator />

        {/* Content sections */}
        {hasTranscript && (
          <Section title="Transcript" content={formatTranscript(session)} />
        )}
        {session.summary && <Section title="Summary" content={session.summary} accent />}
        {session.action_items && <Section title="Action Items" content={session.action_items} accent />}
        {session.decisions && <Section title="Decisions" content={session.decisions} accent />}
        {session.requirements && <Section title="Requirements" content={session.requirements} accent />}
        {!hasTranscript && (
          <p className="text-sm text-muted-foreground text-center py-6">
            Click Process to transcribe the audio and identify speakers.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function Section({ title, content, accent = false }: { title: string; content: string; accent?: boolean }) {
  return (
    <div>
      <h3 className={`text-xs font-semibold uppercase tracking-wider mb-2 ${accent ? "text-primary" : "text-muted-foreground"}`}>
        {title}
      </h3>
      <div className="rounded-lg bg-muted/40 p-4 text-sm leading-relaxed whitespace-pre-wrap font-mono">
        {content}
      </div>
    </div>
  );
}

function formatTranscript(s: SessionFull): string {
  return s.segments
    .map((seg) => {
      const name = s.speakers[seg.speaker_id]?.display_name || seg.speaker_id;
      const start = formatT(seg.start);
      const end = formatT(seg.end);
      return `[${start} → ${end}] ${name}: ${seg.text}`;
    })
    .join("\n");
}

function formatT(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function dayLabel(d: Date): string {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = new Date(d);
  target.setHours(0, 0, 0, 0);
  const diff = Math.round((target.getTime() - today.getTime()) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  return d.toLocaleDateString([], { weekday: "short" });
}

function formatDur(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}
