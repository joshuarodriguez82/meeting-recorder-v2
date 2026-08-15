"use client";

import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { toast } from "sonner";
import { api, formatBytes, openExternal, type Meeting, type SessionSummary } from "@/lib/api";
import { refreshRecordingStatus, useRecordingStatus } from "@/lib/recording-status";
import {
  Mic, History, CheckSquare, Target, Search,
  LayoutDashboard, Settings as SettingsIcon, HelpCircle, Loader2,
  Sparkles, MessageCircle, Handshake, BarChart3, FileSpreadsheet,
  Sun, PanelLeftClose, PanelLeftOpen, CheckCircle2,
} from "lucide-react";
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from "@/components/ui/tooltip";
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
import { ViewErrorBoundary } from "@/components/view-error-boundary";

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

// localStorage key for the nav rail's expanded/collapsed choice.
const NAV_COLLAPSED_KEY = "navCollapsed";

/**
 * One row of the nav rail. Extracted so the expanded and collapsed
 * variants can't drift apart, and defined at module scope (not inside
 * Home) so it isn't re-created every render — an inline component would
 * remount the button on each parent render and drop keyboard focus.
 *
 * In collapsed/icon mode the label moves into a Tooltip and the
 * unprocessed-session badge rides the icon's top-right corner instead of
 * disappearing. The active pill (bg-accent) is identical in both modes,
 * so "where am I" survives the collapse. Design review 2026-08-11.
 */
function RailButton({
  icon: Icon, label, active, collapsed, badge = 0, hint, onClick,
}: {
  icon: React.ElementType;
  label: string;
  active: boolean;
  collapsed: boolean;
  badge?: number;
  hint?: string;
  onClick: () => void;
}) {
  const button = (
    <button
      onClick={onClick}
      className={`relative flex w-full items-center rounded-xl text-sm transition-colors ${
        collapsed ? "justify-center px-0 py-2.5" : "gap-3 px-3 py-2.5"
      } ${
        active
          ? "bg-accent text-accent-foreground font-medium"
          : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
      }`}
      // Collapsed mode has no visible text, so the accessible name has
      // to come from aria-label; expanded mode keeps the original
      // title-attribute behaviour for the badge explanation.
      title={collapsed ? undefined : hint}
      aria-label={collapsed ? label : undefined}
    >
      <Icon className="h-5 w-5 shrink-0" />
      {!collapsed && <span className="flex-1 text-left">{label}</span>}
      {badge > 0 && (collapsed ? (
        <span className="absolute top-1 right-1.5 inline-flex items-center justify-center min-w-4 h-4 px-1 rounded-full bg-primary text-primary-foreground text-[9px] font-semibold">
          {badge}
        </span>
      ) : (
        <span className="inline-flex items-center justify-center min-w-5 h-5 px-1.5 rounded-full bg-primary text-primary-foreground text-[10px] font-semibold">
          {badge}
        </span>
      ))}
    </button>
  );

  if (!collapsed) return button;
  return (
    <Tooltip>
      <TooltipTrigger render={button} />
      <TooltipContent side="right">
        {hint ? `${label} — ${hint}` : label}
      </TooltipContent>
    </Tooltip>
  );
}

export default function Home() {
  const [backendReady, setBackendReady] = useState(false);
  const [nav, setNav] = useState<string>("record");
  // Nav rail expanded (icons + labels) vs. collapsed (icon-only).
  // Persisted so the choice survives an app restart. The initial state
  // is hard-coded to `false` and the stored value is applied in an
  // effect on purpose: this app ships as a Next.js STATIC EXPORT, so the
  // first render happens during prerender where `window`/`localStorage`
  // don't exist — reading storage in the useState initializer (or at
  // module scope) would throw at build time and hydrate-mismatch at
  // runtime. Design review 2026-08-11.
  // One scroll container is shared by every view — the views swap inside
  // it rather than each owning its own scroller. That's what keeps the
  // header fixed, but it also means scrollTop SURVIVES a nav change:
  // scroll halfway down Record, switch to Sessions, and Sessions opens
  // halfway down too (field report 2026-08-11).
  //
  // Reset on every nav change. Not `behavior: smooth` — a new view
  // should already be at the top when it paints, not visibly race
  // upward after it.
  const mainScrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    mainScrollRef.current?.scrollTo({ top: 0 });
  }, [nav]);

  const [navCollapsed, setNavCollapsed] = useState(false);
  useEffect(() => {
    // Deferred to a microtask so this reads as "subscribe to an external
    // system after mount" rather than a synchronous cascading setState
    // in the effect body (react-hooks/set-state-in-effect).
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (cancelled) return;
      try {
        setNavCollapsed(localStorage.getItem(NAV_COLLAPSED_KEY) === "1");
      } catch {
        /* storage unavailable (private mode / blocked) — stay expanded */
      }
    });
    return () => { cancelled = true; };
  }, []);
  const toggleNavCollapsed = useCallback(() => {
    setNavCollapsed((v) => {
      const next = !v;
      try { localStorage.setItem(NAV_COLLAPSED_KEY, next ? "1" : "0"); }
      catch { /* storage unavailable — the toggle still works this session */ }
      return next;
    });
  }, []);
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
  // THE single shared recording poll (src/lib/recording-status.ts).
  // The sidebar badge below and the Record view read this exact same
  // object — field report 2026-08-11 was the two of them holding
  // separate copies and drifting apart after a failed Stop.
  const { status: recordingStatus, reachable, consecutiveFailures } =
    useRecordingStatus();
  // Derived, not stored. There is no local "am I recording" state to
  // fall out of date: the badge renders whatever the last successful
  // poll said, full stop.
  const recordingNow = useMemo(() => ({
    isRecording: !!recordingStatus?.is_recording,
    sessionId: recordingStatus?.session_id ?? null,
    startedAt: recordingStatus?.started_at ?? null,
    autoSubject: recordingStatus?.auto_record_subject ?? null,
  }), [recordingStatus]);
  // Sidebar pipeline message: shown WITHOUT a spinner once nothing is
  // actually in flight, and cleared a few seconds after the backend
  // goes idle so the sidebar returns to a clean state instead of
  // parking a stale sentence forever. `current_status` on the backend
  // is a write-once mailbox that's never cleared (see api.ts), so this
  // is the thing that makes "Processing complete." eventually go away
  // — the backend's `is_processing` flag is what stops the SPINNER
  // immediately (that's the actual bug fix; this is polish on top of
  // it).
  //
  // Implementation note: state is only ever set from inside a
  // setTimeout callback (never synchronously in the effect body) —
  // scheduling, not deciding, happens in the effect. `hiddenStatusText`
  // just remembers "this exact string has already been shown its full
  // 8s and should fade"; the visible text is derived in `pipelineStatus`
  // below by comparing the live current_status against it.
  //
  // Guarded against clearing a message that belongs to newly-started
  // work two ways: (1) while `is_processing` is true, pipelineStatus
  // below reads current_status directly and never consults
  // `hiddenStatusText` at all, so an in-progress message can never be
  // faded out from under itself; (2) the instant work starts, any
  // pending fade timer is cancelled and the memory of what was hidden
  // is cleared, so a later message can't be mistaken for an
  // already-faded one just because the text happens to repeat.
  const [hiddenStatusText, setHiddenStatusText] = useState("");
  const fadeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const text = recordingStatus?.current_status || "";
    const busy = !!recordingStatus?.is_processing;
    if (fadeTimerRef.current) {
      clearTimeout(fadeTimerRef.current);
      fadeTimerRef.current = null;
    }
    if (busy || !text) {
      // Busy: nothing to fade yet — pipelineStatus shows the live text
      // unconditionally while is_processing is true. Empty: nothing to
      // show at all. Either way, forget any stale "hidden" memory so a
      // later message isn't mistaken for one already faded.
      fadeTimerRef.current = setTimeout(() => setHiddenStatusText(""), 0);
      return;
    }
    fadeTimerRef.current = setTimeout(() => setHiddenStatusText(text), 8000);
  }, [recordingStatus?.current_status, recordingStatus?.is_processing]);
  useEffect(() => () => {
    if (fadeTimerRef.current) clearTimeout(fadeTimerRef.current);
  }, []);

  // Sidebar pipeline line: model warmup on cold start, transcription
  // progress during processing, or the reconnect notice while a
  // respawned backend boots. 2+ consecutive failures before we say
  // anything, so a single dropped request during a normal respawn
  // doesn't flash a scary banner.
  //
  // `loading` (spin the dial) is driven by GENUINE activity only:
  // backend unreachable, models loading, or the backend's real
  // `is_processing` in-flight signal. A `current_status` string with no
  // in-flight work behind it still shows as text (until it fades, via
  // hiddenStatusText above) but WITHOUT the spinner — that's the fix
  // for the spinner that used to run forever after "Processing
  // complete." landed.
  const pipelineStatus = useMemo<{ loading: boolean; text: string }>(() => {
    if (!reachable && consecutiveFailures >= 2) {
      return {
        loading: true,
        text: "Reconnecting to backend… (it restarted — one moment)",
      };
    }
    if (recordingStatus?.models_loading) {
      return { loading: true, text: "Loading AI models…" };
    }
    if (recordingStatus?.is_processing && recordingStatus?.current_status) {
      return { loading: true, text: recordingStatus.current_status };
    }
    const text = recordingStatus?.current_status || "";
    if (text && text !== hiddenStatusText) {
      return { loading: false, text };
    }
    return { loading: false, text: "" };
  }, [recordingStatus, reachable, consecutiveFailures, hiddenStatusText]);
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

  // Auto-record announcements, driven off the shared status rather than
  // a poll of our own. Everything this effect used to do that was about
  // *tracking* recording state now happens in
  // src/lib/recording-status.ts (including reconciling the Tauri
  // shell's RECORDING_ACTIVE flag on every successful poll); what's
  // left here is purely the one-shot user-facing announcements, which
  // are page-level concerns.
  useEffect(() => {
    const s = recordingStatus;
    if (!s) return;

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
  }, [recordingStatus]);

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

  // FINALIZE-IN-PROGRESS live pickup (field repro 2026-08-14): a session
  // whose finalize subprocess is still running (echo cancellation can
  // push this to several minutes) needs the Sessions list — and any
  // session-detail dialog reading from it — to notice the moment it
  // completes, without the user having to manually refresh or tab away
  // and back. Reuses the SAME `reloadSessions` every other refresh path
  // here already calls; the only new thing is an adaptive interval
  // (same idea as src/lib/recording-status.ts's RECORDING_POLL_INTERVAL_MS
  // — poll faster only while something time-sensitive is actually
  // happening) that arms itself only while at least one loaded session
  // is finalizing, and tears itself down the moment none are.
  // Includes "queued" (2026-08 serialized finalize — at most one
  // finalize subprocess runs process-wide, see backend utils/
  // finalize_gate.py) alongside "finalizing": a session waiting behind
  // another finalize job needs this same live-pickup polling just as
  // much as one actually running, so it doesn't sit un-refreshed for
  // however long the job ahead of it takes.
  const anyFinalizing = sessions.some(
    (s) => s.finalize_status === "finalizing" || s.finalize_status === "queued");
  useEffect(() => {
    if (!backendReady || !anyFinalizing) return;
    const id = setInterval(reloadSessions, 8_000);
    return () => clearInterval(id);
  }, [backendReady, anyFinalizing, reloadSessions]);

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
      {/* Sidebar. Width is the only structural difference between the two
          modes — every control below stays mounted and usable in icon
          mode, just abbreviated. */}
      <TooltipProvider>
      <aside
        className={`flex h-full shrink-0 flex-col border-r border-border bg-sidebar transition-[width] duration-200 ${
          navCollapsed ? "w-16" : "w-64"
        }`}
      >
        <div
          className={`flex h-16 items-center border-b border-border ${
            navCollapsed ? "justify-center px-2" : "gap-2.5 px-4"
          }`}
        >
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <Mic className="h-4 w-4" />
          </div>
          {!navCollapsed && (
            <div className="flex flex-col leading-tight min-w-0 flex-1">
              <span className="text-sm font-semibold">Meeting Recorder</span>
              <span className="text-[10px] text-muted-foreground">
                {appVersion ? `v${appVersion}` : "v2"}
              </span>
            </div>
          )}
        </div>
        {pipelineStatus.text && (
          // Icon mode drops the status text but keeps the icon (and the
          // full text in the title attribute) — the user still sees a
          // signal rather than losing it entirely. Spinner only while
          // `loading` is true (genuine backend activity — see
          // pipelineStatus above); once work finishes this still shows
          // the last message as plain text with a static checkmark
          // instead of a spinner that never stops.
          <div
            className={`flex items-center border-b border-border py-2 text-xs ${
              pipelineStatus.loading
                ? "bg-accent/30 text-foreground"
                : "bg-muted/30 text-muted-foreground"
            } ${navCollapsed ? "justify-center px-2" : "gap-2 px-4"}`}
            title={pipelineStatus.text}
          >
            {pipelineStatus.loading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0 text-primary" />
            ) : (
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
            )}
            {!navCollapsed && <span className="truncate">{pipelineStatus.text}</span>}
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
          // Icon mode stacks the two affordances vertically instead of
          // dropping either one — the Stop button in particular must
          // stay reachable from every tab in BOTH widths.
          <div
            className={`flex border-b border-border bg-red-500/10 text-xs text-foreground ${
              navCollapsed ? "flex-col items-stretch" : "items-stretch"
            }`}
            title={recordingNow.autoSubject
              ? `Auto-recording: ${recordingNow.autoSubject}`
              : "Recording in progress"}
          >
            <button
              type="button"
              onClick={() => setNav("record")}
              className={`flex items-center hover:bg-red-500/15 transition-colors min-w-0 ${
                navCollapsed
                  ? "flex-col justify-center gap-1 px-1 py-2"
                  : "gap-2 px-4 py-2 flex-1 text-left"
              }`}
              title="Open the Record view"
            >
              <span className="relative inline-flex h-2.5 w-2.5 shrink-0">
                <span className="absolute inset-0 inline-flex h-full w-full animate-ping rounded-full bg-red-500 opacity-75" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-red-500" />
              </span>
              {!navCollapsed && (
                <span className="truncate flex-1">
                  {recordingNow.autoSubject
                    ? `Auto-recording: ${recordingNow.autoSubject}`
                    : "Recording…"}
                </span>
              )}
              <span
                className={`font-mono text-muted-foreground shrink-0 ${
                  navCollapsed ? "text-[9px] leading-none" : "text-[11px]"
                }`}
              >
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
                  // Field report 2026-08-11: Stop pressed while the
                  // backend was restarting. We deliberately do NOT
                  // clear the badge here — the backend may still be
                  // capturing, and guessing is what let the sidebar
                  // and the Record view disagree. The shared poll
                  // reconciles to the server's answer as soon as it's
                  // reachable again, so say that out loud instead of
                  // leaving the user staring at an indicator they just
                  // tried to dismiss.
                  toast.error(
                    `Couldn't stop: ${err instanceof Error ? err.message : err}`,
                    {
                      description:
                        "The backend didn't answer. If it restarted, the "
                        + "recording indicator clears itself as soon as the "
                        + "backend is reachable again.",
                    },
                  );
                }
                // Either way, ask the server immediately rather than
                // waiting out the poll interval.
                refreshRecordingStatus();
              }}
              className={`flex items-center justify-center hover:bg-red-500/25 transition-colors text-red-700 dark:text-red-300 font-medium ${
                navCollapsed
                  ? "border-t border-red-500/30 py-1.5"
                  : "px-3 border-l border-red-500/30"
              }`}
              title="Stop recording"
              aria-label="Stop recording"
            >
              <span className="h-2.5 w-2.5 rounded-[2px] bg-red-600 dark:bg-red-400" />
              {!navCollapsed && <span className="ml-1.5 text-[11px]">Stop</span>}
            </button>
          </div>
        )}

        <nav className={`flex-1 overflow-y-auto ${navCollapsed ? "p-2" : "p-3"}`}>
          {!navCollapsed && (
            <div className="mb-2 px-3 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              Workspace
            </div>
          )}
          <ul className="space-y-1.5">
            {navItems.map((item) => {
              const badge = item.id === "sessions" && unprocessedCount > 0
                ? unprocessedCount
                : 0;
              return (
                <li key={item.id}>
                  <RailButton
                    icon={item.icon}
                    label={item.label}
                    active={nav === item.id}
                    collapsed={navCollapsed}
                    badge={badge}
                    hint={badge > 0
                      ? `${badge} session${badge === 1 ? "" : "s"} awaiting processing`
                      : undefined}
                    onClick={() => setNav(item.id)}
                  />
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Secondary cluster — Settings + Usage Guide sit apart from the
            workspace nav with their own label + top border so they read
            as utility items rather than more workflow tabs. The top
            border carries that separation into icon mode, where the
            "Support" caption itself doesn't fit. */}
        <div
          className={`border-t border-border pt-3 pb-3 space-y-1.5 ${
            navCollapsed ? "px-2" : "px-3"
          }`}
        >
          {!navCollapsed && (
          <div className="px-3 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Support
          </div>
          )}
          <RailButton
            icon={SettingsIcon}
            label="Settings"
            active={nav === "settings"}
            collapsed={navCollapsed}
            onClick={() => setNav("settings")}
          />
          <RailButton
            icon={HelpCircle}
            label="Usage Guide"
            active={nav === "help"}
            collapsed={navCollapsed}
            onClick={() => setNav("help")}
          />
          {/* Collapse toggle lives in the utility cluster so it reads
              the same in both widths and never competes with the brand
              mark for the 64px header. */}
          <RailButton
            icon={navCollapsed ? PanelLeftOpen : PanelLeftClose}
            label={navCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            active={false}
            collapsed={navCollapsed}
            onClick={toggleNavCollapsed}
          />
          {storage && (navCollapsed ? (
            // Icon mode keeps the readout usable by abbreviating to the
            // headline number; the full breakdown stays in the tooltip.
            <Tooltip>
              <TooltipTrigger
                render={
                  <div className="mt-2 rounded-xl bg-muted/60 px-1 py-1.5 text-center text-[10px] leading-tight text-muted-foreground">
                    <div className="font-medium text-foreground truncate">
                      {formatBytes(storage.total_bytes)}
                    </div>
                    <div className="text-[9px]">used</div>
                  </div>
                }
              />
              <TooltipContent side="right">
                {`Storage ${formatBytes(storage.total_bytes)} — ${storage.session_count} sessions · ${storage.wav_count} audio`}
              </TooltipContent>
            </Tooltip>
          ) : (
            <div className="mt-2 rounded-xl bg-muted/60 px-3.5 py-2.5 text-[11px] text-muted-foreground">
              <div className="flex items-center justify-between">
                <span>Storage</span>
                <span className="font-medium text-foreground">{formatBytes(storage.total_bytes)}</span>
              </div>
              <div className="mt-0.5 text-[10px]">
                {storage.session_count} sessions · {storage.wav_count} audio
              </div>
            </div>
          ))}
        </div>
      </aside>
      </TooltipProvider>

      {/* Main */}
      <main className="flex flex-1 flex-col overflow-hidden min-w-0">
        {/* Opaque, not bg-background/80 + backdrop-blur. Design review
            2026-08-11: the translucent header let the scrolled list show
            through it, which read as "the first card is clipped under
            the toolbar". The header is a flex sibling of the scroll
            container, so with an opaque fill nothing can visually
            overlap it. */}
        <header className="flex h-16 items-center justify-between border-b border-border bg-background px-6 shrink-0">
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

        {/* Shared scroll shell for EVERY view, so the spacing fixes here
            land everywhere at once (design review 2026-08-11):
              • pt-6 keeps a clear gap between the header rule and the
                first row of any view's toolbar.
              • pb-16 gives the last card room to breathe instead of
                butting against the window edge mid-scroll.
              • scrollbar-gutter:stable reserves the scrollbar's track
                even when a view doesn't overflow, so content never
                shifts on nav change AND the bar no longer sits flush
                against the content — pr-8 stays clear of it. */}
        <div
          ref={mainScrollRef}
          className="flex-1 overflow-y-auto overflow-x-hidden px-8 pt-6 pb-16 min-h-0 [scrollbar-gutter:stable]"
        >
          {nav === "today" && todayEnabled && (
            <ViewErrorBoundary viewName="Today">
              <TodayView onNavigate={setNav} />
            </ViewErrorBoundary>
          )}
          {nav === "record" && (
            <ViewErrorBoundary viewName="Record">
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
            </ViewErrorBoundary>
          )}
          {nav === "sessions" && (
            <ViewErrorBoundary viewName="Sessions">
              <SessionsView sessions={sessions} onReload={reloadSessions} onOpenSession={openSession} />
            </ViewErrorBoundary>
          )}
          {nav === "follow-ups" && (
            <ViewErrorBoundary viewName="Follow-Ups">
              <FollowUpsView sessions={sessions} onOpenSession={openSession} />
            </ViewErrorBoundary>
          )}
          {nav === "commitments" && (
            <ViewErrorBoundary viewName="Commitments">
              <CommitmentsView onOpenSession={openSession} />
            </ViewErrorBoundary>
          )}
          {nav === "decisions" && (
            <ViewErrorBoundary viewName="Decisions">
              <DecisionsView sessions={sessions} onOpenSession={openSession} />
            </ViewErrorBoundary>
          )}
          {nav === "search" && (
            <ViewErrorBoundary viewName="Search">
              <SearchView onOpenSession={openSession} />
            </ViewErrorBoundary>
          )}
          {nav === "qa" && (
            <ViewErrorBoundary viewName="Ask">
              <QAView onOpenSession={openSession} />
            </ViewErrorBoundary>
          )}
          {nav === "clients" && (
            <ViewErrorBoundary viewName="Clients">
              <ClientsView sessions={sessions} onReload={reloadSessions} onOpenSession={openSession} />
            </ViewErrorBoundary>
          )}
          {nav === "engagements" && (
            <ViewErrorBoundary viewName="Engagements">
              <EngagementView sessions={sessions} />
            </ViewErrorBoundary>
          )}
          {nav === "insights" && (
            <ViewErrorBoundary viewName="Insights">
              <InsightsView
                onOpenSession={openSession}
                existingClients={existingClients}
              />
            </ViewErrorBoundary>
          )}
          {nav === "prep-brief" && (
            <ViewErrorBoundary viewName="Prep Brief">
              <PrepBriefView sessions={sessions} meetings={meetings} />
            </ViewErrorBoundary>
          )}
          {nav === "settings" && (
            <ViewErrorBoundary viewName="Settings">
              <SettingsView onSaved={reloadSessions} />
            </ViewErrorBoundary>
          )}
          {nav === "help" && (
            <ViewErrorBoundary viewName="Usage Guide">
              <UsageGuideView onLaunchSetup={() => setOnboardingOpen(true)} />
            </ViewErrorBoundary>
          )}
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
