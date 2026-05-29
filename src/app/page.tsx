"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { toast } from "sonner";
import { api, formatBytes, openExternal, type Meeting, type SessionSummary } from "@/lib/api";
import {
  Mic, History, CheckSquare, Target, Search,
  LayoutDashboard, Settings as SettingsIcon, HelpCircle, Loader2,
  Sparkles, MessageCircle, Handshake, BarChart3, FileSpreadsheet,
  Sun,
} from "lucide-react";
import { InsightsView } from "@/components/insights-view";
import { TodayView } from "@/components/today-view";
import { OnboardingTour, onboardingDismissed } from "@/components/onboarding-tour";
import { RecordView } from "@/components/record-view";
import { SettingsView } from "@/components/settings-view";
import { SessionsView } from "@/components/sessions-view";
import { FollowUpsView } from "@/components/follow-ups-view";
import { DecisionsView } from "@/components/decisions-view";
import { SearchView } from "@/components/search-view";
import { ClientsView } from "@/components/clients-view";
import { EngagementView } from "@/components/engagement-view";
import { PrepBriefView } from "@/components/prep-brief-view";
import { QAView } from "@/components/qa-view";
import { CommitmentsView } from "@/components/commitments-view";
import { UsageGuideView } from "@/components/usage-guide-view";
import { CalendarMonitor } from "@/components/calendar-monitor";
import { SessionDetailDialog } from "@/components/session-detail-dialog";
import { useUnprocessedSessions } from "@/lib/useUnprocessedSessions";

// "today" is opt-in (Settings → Daily Briefing). It's prepended to the
// nav at render time only when today_view_enabled is true, so it's not
// in this static list.
const NAV_ITEMS = [
  { id: "record", label: "Record", icon: Mic },
  { id: "sessions", label: "Sessions", icon: History },
  { id: "follow-ups", label: "Follow-Ups", icon: CheckSquare },
  { id: "commitments", label: "Commitments", icon: Handshake },
  { id: "decisions", label: "Decisions", icon: Target },
  { id: "search", label: "Search", icon: Search },
  { id: "qa", label: "Ask", icon: MessageCircle },
  { id: "clients", label: "Clients", icon: LayoutDashboard },
  { id: "engagements", label: "Engagements", icon: FileSpreadsheet },
  { id: "insights", label: "Insights", icon: BarChart3 },
  { id: "prep-brief", label: "Prep Brief", icon: Sparkles },
];

export default function Home() {
  const [backendReady, setBackendReady] = useState(false);
  const [nav, setNav] = useState<string>("record");
  // Opt-in "Today" daily-briefing tab. Hidden + skipped as landing view
  // unless the user enables it in Settings. Persisted server-side.
  const [todayEnabled, setTodayEnabled] = useState(false);
  // Auto-land on Today only ONCE per app launch (when enabled) — so the
  // periodic settings refresh on window focus doesn't yank the user back
  // to Today after they've navigated elsewhere.
  const didInitialLandRef = useRef(false);
  // First-run guided tour. Opens automatically on a true first run (no
  // usable AI key AND no sessions) unless previously dismissed; also
  // re-openable from the Help tab.
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const onboardingCheckedRef = useRef(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  // Configured clients (from client_configs.json). Merged into
  // existingClients so the Sessions detail dialog's client picker also
  // surfaces clients that were created but haven't been tagged on a
  // session yet. Refreshed alongside sessions.
  const [clientConfigs, setClientConfigs] = useState<Record<string, { export_folder: string; display_name?: string }>>({});
  const [storage, setStorage] = useState<{
    total_bytes: number;
    session_count: number;
    wav_count: number;
  } | null>(null);
  const [notifyMinutes, setNotifyMinutes] = useState(0);
  const [detailSessionId, setDetailSessionId] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailInitialTab, setDetailInitialTab] = useState("overview");

  // Calendar state lives here (not in RecordView) so switching nav and
  // coming back doesn't drop the already-loaded meetings. RecordView
  // unmounts on nav change; anything local to it is lost. The user
  // noticed the list "flashing away" — that was the empty-initial render
  // on remount before the re-fetch completed.
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [meetingsLoading, setMeetingsLoading] = useState(false);

  const openSession = (id: string, tab: string = "overview") => {
    setDetailSessionId(id);
    setDetailInitialTab(tab);
    setDetailOpen(true);
  };

  const existingClients = (() => {
    const seen = new Map<string, string>();
    for (const s of sessions) {
      const name = (s.client || "").trim();
      if (!name) continue;
      const key = name.toLowerCase();
      if (!seen.has(key)) seen.set(key, name);
    }
    for (const cfg of Object.values(clientConfigs)) {
      const name = (cfg.display_name || "").trim();
      if (!name) continue;
      const key = name.toLowerCase();
      if (!seen.has(key)) seen.set(key, name);
    }
    return Array.from(seen.values()).sort((a, b) => a.localeCompare(b));
  })();
  // Scope projects to the client they were tagged under so the detail
  // dialog's Project dropdown doesn't offer a project from a different
  // customer (which would silently mis-tag the session on save).
  const projectsByClient: Record<string, string[]> = {};
  for (const s of sessions) {
    const c = (s.client || "").trim().toLowerCase();
    const p = (s.project || "").trim();
    if (!c || !p) continue;
    if (!projectsByClient[c]) projectsByClient[c] = [];
    if (!projectsByClient[c].includes(p)) projectsByClient[c].push(p);
  }
  for (const k of Object.keys(projectsByClient)) projectsByClient[k].sort();

  const [backendAttempts, setBackendAttempts] = useState(0);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [appVersion, setAppVersion] = useState<string>("");
  const [pipelineStatus, setPipelineStatus] = useState<{
    loading: boolean; text: string;
  }>({ loading: false, text: "" });
  // Mirrored from /recording/status so the sidebar can show a
  // persistent "Recording…" badge on every tab — the previous design
  // only surfaced recording state inside the Record view, so when
  // auto-record fired silently the user had no visible cue and ended
  // up colliding with their own manual Start.
  const [recordingNow, setRecordingNow] = useState<{
    isRecording: boolean;
    sessionId: string | null;
    startedAt: string | null;
    autoSubject: string | null;
  }>({ isRecording: false, sessionId: null, startedAt: null, autoSubject: null });
  // Dedup keys: only fire the auto-record toast/notification ONCE per
  // session, and only show each unique skip-reason once.
  const lastAutoSessionRef = useRef<string | null>(null);
  const lastSkipReasonRef = useRef<string | null>(null);
  // Live timer for the recording badge. Ticks every second while a
  // recording is active so the badge label shows real elapsed time.
  const [recordingElapsedS, setRecordingElapsedS] = useState(0);

  // App-wide external-link handler. In a Tauri webview a plain
  // <a target="_blank"> never reaches the system browser, so EVERY
  // external link in the app (Join meeting, Settings/API console
  // links, Usage Guide, etc.) silently did nothing in the packaged
  // build. One delegated listener routes any http(s) anchor click
  // through the real OS opener — no per-component changes needed.
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0) return;
      const a = (e.target as HTMLElement | null)?.closest?.("a");
      const href = a?.getAttribute("href") || "";
      if (/^https?:\/\//i.test(href)) {
        e.preventDefault();
        openExternal(href);
      }
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  // Pull the installed Tauri app version so the sidebar shows the real
  // build number instead of a stale hardcoded string.
  useEffect(() => {
    (async () => {
      try {
        const { getVersion } = await import("@tauri-apps/api/app");
        const v = await getVersion();
        setAppVersion(v);
      } catch {
        // Not running under Tauri (e.g. `next dev` in a browser) — keep blank.
      }
    })();
  }, []);

  // Quiet check-for-updates on app launch. If a newer release exists on
  // GitHub, surface a single non-blocking toast with a Download action
  // that opens the GitHub release page in the default browser. Failures
  // (no network, GitHub rate-limit, deleted repo) collapse silently —
  // we don't want to nag users about transient issues at startup. The
  // full UI lives in Settings → App Updates; this is just the "nudge."
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { checkForUpdate, downloadUpdate } = await import("@/lib/updater");
        const result = await checkForUpdate();
        if (cancelled) return;
        if (result.kind === "available") {
          toast.message(`Update available: v${result.release.version}`, {
            description: "Click Download to grab the installer for your OS.",
            duration: 12000,
            action: {
              label: "Download",
              onClick: () => { downloadUpdate(result.release); },
            },
          });
        }
      } catch {
        // Silent.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Poll /recording/status so the sidebar can show what the backend is
  // currently doing — model warmup on cold start, transcription progress
  // during processing, etc. The user wanted visible feedback instead of
  // silent delays where the app looked frozen or unresponsive.
  useEffect(() => {
    if (!backendReady) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const s = await api.recordingStatus();
        if (cancelled) return;
        if (s.models_loading) {
          setPipelineStatus({ loading: true, text: "Loading AI models…" });
        } else if (s.current_status && !s.is_recording) {
          setPipelineStatus({ loading: true, text: s.current_status });
        } else {
          setPipelineStatus({ loading: false, text: "" });
        }

        // Mirror the recording state so the persistent badge can
        // render on every tab — not just inside the Record view.
        setRecordingNow({
          isRecording: !!s.is_recording,
          sessionId: s.session_id ?? null,
          startedAt: s.started_at ?? null,
          autoSubject: s.auto_record_subject ?? null,
        });

        // Auto-record start — fire native + in-app notification ONCE
        // per session_id so the user can't miss that auto-record fired
        // (the original complaint: it started, no visual cue at all).
        if (
          s.is_recording &&
          s.auto_record_subject &&
          s.session_id &&
          lastAutoSessionRef.current !== s.session_id
        ) {
          lastAutoSessionRef.current = s.session_id;
          const subject = s.auto_record_subject;
          toast.message("Auto-recording started", { description: subject });
          void (async () => {
            try {
              const { sendNotification, isPermissionGranted, requestPermission } =
                await import("@tauri-apps/plugin-notification");
              let granted = await isPermissionGranted();
              if (!granted) granted = (await requestPermission()) === "granted";
              if (granted) {
                await sendNotification({
                  title: "Auto-recording started", body: subject,
                });
              }
            } catch { /* not Tauri */ }
          })();
        }

        // One-shot skip reason (backend clears after one read).
        // Surfaces "no mic configured" etc. so silent failure can't
        // happen the way it did before.
        if (
          s.auto_record_skip_reason &&
          s.auto_record_skip_reason !== lastSkipReasonRef.current
        ) {
          const reason = s.auto_record_skip_reason;
          lastSkipReasonRef.current = reason;
          toast.warning(reason, { duration: 10000 });
          void (async () => {
            try {
              const { sendNotification, isPermissionGranted, requestPermission } =
                await import("@tauri-apps/plugin-notification");
              let granted = await isPermissionGranted();
              if (!granted) granted = (await requestPermission()) === "granted";
              if (granted) {
                await sendNotification({
                  title: "Auto-record skipped", body: reason,
                });
              }
            } catch { /* not Tauri */ }
          })();
        }
      } catch {
        // Backend unreachable — don't overwrite any message in flight.
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [backendReady]);

  // Drives the elapsed timer next to the persistent "● Recording"
  // badge. 1s tick is plenty for a wall-clock label; the heavy poll
  // above stays on its 2s cadence.
  useEffect(() => {
    if (!recordingNow.isRecording || !recordingNow.startedAt) {
      setRecordingElapsedS(0);
      return;
    }
    const startMs = Date.parse(recordingNow.startedAt);
    if (Number.isNaN(startMs)) return;
    const update = () => setRecordingElapsedS(
      Math.max(0, Math.floor((Date.now() - startMs) / 1000)),
    );
    update();
    const id = setInterval(update, 1000);
    return () => clearInterval(id);
  }, [recordingNow.isRecording, recordingNow.startedAt]);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    // Max startup wait. Cold-starts on corporate laptops where every
    // torch/pyannote DLL hits the AV scanner can take 2-3 minutes on
    // first launch. After that the OS page cache makes launches fast.
    // We'd rather keep retrying quietly than show a false "failed"
    // screen to users who just need to wait.
    const MAX_ATTEMPTS = 240; // 4 minutes
    const check = async () => {
      try {
        await api.health();
        if (!cancelled) setBackendReady(true);
      } catch {
        if (cancelled) return;
        attempts += 1;
        setBackendAttempts(attempts);
        if (attempts >= MAX_ATTEMPTS) {
          setBackendError(
            `Backend failed to start after ${MAX_ATTEMPTS} seconds. ` +
            "Click Retry — if the backend finished starting while this " +
            "screen was up, it will come online immediately. Otherwise " +
            "check %APPDATA%\\MeetingRecorder\\backend.log and rust.log."
          );
        } else {
          setTimeout(check, 1000);
        }
      }
    };
    check();
    return () => { cancelled = true; };
  }, []);

  const reloadSessions = useCallback(async () => {
    try {
      const [s, stats, settings, cfgs] = await Promise.all([
        api.listSessions(),
        api.getRetentionStats().catch(() => null),
        api.getSettings().catch(() => null),
        api.getClientConfigs().catch(() => ({} as Record<string, { export_folder: string; display_name?: string }>)),
      ]);
      setSessions(s);
      setStorage(stats);
      setClientConfigs(cfgs);
      if (settings) {
        setNotifyMinutes(settings.notify_minutes_before);
        const enabled = !!settings.today_view_enabled;
        setTodayEnabled(enabled);
        // First settings resolution after launch: if Today is enabled,
        // make it the landing view. Guarded by the ref so re-fetches on
        // focus don't re-navigate the user mid-session.
        if (enabled && !didInitialLandRef.current) {
          setNav("today");
        }
        didInitialLandRef.current = true;

        // First-run detection: no usable AI key AND no recordings yet →
        // this is a fresh install. Auto-open the guided tour once (unless
        // the user already dismissed it). Checked a single time per launch.
        if (!onboardingCheckedRef.current) {
          onboardingCheckedRef.current = true;
          const noKey = !settings.is_configured;
          if (noKey && s.length === 0 && !onboardingDismissed()) {
            setOnboardingOpen(true);
          }
        }
      }
    } catch (e) {
      console.error(e);
    }
  }, []);

  // Sessions carry the AI-extracted fields (summary / action_items /
  // decisions) that the Follow-ups & Decisions tabs parse. Those tabs
  // read the page-level `sessions`, so without a refresh they show
  // whatever was loaded at startup — a call that auto-processed in the
  // background wouldn't appear until a full reload. Re-pull sessions
  // when the user opens one of those tabs, and when the app window
  // regains focus (e.g. tabbing back after a call finished processing).
  useEffect(() => {
    if (!backendReady) return;
    if (
      nav === "sessions" || nav === "follow-ups"
      || nav === "decisions" || nav === "clients"
      || nav === "engagements"
    ) {
      reloadSessions();
    }
  }, [nav, backendReady, reloadSessions]);

  useEffect(() => {
    if (!backendReady) return;
    let last = 0;
    const refresh = () => {
      // Debounce: focus + visibilitychange can both fire on one tab-back.
      const now = Date.now();
      if (now - last < 10_000) return;
      last = now;
      reloadSessions();
    };
    const onVis = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [backendReady, reloadSessions]);

  // Calendar loader. Kept here so the list survives nav switches. On
  // errors or empty responses from a transient COM hiccup, we preserve
  // whatever meetings we already have rather than blanking the UI —
  // that was the root of the "flash away, need to refresh" behavior.
  const reloadCalendar = useCallback(async (opts?: {
    force?: boolean;
    announce?: boolean;
    // `silent=true` for background refreshes (focus-triggered): keep the
    // existing list on empty/failure so an Outlook hiccup doesn't make
    // the panel flash blank while the user is just tabbing around.
    silent?: boolean;
  }) => {
    const force = !!opts?.force;
    const announce = !!opts?.announce;
    const silent = !!opts?.silent;
    setMeetingsLoading(true);
    try {
      const cal = await api.getUpcomingMeetings(168, force);
      setMeetings((prev) => {
        // Backend returned nothing but we already had meetings — keep
        // the prior list. Specifically guards the Outlook-COM-timeout
        // path that returns [] after 15s. For silent refreshes we always
        // preserve; for explicit Refresh clicks we only preserve when
        // the request wasn't forced (i.e. cache-hit empty).
        if (cal.length === 0 && prev.length > 0 && (silent || !force)) {
          return prev;
        }
        return cal;
      });
      if (announce) toast.success(`Loaded ${cal.length} upcoming meetings`);
    } catch (e) {
      if (announce) {
        toast.error(`Calendar: ${e instanceof Error ? e.message : e}`);
      }
      // don't clobber an existing list on transient failure
    } finally {
      setMeetingsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (backendReady) {
      reloadSessions();
      reloadCalendar();
    }
  }, [backendReady, reloadCalendar]);

  // If Today gets disabled (in Settings) while the user is sitting on it,
  // bounce them to Record so they're not stranded on a now-hidden tab.
  useEffect(() => {
    if (!todayEnabled && nav === "today") setNav("record");
  }, [todayEnabled, nav]);

  // Auto pre-meeting brief notifications. The backend loop generates
  // briefs before meetings and flags un-notified ones; we poll, fire a
  // native "prep brief ready" toast, and mark them notified so each
  // fires once. Fully backend-driven generation — this only surfaces it.
  useEffect(() => {
    if (!backendReady) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const pending = await api.getPendingAutoPrepBriefs();
        if (cancelled || !pending.length) return;
        for (const b of pending) {
          try {
            const { sendNotification, isPermissionGranted, requestPermission } =
              await import("@tauri-apps/plugin-notification");
            let granted = await isPermissionGranted();
            if (!granted) granted = (await requestPermission()) === "granted";
            if (granted) {
              await sendNotification({
                title: "Prep brief ready",
                body: `${b.subject} — starts in ~${b.minutes_before ?? "a few"} min. Brief is ready in the app.`,
              });
            }
          } catch {
            /* plugin unavailable (dev browser) — in-app toast still fires */
          }
          toast.info(`Prep brief ready: ${b.subject}`, {
            description: "Generated from your prior sessions with this client.",
          });
          await api.markAutoPrepBriefNotified(b.key).catch(() => {});
        }
      } catch {
        /* transient — try again next tick */
      }
    };
    poll();
    const id = setInterval(poll, 60_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [backendReady]);

  // Nav list with the opt-in Today tab prepended only when enabled.
  const navItems = todayEnabled
    ? [{ id: "today", label: "Today", icon: Sun }, ...NAV_ITEMS]
    : NAV_ITEMS;

  // Notification system — polls /sessions/unprocessed every 60s and fires
  // a Windows toast the first time a new unprocessed session appears. The
  // count populates the sidebar badge on the Sessions nav item.
  const { count: unprocessedCount } = useUnprocessedSessions(backendReady);

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
                • A stale backend process is holding the app&apos;s port (it&apos;s chosen automatically at startup)
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
            {backendAttempts > 5 && backendAttempts <= 30 && (
              <div className="text-xs text-muted-foreground max-w-xs text-center">
                Loading torch + pyannote models. Takes 10-30s on warm cache,
                up to 2-3 minutes on first launch after install while Windows
                Defender scans the runtime. ({backendAttempts}s elapsed)
              </div>
            )}
            {backendAttempts > 30 && (
              <div className="text-xs text-muted-foreground max-w-xs text-center">
                Still starting — this is normal on corporate laptops where
                antivirus scans each DLL on first access. Hang tight.
                ({backendAttempts}s elapsed)
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
          <div className="flex flex-col leading-tight min-w-0 flex-1">
            <span className="text-sm font-semibold">Meeting Recorder</span>
            <span className="text-[10px] text-muted-foreground">
              {appVersion ? `v${appVersion}` : "v2"}
            </span>
          </div>
        </div>
        {pipelineStatus.loading && (
          <div
            className="flex items-center gap-2 border-b border-border bg-accent/30 px-4 py-2 text-xs text-foreground"
            title={pipelineStatus.text}
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0 text-primary" />
            <span className="truncate">{pipelineStatus.text}</span>
          </div>
        )}

        {recordingNow.isRecording && (
          // Two affordances in one strip: clicking the body opens the
          // Record view; clicking the dedicated Stop button halts the
          // recording from anywhere in the app. CRITICAL — without
          // the in-strip stop, the only way to halt an auto-record
          // was to navigate to Record view, and a UI-state race could
          // hide the stop button there (the 4h17m orphan-record
          // incident traced partly to this).
          <div
            className="flex items-stretch border-b border-border bg-red-500/10 text-xs text-foreground"
            title={recordingNow.autoSubject
              ? `Auto-recording: ${recordingNow.autoSubject}`
              : "Recording in progress"}
          >
            <button
              type="button"
              onClick={() => setNav("record")}
              className="flex items-center gap-2 px-4 py-2 hover:bg-red-500/15 transition-colors flex-1 min-w-0 text-left"
              title="Open the Record view"
            >
              <span className="relative inline-flex h-2.5 w-2.5 shrink-0">
                <span className="absolute inset-0 inline-flex h-full w-full animate-ping rounded-full bg-red-500 opacity-75" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-red-500" />
              </span>
              <span className="truncate flex-1">
                {recordingNow.autoSubject
                  ? `Auto-recording: ${recordingNow.autoSubject}`
                  : "Recording…"}
              </span>
              <span className="font-mono text-[11px] text-muted-foreground shrink-0">
                {(() => {
                  const s = recordingElapsedS;
                  const h = Math.floor(s / 3600);
                  const m = Math.floor((s % 3600) / 60);
                  const ss = s % 60;
                  const pad = (n: number) => String(n).padStart(2, "0");
                  return h ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
                })()}
              </span>
            </button>
            <button
              type="button"
              onClick={async (e) => {
                e.stopPropagation();
                try {
                  await api.stopRecording();
                  toast.success("Recording stopped");
                } catch (err) {
                  toast.error(`Couldn't stop: ${err instanceof Error ? err.message : err}`);
                }
              }}
              className="flex items-center justify-center px-3 border-l border-red-500/30 hover:bg-red-500/25 transition-colors text-red-700 dark:text-red-300 font-medium"
              title="Stop recording"
              aria-label="Stop recording"
            >
              <span className="h-2.5 w-2.5 rounded-[2px] bg-red-600 dark:bg-red-400" />
              <span className="ml-1.5 text-[11px]">Stop</span>
            </button>
          </div>
        )}

        <nav className="flex-1 overflow-y-auto p-3">
          <div className="mb-2 px-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Workspace
          </div>
          <ul className="space-y-0.5">
            {navItems.map((item) => {
              const Icon = item.icon;
              const active = nav === item.id;
              const badge = item.id === "sessions" && unprocessedCount > 0
                ? unprocessedCount
                : 0;
              return (
                <li key={item.id}>
                  <button
                    onClick={() => setNav(item.id)}
                    className={`flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors ${
                      active
                        ? "bg-accent text-accent-foreground font-medium"
                        : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
                    }`}
                    title={badge > 0
                      ? `${badge} session${badge === 1 ? "" : "s"} awaiting processing`
                      : undefined}
                  >
                    <Icon className="h-4 w-4" />
                    <span className="flex-1 text-left">{item.label}</span>
                    {badge > 0 && (
                      <span className="inline-flex items-center justify-center min-w-5 h-5 px-1.5 rounded-full bg-primary text-primary-foreground text-[10px] font-semibold">
                        {badge}
                      </span>
                    )}
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
      <main className="flex flex-1 flex-col overflow-hidden min-w-0">
        <header className="flex h-16 items-center justify-between border-b border-border bg-background/80 px-6 backdrop-blur shrink-0">
          <div>
            <h1 className="text-lg font-semibold capitalize">{nav.replace("-", " ")}</h1>
            <p className="text-xs text-muted-foreground">
              {nav === "today" && "Your daily briefing — top priority, agenda, action items, FYI"}
              {nav === "record" && "Start a new recording or pick one from your calendar"}
              {nav === "sessions" && "Browse every meeting you've recorded"}
              {nav === "follow-ups" && "Track action items across every meeting"}
              {nav === "commitments" && "Every promise made in your meetings — who owes what, by when, status"}
              {nav === "decisions" && "Every decision, auto-generated ADR log"}
              {nav === "search" && "Search across all transcripts"}
              {nav === "qa" && "Ask Claude questions about your meetings — answers come with citations"}
              {nav === "clients" && "Clients and their projects — drill in to see meetings"}
              {nav === "engagements" && "Per-client engagement register — requirements, decisions, actions & open questions rolled up across every meeting, exportable to Excel"}
              {nav === "insights" && "Cross-meeting analytics — time allocation, recurring topics, open loops"}
              {nav === "prep-brief" && "Generate a pre-meeting brief from past sessions"}
              {nav === "settings" && "Configure API keys, devices, and workflow"}
              {nav === "help" && "How to use Meeting Recorder"}
            </p>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto overflow-x-hidden p-8 min-h-0">
          {nav === "today" && todayEnabled && <TodayView onNavigate={setNav} />}
          {nav === "record" && (
            <RecordView
              onSessionsChanged={reloadSessions}
              onOpenSession={openSession}
              meetings={meetings}
              meetingsLoading={meetingsLoading}
              onRefreshCalendar={(silent) => reloadCalendar({
                force: true,
                announce: !silent,
                silent: !!silent,
              })}
            />
          )}
          {nav === "sessions" && (
            <SessionsView sessions={sessions} onReload={reloadSessions} onOpenSession={openSession} />
          )}
          {nav === "follow-ups" && (
            <FollowUpsView sessions={sessions} onOpenSession={openSession} />
          )}
          {nav === "commitments" && <CommitmentsView onOpenSession={openSession} />}
          {nav === "decisions" && (
            <DecisionsView sessions={sessions} onOpenSession={openSession} />
          )}
          {nav === "search" && <SearchView onOpenSession={openSession} />}
          {nav === "qa" && <QAView onOpenSession={openSession} />}
          {nav === "clients" && (
            <ClientsView sessions={sessions} onReload={reloadSessions} onOpenSession={openSession} />
          )}
          {nav === "engagements" && <EngagementView sessions={sessions} />}
          {nav === "insights" && (
            <InsightsView
              onOpenSession={openSession}
              existingClients={existingClients}
            />
          )}
          {nav === "prep-brief" && <PrepBriefView sessions={sessions} meetings={meetings} />}
          {nav === "settings" && <SettingsView onSaved={reloadSessions} />}
          {nav === "help" && <UsageGuideView onLaunchSetup={() => setOnboardingOpen(true)} />}
        </div>
      </main>

      <SessionDetailDialog
        sessionId={detailSessionId}
        open={detailOpen}
        onOpenChange={(o) => { setDetailOpen(o); if (!o) setDetailSessionId(null); }}
        onChanged={reloadSessions}
        initialTab={detailInitialTab}
        existingClients={existingClients}
        projectsByClient={projectsByClient}
      />

      <OnboardingTour
        open={onboardingOpen}
        onClose={() => setOnboardingOpen(false)}
        onNavigate={(id) => { setOnboardingOpen(false); setNav(id); }}
      />
    </div>
  );
}
