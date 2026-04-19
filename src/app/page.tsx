"use client";

import { useEffect, useState } from "react";
import { api, formatBytes, type SessionSummary } from "@/lib/api";
import {
  Mic, History, CheckSquare, Target, Search,
  LayoutDashboard, Settings as SettingsIcon, HelpCircle, Loader2,
  Sparkles,
} from "lucide-react";
import { RecordView } from "@/components/record-view";
import { SettingsView } from "@/components/settings-view";
import { SessionsView } from "@/components/sessions-view";
import { FollowUpsView } from "@/components/follow-ups-view";
import { DecisionsView } from "@/components/decisions-view";
import { SearchView } from "@/components/search-view";
import { ClientsView } from "@/components/clients-view";
import { PrepBriefView } from "@/components/prep-brief-view";
import { UsageGuideView } from "@/components/usage-guide-view";
import { CalendarMonitor } from "@/components/calendar-monitor";
import { SessionDetailDialog } from "@/components/session-detail-dialog";

const NAV_ITEMS = [
  { id: "record", label: "Record", icon: Mic },
  { id: "sessions", label: "Sessions", icon: History },
  { id: "follow-ups", label: "Follow-Ups", icon: CheckSquare },
  { id: "decisions", label: "Decisions", icon: Target },
  { id: "search", label: "Search", icon: Search },
  { id: "clients", label: "Clients", icon: LayoutDashboard },
  { id: "prep-brief", label: "Prep Brief", icon: Sparkles },
];

export default function Home() {
  const [backendReady, setBackendReady] = useState(false);
  const [nav, setNav] = useState<string>("record");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [storage, setStorage] = useState<{
    total_bytes: number;
    session_count: number;
    wav_count: number;
  } | null>(null);
  const [notifyMinutes, setNotifyMinutes] = useState(0);
  const [detailSessionId, setDetailSessionId] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailInitialTab, setDetailInitialTab] = useState("overview");

  const openSession = (id: string, tab: string = "overview") => {
    setDetailSessionId(id);
    setDetailInitialTab(tab);
    setDetailOpen(true);
  };

  const existingClients = Array.from(new Set(sessions.map((s) => s.client).filter(Boolean))).sort();
  const existingProjects = Array.from(new Set(sessions.map((s) => s.project).filter(Boolean))).sort();

  const [backendAttempts, setBackendAttempts] = useState(0);
  const [backendError, setBackendError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    const check = async () => {
      try {
        await api.health();
        if (!cancelled) setBackendReady(true);
      } catch {
        if (cancelled) return;
        attempts += 1;
        setBackendAttempts(attempts);
        if (attempts >= 30) {
          setBackendError(
            "Backend failed to start after 30 seconds. " +
            "Check %APPDATA%\\MeetingRecorder\\backend.log and rust.log for details."
          );
        } else {
          setTimeout(check, 1000);
        }
      }
    };
    check();
    return () => { cancelled = true; };
  }, []);

  const reloadSessions = async () => {
    try {
      const [s, stats, settings] = await Promise.all([
        api.listSessions(),
        api.getRetentionStats().catch(() => null),
        api.getSettings().catch(() => null),
      ]);
      setSessions(s);
      setStorage(stats);
      if (settings) setNotifyMinutes(settings.notify_minutes_before);
    } catch (e) {
      console.error(e);
    }
  };

  useEffect(() => {
    if (backendReady) reloadSessions();
  }, [backendReady]);

  if (!backendReady) {
    return (
      <div className="flex h-screen items-center justify-center p-8">
        {backendError ? (
          <div className="max-w-xl space-y-4">
            <div className="flex items-center gap-3 text-red-600">
              <div className="h-2 w-2 rounded-full bg-red-600" />
              <h2 className="font-semibold">Backend didn&apos;t start</h2>
            </div>
            <p className="text-sm text-muted-foreground">{backendError}</p>
            <div className="rounded-md border bg-muted/40 p-4 text-xs font-mono space-y-2">
              <div>
                <strong>Log files:</strong>
                <br />
                %APPDATA%\MeetingRecorder\backend.log
                <br />
                %APPDATA%\MeetingRecorder\rust.log
              </div>
              <div>
                <strong>Common causes:</strong>
                <br />
                • Python venv missing (run <code>python setup.py</code>)
                <br />
                • Another instance is running (check Task Manager for meeting-recorder.exe / pythonw.exe)
                <br />
                • Port 17645 held by a zombie process
              </div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => window.location.reload()}
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm"
              >
                Retry
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3 text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
            <div className="text-sm">Starting backend…</div>
            {backendAttempts > 5 && (
              <div className="text-xs text-muted-foreground max-w-xs text-center">
                Taking longer than expected. Attempt {backendAttempts}/30. Python is loading torch+pyannote; this can take up to 20s on first launch.
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      <CalendarMonitor
        enabled={notifyMinutes > 0}
        minutesBefore={notifyMinutes}
        onStart={() => setNav("record")}
      />
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
            className={`flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${
              nav === "settings" ? "bg-accent text-accent-foreground font-medium"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
            }`}
          >
            <SettingsIcon className="h-4 w-4" />
            Settings
          </button>
          <button
            onClick={() => setNav("help")}
            className={`flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${
              nav === "help" ? "bg-accent text-accent-foreground font-medium"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
            }`}
          >
            <HelpCircle className="h-4 w-4" />
            Usage Guide
          </button>
          {storage && (
            <div className="mt-2 rounded-md bg-muted/60 px-3 py-2 text-[11px] text-muted-foreground">
              <div className="flex items-center justify-between">
                <span>Storage</span>
                <span className="font-medium text-foreground">{formatBytes(storage.total_bytes)}</span>
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
              {nav === "prep-brief" && "Generate a pre-meeting brief from past sessions"}
              {nav === "settings" && "Configure API keys, devices, and workflow"}
              {nav === "help" && "How to use Meeting Recorder"}
            </p>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto p-8">
          {nav === "record" && (
            <RecordView onSessionsChanged={reloadSessions} onOpenSession={openSession} />
          )}
          {nav === "sessions" && (
            <SessionsView sessions={sessions} onReload={reloadSessions} onOpenSession={openSession} />
          )}
          {nav === "follow-ups" && (
            <FollowUpsView sessions={sessions} onOpenSession={openSession} />
          )}
          {nav === "decisions" && (
            <DecisionsView sessions={sessions} onOpenSession={openSession} />
          )}
          {nav === "search" && <SearchView onOpenSession={openSession} />}
          {nav === "clients" && (
            <ClientsView sessions={sessions} onReload={reloadSessions} onOpenSession={openSession} />
          )}
          {nav === "prep-brief" && <PrepBriefView sessions={sessions} />}
          {nav === "settings" && <SettingsView />}
          {nav === "help" && <UsageGuideView />}
        </div>
      </main>

      <SessionDetailDialog
        sessionId={detailSessionId}
        open={detailOpen}
        onOpenChange={(o) => { setDetailOpen(o); if (!o) setDetailSessionId(null); }}
        onChanged={reloadSessions}
        initialTab={detailInitialTab}
        existingClients={existingClients}
        existingProjects={existingProjects}
      />
    </div>
  );
}
