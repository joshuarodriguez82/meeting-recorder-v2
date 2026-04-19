"use client";

import { useEffect, useState } from "react";
import { api, formatBytes, type Meeting, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import {
  Mic,
  Calendar as CalendarIcon,
  History,
  CheckSquare,
  Target,
  Search,
  LayoutDashboard,
  Settings as SettingsIcon,
  HelpCircle,
  Sparkles,
  Square,
  Play,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

const TEMPLATES = [
  "General",
  "Requirements Gathering",
  "Design Review",
  "Sprint Planning",
  "Stakeholder Update",
];

const NAV_ITEMS = [
  { id: "record", label: "Record", icon: Mic },
  { id: "sessions", label: "Sessions", icon: History },
  { id: "follow-ups", label: "Follow-Ups", icon: CheckSquare },
  { id: "decisions", label: "Decisions", icon: Target },
  { id: "search", label: "Search", icon: Search },
  { id: "clients", label: "Clients", icon: LayoutDashboard },
];

export default function Home() {
  const [backendReady, setBackendReady] = useState(false);
  const [nav, setNav] = useState<string>("record");
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [meetingName, setMeetingName] = useState("");
  const [template, setTemplate] = useState("General");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [recording, setRecording] = useState(false);
  const [loadingCal, setLoadingCal] = useState(false);
  const [storage, setStorage] = useState<{
    total_bytes: number;
    session_count: number;
    wav_count: number;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        await api.health();
        if (!cancelled) setBackendReady(true);
      } catch {
        if (!cancelled) setTimeout(check, 1000);
      }
    };
    check();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!backendReady) return;
    (async () => {
      try {
        const [s, cal, stats] = await Promise.all([
          api.listSessions(),
          api.getCalendarToday().catch(() => []),
          api.getRetentionStats().catch(() => null),
        ]);
        setSessions(s);
        setMeetings(cal);
        setStorage(stats);
      } catch (e) {
        console.error(e);
      }
    })();
  }, [backendReady]);

  const refreshCalendar = async () => {
    setLoadingCal(true);
    try {
      const cal = await api.getCalendarToday();
      setMeetings(cal);
      toast.success(`Loaded ${cal.length} meetings`);
    } catch (e) {
      toast.error(`Calendar failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setLoadingCal(false);
    }
  };

  const startFromMeeting = (m: Meeting) => {
    const date = new Date(m.start).toISOString().slice(0, 10);
    setMeetingName(`${m.subject} - ${date}`);
    setNav("record");
    toast.info(`Meeting set: ${m.subject}`);
  };

  if (!backendReady) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
          <div className="text-sm">Starting backend…</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      {/* Sidebar */}
      <aside className="flex h-full w-60 flex-col border-r border-border bg-sidebar">
        <div className="flex h-16 items-center gap-2.5 border-b border-border px-4">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-sm">
            <Mic className="h-4 w-4" />
          </div>
          <div className="flex flex-col leading-tight">
            <span className="text-sm font-semibold">Meeting Recorder</span>
            <span className="text-[10px] text-muted-foreground">v2.0</span>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto p-3">
          <div className="mb-2 px-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Workspace
          </div>
          <ul className="space-y-0.5">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = nav === item.id;
              return (
                <li key={item.id}>
                  <button
                    onClick={() => setNav(item.id)}
                    className={`flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${
                      active
                        ? "bg-accent text-accent-foreground font-medium"
                        : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
                    }`}
                  >
                    <Icon className="h-4 w-4" />
                    {item.label}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="border-t border-border p-3 space-y-0.5">
          <button
            onClick={() => setNav("settings")}
            className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-muted-foreground hover:bg-accent/50 hover:text-foreground"
          >
            <SettingsIcon className="h-4 w-4" />
            Settings
          </button>
          <button
            onClick={() => setNav("help")}
            className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-muted-foreground hover:bg-accent/50 hover:text-foreground"
          >
            <HelpCircle className="h-4 w-4" />
            Usage Guide
          </button>
          {storage && (
            <div className="mt-2 rounded-md bg-muted/60 px-3 py-2 text-[11px] text-muted-foreground">
              <div className="flex items-center justify-between">
                <span>Storage</span>
                <span className="font-medium text-foreground">
                  {formatBytes(storage.total_bytes)}
                </span>
              </div>
              <div className="mt-0.5 text-[10px]">
                {storage.session_count} sessions · {storage.wav_count} audio
              </div>
            </div>
          )}
        </div>
      </aside>

      {/* Main */}
      <main className="flex flex-1 flex-col overflow-hidden">
        <header className="flex h-16 items-center justify-between border-b border-border bg-background/80 px-6 backdrop-blur">
          <div>
            <h1 className="text-lg font-semibold capitalize">{nav.replace("-", " ")}</h1>
            <p className="text-xs text-muted-foreground">
              {nav === "record" && "Start a new recording or pick one from your calendar"}
              {nav === "sessions" && "Browse every meeting you've recorded"}
              {nav === "follow-ups" && "Track action items across every meeting"}
              {nav === "decisions" && "Every decision, auto-generated ADR log"}
              {nav === "search" && "Search across all transcripts"}
              {nav === "clients" && "Per-client overview of meetings and work"}
              {nav === "settings" && "Configure API keys, devices, and workflow"}
              {nav === "help" && "How to use Meeting Recorder"}
            </p>
          </div>

          {nav === "record" && (
            <div className="flex items-center gap-2">
              {recording ? (
                <Button
                  variant="destructive"
                  onClick={() => {
                    setRecording(false);
                    toast.success("Recording stopped (stub)");
                  }}
                >
                  <Square className="h-4 w-4 mr-2" />
                  Stop Recording
                </Button>
              ) : (
                <Button
                  onClick={() => {
                    setRecording(true);
                    toast.info("Recording started (stub)");
                  }}
                  className="bg-red-600 hover:bg-red-700 text-white"
                >
                  <Play className="h-4 w-4 mr-2 fill-current" />
                  Start Recording
                </Button>
              )}
            </div>
          )}
        </header>

        <div className="flex-1 overflow-y-auto p-6">
          {nav === "record" && (
            <div className="mx-auto max-w-4xl space-y-6">
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base">
                    <Sparkles className="h-4 w-4 text-primary" />
                    Meeting Details
                  </CardTitle>
                </CardHeader>
                <CardContent className="grid grid-cols-1 gap-4 md:grid-cols-2">
                  <div className="md:col-span-2 space-y-2">
                    <Label htmlFor="mtg-name">Meeting Name</Label>
                    <Input
                      id="mtg-name"
                      value={meetingName}
                      onChange={(e) => setMeetingName(e.target.value)}
                      placeholder="e.g. Design Review — 2026-04-20"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>Template</Label>
                    <Select value={template} onValueChange={(v) => v && setTemplate(v)}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {TEMPLATES.map((t) => (
                          <SelectItem key={t} value={t}>
                            {t}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label>Client</Label>
                    <Input
                      value={client}
                      onChange={(e) => setClient(e.target.value)}
                      placeholder="Optional"
                    />
                  </div>
                  <div className="space-y-2 md:col-span-2">
                    <Label>Project</Label>
                    <Input
                      value={project}
                      onChange={(e) => setProject(e.target.value)}
                      placeholder="Optional"
                    />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="flex-row items-center justify-between space-y-0">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <CalendarIcon className="h-4 w-4 text-primary" />
                    Today&apos;s Meetings
                  </CardTitle>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={refreshCalendar}
                    disabled={loadingCal}
                  >
                    {loadingCal ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Refresh"}
                  </Button>
                </CardHeader>
                <CardContent>
                  {meetings.length === 0 ? (
                    <p className="text-sm text-muted-foreground text-center py-8">
                      No meetings for today, or Outlook not connected.
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
                              live
                                ? "bg-red-50 border-red-200 dark:bg-red-950/40 dark:border-red-900"
                                : past
                                ? "opacity-60"
                                : "hover:bg-muted/40"
                            }`}
                          >
                            <div className="flex flex-col items-start w-24 text-xs font-medium">
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
                              <Button size="sm" variant="outline" onClick={() => startFromMeeting(m)}>
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
            </div>
          )}

          {nav === "sessions" && <SessionsView sessions={sessions} />}

          {nav !== "record" && nav !== "sessions" && (
            <div className="flex h-full items-center justify-center">
              <div className="text-center max-w-sm">
                <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-muted">
                  <Sparkles className="h-8 w-8 text-muted-foreground" />
                </div>
                <h3 className="text-lg font-semibold mb-2">Coming soon</h3>
                <p className="text-sm text-muted-foreground">
                  This section will be built out in the next phase. The Python app still
                  has full functionality for everything — this is a UI rewrite in progress.
                </p>
              </div>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

function SessionsView({ sessions }: { sessions: SessionSummary[] }) {
  return (
    <div className="mx-auto max-w-6xl">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{sessions.length} sessions</CardTitle>
        </CardHeader>
        <CardContent>
          {sessions.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              No sessions yet. Hit Record to create one.
            </p>
          ) : (
            <div className="space-y-1">
              {sessions.map((s) => (
                <div
                  key={s.session_id}
                  className="flex items-center gap-4 rounded-md p-3 hover:bg-muted/40 border-b last:border-b-0"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{s.display_name}</div>
                    <div className="text-xs text-muted-foreground flex items-center gap-2 mt-0.5">
                      <span>{s.started_at ? new Date(s.started_at).toLocaleDateString() : "—"}</span>
                      {s.client && (<><span>·</span><span>{s.client}</span></>)}
                      {s.project && (<><span>·</span><span>{s.project}</span></>)}
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    {s.audio_exists && <Badge variant="outline" className="text-[10px]">🎤</Badge>}
                    {s.has_transcript && <Badge variant="outline" className="text-[10px]">⚙ transcript</Badge>}
                    {s.has_summary && <Badge variant="outline" className="text-[10px]">✨ summary</Badge>}
                    {s.has_action_items && <Badge variant="outline" className="text-[10px]">📋 actions</Badge>}
                    {s.has_decisions && <Badge variant="outline" className="text-[10px]">🎯 decisions</Badge>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
