"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, openExternal, openSystemSettings, type AudioDevice, type AudioSyncRisk, type Meeting, type RecordingStatus, type SessionFull, type SessionSummary } from "@/lib/api";
import {
  refreshRecordingStatus,
  suppressWatchdogForNewRecording,
  useRecordingStatus,
} from "@/lib/recording-status";
import { toast } from "sonner";
import {
  Calendar as CalendarIcon,
  Sparkles,
  Loader2,
  Square,
  Play,
  Mic,
  MicOff,
  Volume2,
  VolumeX,
  FileText,
  Camera,
  Ban,
  ChevronRight,
  ChevronDown,
  ExternalLink,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Info,
  Image as ImageIcon,
  FolderOpen,
  Copy,
  Check,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InfoTip } from "@/components/ui/info-tip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { LiveTranscriptPanel } from "./live-transcript-panel";
import { MeetingBriefModal } from "./meeting-brief-modal";
import { LiveSearchPanel } from "./live-search-panel";
import { CoPilotPanel } from "./co-pilot-panel";

interface Props {
  onSessionsChanged: () => void;
  onOpenSession: (id: string, tab?: string) => void;
  meetings: Meeting[];
  meetingsLoading: boolean;
  // Pass `silent=true` for background refreshes (focus-triggered, etc.)
  // where an Outlook hiccup should keep the current list instead of
  // flashing to empty. Silent mode also suppresses toast feedback.
  onRefreshCalendar: (silent?: boolean) => void;
}

// One physical display, as reported by Tauri's monitor list. Bounds are
// in physical pixels; the Rust capture command converts as needed.
interface ScreenMonitor {
  name: string | null;
  x: number;
  y: number;
  width: number;
  height: number;
  scale: number;
}

export function RecordView({
  onSessionsChanged,
  onOpenSession,
  meetings,
  meetingsLoading,
  onRefreshCalendar,
}: Props) {
  const [templates, setTemplates] = useState<string[]>([]);
  const [inputDevices, setInputDevices] = useState<AudioDevice[]>([]);
  const [outputDevices, setOutputDevices] = useState<AudioDevice[]>([]);
  // Keep the whole session list in state so we can filter projects by
  // the currently-selected client (rather than showing every project
  // anyone's ever used, regardless of customer).
  const [allSessions, setAllSessions] = useState<SessionSummary[]>([]);
  // Clients that have been persisted to client_configs.json. Lets a
  // freshly-created client name (one with no sessions yet) show up in
  // the autocomplete here, and gives us the canonical set we check
  // against when deciding whether to persist a newly-typed name on
  // start().
  const [clientConfigs, setClientConfigs] = useState<Record<string, { export_folder: string; display_name?: string }>>({});

  const [meetingName, setMeetingName] = useState("");
  const [template, setTemplate] = useState("General");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [micIdx, setMicIdx] = useState<number | null>(null);
  const [outIdx, setOutIdx] = useState<number | null>(null);
  // Conference room mode: laptop in middle of a room, mic captures
  // everyone, no system audio to record. Persisted across sessions
  // since it's a deliberate choice tied to physical setup, not a
  // per-meeting toggle most of the time.
  const [conferenceRoomMode, setConferenceRoomMode] = useState<boolean>(() => {
    try { return localStorage.getItem("conferenceRoomMode") === "1"; }
    catch { return false; }
  });
  const [attendees, setAttendees] = useState<string[]>([]);
  // ISO datetime of the calendar meeting's scheduled end, when the
  // user clicked Use on a calendar tile. Threaded through to
  // /recording/start so the backend's auto-stop watchdog knows when
  // to start nagging about meeting overrun. null for ad-hoc recordings.
  const [scheduledEndIso, setScheduledEndIso] = useState<string | null>(null);

  // Recording state is READ, never stored, from the app-wide shared
  // poll (src/lib/recording-status.ts) that the sidebar badge also
  // reads. Field report 2026-08-11: this view and the sidebar each kept
  // their own copy and drifted apart after a Stop that failed against a
  // restarting backend — the Record page showed the idle "Start
  // Recording" form while the sidebar counted "Recording… 20:10"
  // upward. There is now exactly one answer to "are we recording", and
  // only the server can change it.
  const { status: recordingStatus, reachable: backendReachable } = useRecordingStatus();
  const recording = !!recordingStatus?.is_recording;
  const duration = recordingStatus?.duration_s ?? 0;
  const modelsLoading = !!recordingStatus?.models_loading;
  // The active recording's session_id, captured from /recording/status.
  // Lets mid-recording form edits PATCH the right session without
  // having to ask the backend again on every keystroke.
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  // Screenshots captured during the current recording. Count is shown
  // on the button; the files themselves are attached server-side and
  // fed to the summarizer as visual context.
  const [screenshotBusy, setScreenshotBusy] = useState(false);
  const [screenshotCount, setScreenshotCount] = useState(0);
  // Thumbnail-strip entries for the current recording, pushed as each
  // capture completes (manual button or auto-timer) — never polled.
  // `index` is the screenshot's 0-based position in the session's
  // `screenshots` list (== attachScreenshot's returned count - 1),
  // which is what /sessions/{id}/screenshots/{index} expects.
  const [screenshotEntries, setScreenshotEntries] = useState<
    { index: number; takenAt: number }[]
  >([]);
  // Indices whose image fetch has failed (backend hiccup, file not
  // finished writing, capture permission revoked mid-meeting, etc.).
  // Tracked so a failed fetch swaps in a placeholder tile instead of
  // ever dropping the entry or blanking the strip.
  const [screenshotLoadFailed, setScreenshotLoadFailed] = useState<Set<number>>(new Set());
  // Larger view when a thumbnail is clicked; null = closed.
  const [zoomedScreenshot, setZoomedScreenshot] = useState<number | null>(null);
  // Destination folder for this recording's screenshots (Part 2 — the
  // actual pain point). Fetched once per recording, independent of
  // whether any screenshot has been taken yet, so the folder is
  // discoverable before the first capture.
  const [screenshotDirPath, setScreenshotDirPath] = useState<string | null>(null);
  const [screenshotDirCopied, setScreenshotDirCopied] = useState(false);
  // Resolved backend origin + auth suffix for the thumbnail/zoom <img>
  // src — same mechanism the audio player and session Screenshots tab
  // use (session-detail-dialog.tsx): getBaseUrl() for the dynamic port,
  // authQuery() because an <img> src can't carry an Authorization header.
  const [mediaBaseUrl, setMediaBaseUrl] = useState("");
  const [mediaAuthQuery, setMediaAuthQuery] = useState("");
  useEffect(() => {
    api.getBaseUrl().then(setMediaBaseUrl).catch(() => {});
    api.authQuery().then(setMediaAuthQuery).catch(() => {});
  }, []);
  // When the user has >1 monitor, clicking Screenshot opens this picker
  // so they choose which display to capture (rather than us guessing).
  const [monitorPicker, setMonitorPicker] = useState<{
    dir: string;
    monitors: ScreenMonitor[];
  } | null>(null);
  // Subjects flagged "never auto-record" (permanent, server-persisted,
  // matched by subject so a recurring series stays blocked).
  const [blockedSubjects, setBlockedSubjects] = useState<string[]>([]);
  // Case-insensitive substring patterns (managed in Settings → Auto-record
  // skip patterns). A meeting is blocked if any pattern occurs anywhere
  // in its subject. The backend already enforces these; the frontend
  // tracks them too so the meeting tile can SHOW that a meeting is
  // pattern-blocked instead of looking unblocked.
  const [blockedPatterns, setBlockedPatterns] = useState<string[]>([]);
  // Lazy per-meeting detail (agenda/body, attendees, join link). Keyed
  // by subject|start. Only the expanded meeting is fetched, so the
  // calendar list stays fast.
  const [expandedMeeting, setExpandedMeeting] = useState<string | null>(null);
  const [meetingDetails, setMeetingDetails] = useState<
    Record<
      string,
      {
        loading: boolean;
        data?: { attendees: string[]; body: string; join_url: string | null };
        error?: string;
      }
    >
  >({});

  const meetingKey = (m: Meeting) => `${m.subject}|${m.start}`;

  // Rows sourced from the Chrome extension's Outlook Web scrape rather
  // than the local Outlook/EventKit calendar. They exist precisely
  // BECAUSE the local calendar can't see them, so anything that reaches
  // back into the LOCAL CALENDAR — the /calendar/meeting-detail lookup
  // for agenda/attendees — has nothing to resolve against (field report
  // 2026-08-11).
  //
  // Auto-record is NO LONGER in that category: the backend's trigger
  // loop and this panel now read one merged two-source list
  // (backend/services/calendar_feed.py), so an extension row can
  // auto-start exactly like a local one and gets the same per-meeting
  // opt-out below.
  const isExtensionSourced = (m: Meeting) => m.source === "extension";

  // Mirrors backend/services/auto_record_service.py's `_is_all_day`:
  // starts at local midnight and runs a whole number of days. Those are
  // birthdays / OOO / travel blocks — auto-record skips them outright,
  // whichever calendar they came from, so this is the one row type that
  // still has to say "Manual only" and mean it.
  const isAllDay = (m: Meeting) => {
    const start = new Date(m.start);
    const end = new Date(m.end);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return false;
    if (start.getHours() !== 0 || start.getMinutes() !== 0) return false;
    const seconds = (end.getTime() - start.getTime()) / 1000;
    return seconds >= 23 * 3600 && seconds % (24 * 3600) < 60;
  };

  // True when auto-record can actually act on this row. Kept as one
  // named predicate so the tile can never imply a capability the
  // backend doesn't have (or hide one it does) — the "Manual only"
  // label that used to sit here was correct when auto-record read only
  // the local calendar and became a lie the moment it stopped.
  const canAutoRecord = (m: Meeting) => !isAllDay(m);

  const toggleMeetingDetail = (m: Meeting) => {
    const key = meetingKey(m);
    if (expandedMeeting === key) {
      setExpandedMeeting(null);
      return;
    }
    setExpandedMeeting(key);
    if (meetingDetails[key]?.data || meetingDetails[key]?.loading) return;
    if (isExtensionSourced(m)) {
      // /calendar/meeting-detail reads LOCAL Outlook by subject+start.
      // For an Outlook-Web-only meeting that lookup always misses, so
      // it would render an empty panel and a pointless round trip.
      // Show what the scrape actually captured instead.
      setMeetingDetails((prev) => ({
        ...prev,
        [key]: {
          loading: false,
          data: {
            attendees: m.attendees || [],
            body: "",
            join_url: m.join_url || null,
          },
        },
      }));
      return;
    }
    setMeetingDetails((prev) => ({ ...prev, [key]: { loading: true } }));
    api
      .getMeetingDetail(m.subject, m.start)
      .then((d) =>
        setMeetingDetails((prev) => ({
          ...prev,
          [key]: { loading: false, data: d },
        })),
      )
      .catch((e) =>
        setMeetingDetails((prev) => ({
          ...prev,
          [key]: {
            loading: false,
            error: e instanceof Error ? e.message : String(e),
          },
        })),
      );
  };
  // Auto-stop watchdog warnings, read off the shared /recording/status
  // poll while recording. Used to render a banner under the recording
  // bar and to fire native OS notifications (once per code per session,
  // so we don't spam the user with the same toast every second).
  const watchdogWarnings: RecordingStatus["warnings"] =
    recordingStatus?.warnings ?? [];
  const notifiedCodesRef = useRef<Set<string>>(new Set());

  const [session, setSession] = useState<SessionFull | null>(null);
  // Currently-open Brief modal target. null = closed; a Meeting object
  // means the modal is open and showing a brief for that meeting. We
  // store the meeting itself rather than a separate "open" boolean so
  // a fresh click on a different tile cleanly resets the modal state.
  const [briefMeeting, setBriefMeeting] = useState<Meeting | null>(null);

  // Calendar-driven auto-record toggle. Persisted server-side via
  // Settings.auto_record_enabled; mirrored here so the Switch can flip
  // optimistically. `autoRecordNext` is the next qualifying event, shown
  // as a small hint under the toggle.
  const [autoRecord, setAutoRecord] = useState<boolean>(false);
  // Live Co-Pilot opt-in. Hydrated from Settings alongside autoRecord —
  // see the effect below. When true and a recording is in progress, the
  // CoPilotPanel renders beside the live transcript. The recording-bar
  // Switch flips this via api.setLiveCopilotEnabled, which is safe to
  // call mid-recording (unlike the full POST /settings).
  const [liveCopilotEnabled, setLiveCopilotEnabled] = useState<boolean>(false);
  const [liveCopilotSaving, setLiveCopilotSaving] = useState<boolean>(false);
  const [autoRecordNext, setAutoRecordNext] = useState<{
    subject: string;
    start: string;
    end: string;
  } | null>(null);
  const [autoRecordSaving, setAutoRecordSaving] = useState<boolean>(false);

  // Readiness panel (Part 3): whether a calendar is connected at all.
  // null = not checked yet. Informational only — see the render below —
  // a disconnected calendar is never a reason recording can't start, so
  // this never gates "Ready to record". Fetched once on mount alongside
  // the other one-shot local data below; not a new polling loop.
  const [calendarAvailable, setCalendarAvailable] = useState<boolean | null>(null);
  // Full response from /calendar/available — carries `source` and,
  // when calendar_source is "extension", the last-capture time and
  // event counts the empty state below uses so an empty/stale
  // extension capture never looks identical to a genuinely free
  // calendar (field report 2026-08-13).
  const [calendarStatus, setCalendarStatus] = useState<{
    available?: boolean;
    source?: string;
    last_capture_at?: string | null;
    event_count?: number;
    future_event_count?: number;
    // Which calendar-parse path produced the currently-retained events
    // — "structured" (client-side aria-label extraction, no LLM) vs.
    // "text-fallback" (LLM-parsed after the extension's own structured
    // scan came up empty or absent) — and why a fallback happened, so
    // the empty/low-count state can say "N meetings from the
    // extension's structured capture" instead of rendering identically
    // regardless of which path ran (field report chain culminating
    // 2026-08-14; see backend/services/extension_calendar_service.py's
    // replace_all `import_meta`).
    last_import_path?: string | null;
    last_import_fallback_reason?: string | null;
  } | null>(null);

  // Load initial data. Calendar data is owned by the parent (page.tsx)
  // so it survives nav switches; we only load local things here.
  useEffect(() => {
    (async () => {
      try {
        // Fast-path: everything local (device list, templates, sessions)
        // NB: no /recording/status call here — the shared store owns
        // that endpoint now, so this mount fetch can't race it or land
        // a staler answer than the poll already has.
        const [devices, tpls, sessionsList, cfgs, calAvail] = await Promise.all([
          api.getAudioDevices(),
          api.getTemplates(),
          api.listSessions().catch(() => []),
          api.getClientConfigs().catch(() => ({} as Record<string, { export_folder: string; display_name?: string }>)),
          // Readiness panel's "Calendar connected" row. Never a blocker
          // (see calendarAvailable's doc comment) — a failed fetch just
          // means "unknown", shown the same as "not connected".
          api.isCalendarAvailable().catch(() => ({ available: false })),
        ]);
        setCalendarAvailable(!!calAvail.available);
        setCalendarStatus(calAvail);
        setClientConfigs(cfgs);
        setInputDevices(devices.input);
        setOutputDevices(devices.output);
        // Templates endpoint now returns full {name, prompt, ...} entries
        // so the Settings editor can work; the Record-view dropdown only
        // needs the names, so we drop the rest here.
        setTemplates(tpls.map((t) => t.name));
        setAllSessions(sessionsList);

        // Restore saved device selection by NAME (indices can shift
        // between reboots when devices are plugged in/out, so we match
        // on stable name).
        const savedMicName = typeof window !== "undefined"
          ? window.localStorage.getItem("mr.micDeviceName")
          : null;
        const savedOutName = typeof window !== "undefined"
          ? window.localStorage.getItem("mr.outputDeviceName")
          : null;

        const matchedMic = savedMicName
          ? devices.input.find((d) => d.name === savedMicName)
          : null;
        if (matchedMic) {
          setMicIdx(matchedMic.index);
        } else if (devices.input.length > 0) {
          setMicIdx(devices.input[0].index);
        }

        const matchedOut = savedOutName && savedOutName !== "__none__"
          ? devices.output.find((d) => d.name === savedOutName)
          : null;
        if (matchedOut) {
          setOutIdx(matchedOut.index);
        } else if (devices.output.length > 0) {
          // Default to the first speaker (system audio is always on).
          setOutIdx(devices.output[0].index);
        }

        // No local mirroring of `status` here — recording / duration /
        // models_loading all come from the shared store above, which
        // has already polled by the time this mount fetch resolves.

        // Deliberately NOT auto-loading models here anymore. Opening
        // the app used to fire /models/load on Record-view mount, which
        // raced the other load triggers at boot and fed the
        // 0xC0000005 crash loop (2026-07-21 logs). Models now load
        // only when actually needed: on record-start when live
        // transcription is enabled, or on Process. Recording itself
        // never requires them.
      } catch (e) {
        console.error(e);
      }
    })();
  }, []);

  // Autocomplete data: clients are all unique client names, projects are
  // filtered to only those tagged under the currently-selected client.
  // When no client is chosen we fall back to every project — so you can
  // still pick a project first and have the client auto-fill on the next
  // render once you've typed it.
  const existingClients = useMemo(() => {
    const seen = new Map<string, string>();
    for (const s of allSessions) {
      const name = (s.client || "").trim();
      if (!name) continue;
      const key = name.toLowerCase();
      if (!seen.has(key)) seen.set(key, name);
    }
    // Merge in configured clients (e.g. created via Clients view but
    // never tagged on a session yet) so the autocomplete here knows
    // about them too.
    for (const cfg of Object.values(clientConfigs)) {
      const name = (cfg.display_name || "").trim();
      if (!name) continue;
      const key = name.toLowerCase();
      if (!seen.has(key)) seen.set(key, name);
    }
    return Array.from(seen.values()).sort((a, b) => a.localeCompare(b));
  }, [allSessions, clientConfigs]);
  const existingProjects = useMemo(() => {
    const target = (client || "").trim().toLowerCase();
    const scoped = target
      ? allSessions.filter((s) => (s.client || "").trim().toLowerCase() === target)
      : allSessions;
    return Array.from(new Set(scoped.map((s) => s.project).filter(Boolean))).sort();
  }, [allSessions, client]);

  // Clear the project field when the client changes — keeping a project
  // from a different customer around only invites mis-tagged sessions.
  // Skip the effect on mount (no prior client) so we don't nuke a
  // template restore down the line.
  const prevClientRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = prevClientRef.current;
    prevClientRef.current = client;
    if (prev === null) return;
    if (prev === client) return;
    if (project) setProject("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  // Persist device selection by name whenever it changes.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (inputDevices.length === 0 || micIdx === null) return;
    const dev = inputDevices.find((d) => d.index === micIdx);
    if (dev) window.localStorage.setItem("mr.micDeviceName", dev.name);
  }, [micIdx, inputDevices]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (outIdx === null) return;
    const dev = outputDevices.find((d) => d.index === outIdx);
    if (dev) window.localStorage.setItem("mr.outputDeviceName", dev.name);
  }, [outIdx, outputDevices]);

  // Auto-record toggle: hydrate from persisted Settings on mount, then
  // poll /recording/auto-status every 30s so the "next: <subject>" hint
  // stays current as the calendar evolves. The poll is cheap (no
  // calendar fetch — the service exposes its cached next_event).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getSettings();
        if (cancelled) return;
        setAutoRecord(Boolean(s.auto_record_enabled));
        setLiveCopilotEnabled(Boolean(s.live_copilot_enabled));
      } catch {
        // Settings unreachable — leave toggle off. The /settings
        // failure path already surfaces elsewhere.
      }
    })();
    const refresh = async () => {
      try {
        const st = await api.getAutoRecordStatus();
        if (cancelled) return;
        setAutoRecordNext(st.next_event);
      } catch {
        // Backend not ready — try again next tick.
      }
    };
    refresh();
    const id = window.setInterval(refresh, 30_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const handleAutoRecordToggle = async (next: boolean) => {
    if (recording && next) {
      // Avoid the confusing "turn it on while manually recording, then
      // wonder why nothing changed" case. Refuse the flip until Stop.
      toast.info(
        "Stop the current recording before enabling auto-record.");
      return;
    }
    setAutoRecord(next); // optimistic
    setAutoRecordSaving(true);
    try {
      const current = await api.getSettings();
      await api.saveSettings({ ...current, auto_record_enabled: next });
      // Pull a fresh status so the "next: …" hint populates immediately
      // when the user flips on.
      const st = await api.getAutoRecordStatus();
      setAutoRecordNext(st.next_event);
      toast.success(next ? "Auto-record on" : "Auto-record off");
    } catch (e: unknown) {
      setAutoRecord(!next); // rollback
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Couldn't save auto-record setting: ${msg}`);
    } finally {
      setAutoRecordSaving(false);
    }
  };

  // Live Co-Pilot in-bar toggle. Uses the lightweight endpoint that
  // accepts flips mid-recording — the full POST /settings refuses while
  // recording (rebuilding RecordingService would orphan capture
  // threads), but this one just rewrites the env line and updates the
  // cached Settings in place.
  const toggleLiveCopilot = async (next: boolean) => {
    setLiveCopilotEnabled(next); // optimistic
    setLiveCopilotSaving(true);
    try {
      await api.setLiveCopilotEnabled(next);
      toast.success(next ? "Co-Pilot on" : "Co-Pilot off");
    } catch (e: unknown) {
      setLiveCopilotEnabled(!next); // rollback
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Couldn't toggle Co-Pilot: ${msg}`);
    } finally {
      setLiveCopilotSaving(false);
    }
  };

  // Fetch the screenshot destination folder once per recording — not
  // gated on ever taking a screenshot, so the folder is discoverable
  // (the actual pain point) even before the first capture. The endpoint
  // 409s when not recording, so this only fires on the rising edge.
  useEffect(() => {
    // Deliberately no synchronous setScreenshotDirPath(null) on the
    // falsy branch here — the whole card that reads this value is
    // itself gated on `recording`, so there's nothing to blank, and a
    // stale path from the previous recording is silently overwritten
    // the moment the next fetch below resolves.
    if (!recording) return;
    let cancelled = false;
    api.getScreenshotDir()
      .then((r) => { if (!cancelled) setScreenshotDirPath(r.dir); })
      .catch(() => { /* not recording yet, or backend hiccup — leave unset */ });
    return () => { cancelled = true; };
  }, [recording]);

  // Look up the currently selected device objects for display.
  const selectedMic = inputDevices.find((d) => d.index === micIdx);
  const selectedOut = outputDevices.find((d) => d.index === outIdx);

  // Build a screenshot's image URL, or null when we don't have enough
  // to build one yet (base URL hasn't resolved, or — post-recording
  // only — no active session id). Null renders as the placeholder
  // tile, same as a failed fetch — see screenshotLoadFailed.
  //
  // While recording, this reads from GET /recording/screenshots/{index}
  // — straight off the backend's in-memory active session, so a
  // thumbnail is fetchable moments after capture instead of only after
  // the session JSON reaches disk (which historically only happened on
  // stop/process; screenshots now also get a best-effort disk mirror on
  // attach, but the live strip doesn't depend on that mirror landing).
  // Once stopped, fall back to the historical per-session endpoint —
  // the strip itself unmounts on stop, but the zoom dialog reuses this
  // same helper and can still be closing out its last render.
  const screenshotImgSrc = (index: number): string | null => {
    if (!mediaBaseUrl) return null;
    if (recording) {
      return `${mediaBaseUrl}/recording/screenshots/${index}${mediaAuthQuery}`;
    }
    if (!activeSessionId) return null;
    return `${mediaBaseUrl}/sessions/${activeSessionId}/screenshots/${index}${mediaAuthQuery}`;
  };

  const markScreenshotLoadFailed = (index: number) => {
    setScreenshotLoadFailed((prev) => {
      if (prev.has(index)) return prev;
      const next = new Set(prev);
      next.add(index);
      return next;
    });
  };

  // Same copy-to-clipboard fallback used elsewhere in the app
  // (session-detail-dialog.tsx's CopyButton) — navigator.clipboard
  // requires a secure context, which an older WebView may not have.
  const copyScreenshotDir = async () => {
    if (!screenshotDirPath) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(screenshotDirPath);
      } else {
        const ta = document.createElement("textarea");
        ta.value = screenshotDirPath;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setScreenshotDirCopied(true);
      toast.success("Folder path copied");
      setTimeout(() => setScreenshotDirCopied(false), 1500);
    } catch (e) {
      toast.error(`Copy failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // ── Readiness panel (Part 3) ─────────────────────────────────────
  // Derived, not fetched — reuses device lists / backend reachability
  // already held in state, plus the one-shot calendar check above. No
  // new polling loops or endpoints.
  const micReady = micIdx !== null && !!selectedMic;
  // System audio is never a hard requirement — conference room mode is
  // a deliberate, legitimate choice to skip it, and even outside that
  // mode a mic-only recording is allowed (you just lose the far end).
  // So this can only ever reach "pass" or "warn", never "fail".
  const systemAudioReady = conferenceRoomMode || (outIdx !== null && !!selectedOut);
  // "Ready to record" only cares about hard requirements: a mic and a
  // reachable backend. System audio and calendar are surfaced for
  // awareness but never block the summary line — see AGENTS.md Part 3.
  const readyToRecord = micReady && backendReachable;

  // Detect mic↔loopback shared-mode mix-format mismatch. The Windows
  // audio engine resamples each side independently; if the two
  // devices' default formats differ (sample rate or bit depth), the
  // streams drift apart over long recordings — the v2.10.5 field
  // report was 16-bit mic + 24-bit speakers producing ~31 s of drift
  // on a 49-min session. Hidden during recording (too late to fix)
  // and in conference room mode (loopback isn't captured then).
  const [audioSyncRisk, setAudioSyncRisk] = useState<AudioSyncRisk | null>(null);
  useEffect(() => {
    if (recording || conferenceRoomMode) return;
    if (!selectedMic || !selectedOut) {
      setAudioSyncRisk(null);
      return;
    }
    let cancelled = false;
    api.getAudioSyncRisk(selectedMic.name, selectedOut.name)
      .then((r) => { if (!cancelled) setAudioSyncRisk(r); })
      .catch(() => { if (!cancelled) setAudioSyncRisk(null); });
    return () => { cancelled = true; };
  }, [recording, conferenceRoomMode, selectedMic, selectedOut]);

  // Live-patch the active session when the user edits meeting name /
  // client / project mid-recording. Debounced so we don't fire on
  // every keystroke. Without this, a user discovering an auto-record
  // in progress had to wait for it to stop before tagging the session.
  useEffect(() => {
    if (!recording || !activeSessionId) return;
    const t = setTimeout(() => {
      api.patchSession(activeSessionId, {
        display_name: meetingName || undefined,
        client: client || undefined,
        project: project || undefined,
        template: template || undefined,
      }).catch(() => {
        // Best-effort — the next debounce or the post-stop save will
        // cover any transient failure.
      });
    }, 600);
    return () => clearTimeout(t);
  }, [meetingName, client, project, template, activeSessionId, recording]);

  // React to the app-wide /recording/status poll. This view used to run
  // its OWN 1s interval here and keep its own `recording` boolean; that
  // second copy of the truth is what diverged from the sidebar's copy
  // in field report 2026-08-11. The poll itself now lives in
  // src/lib/recording-status.ts and every consumer reads the same
  // object; what stays here is only the reactions that belong to the
  // Record page — watchdog notifications, pulling an auto-record
  // subject into the form, and explaining an auto-stop.
  //
  // Edges are detected against a ref, not against `recording`: that
  // value is now DERIVED from this very status object, so comparing
  // them would never fire. The ref records what the PREVIOUS poll said.
  const prevIsRecordingRef = useRef<boolean | null>(null);
  useEffect(() => {
    const s = recordingStatus;
    // Nothing observed yet (first paint, or the backend has been
    // unreachable since launch). Hold — never assume a state.
    if (!s) return;

    // Auto-stop watchdog warnings — fire native notifications. Codes
    // are stable per condition so we only notify once per code per
    // recording session (the user doesn't want a beep every second
    // while the meeting runs over). The banner itself renders straight
    // off `watchdogWarnings`, which is derived from this same object.
    const warns = s.warnings || [];
    for (const w of warns) {
      if (notifiedCodesRef.current.has(w.code)) continue;
      notifiedCodesRef.current.add(w.code);
      fireNativeNotification(w.code, w.message);
    }

    const prev = prevIsRecordingRef.current;
    prevIsRecordingRef.current = s.is_recording;

    if (prev !== s.is_recording) {
      if (s.is_recording) {
        // Rising edge: an external trigger (auto-record from calendar,
        // /recording/start fired from somewhere else) started a
        // recording without this view initiating it. Pull the meeting
        // name + scope hints the backend set when it started.
        if (s.auto_record_subject) {
          setMeetingName((current) =>
            current.trim() ? current : s.auto_record_subject!);
        }
        if (s.session_id) setActiveSessionId(s.session_id);
        // Reset notification dedupe for the newly-detected recording.
        notifiedCodesRef.current = new Set();
      } else {
        // Falling edge — watchdog auto-stop, parent-PID deadman switch,
        // Stop clicked in the sidebar, or a Stop from this view whose
        // request failed but which the backend actually carried out.
        // The server said not-recording, so we clear. This is the ONLY
        // thing that clears recording state anywhere in the app.
        setActiveSessionId(null);
        // If the watchdog auto-stopped us, the last poll's warning is
        // what we want to surface as an explanatory toast — the user
        // wasn't watching the screen, that's the whole point. `prev`
        // being true means we actually observed the recording running,
        // so this can't fire on a cold start.
        const stopWarn = warns.find(
          (w) => w.code.endsWith("_stop") || w.code === "hard_cap_hit"
        );
        if (stopWarn && prev) {
          toast.warning("Recording auto-stopped", {
            description: stopWarn.message,
          });
          // Auto-stopped sessions still need the post-stop processing
          // path to kick off (auto_process / refresh sessions list).
          onSessionsChanged();
        }
      }
    }

    // Keep the captured session_id fresh in case we missed the rising
    // edge (e.g. view mounted after recording was already in progress).
    if (s.is_recording && s.session_id && !activeSessionId) {
      setActiveSessionId(s.session_id);
    }
  }, [recordingStatus, activeSessionId, onSessionsChanged]);

  const start = async () => {
    try {
      const res = await api.startRecording({
        mic_device_index: micIdx,
        // Conference room mode: backend ignores output_device_index
        // anyway, but send null so the request payload reflects
        // intent (and the live preview never tries to subscribe to a
        // loopback that won't exist).
        output_device_index: conferenceRoomMode ? null : outIdx,
        meeting_name: meetingName || new Date().toISOString().slice(0, 10) + " Meeting",
        template,
        client,
        project,
        attendees,
        scheduled_end_iso: scheduledEndIso ?? undefined,
        conference_room_mode: conferenceRoomMode,
      });
      // Suppress the shell's watchdog kill for the duration of the
      // recording — an unresponsive backend may still be capturing
      // audio, and this meeting can't be re-run. Safe to do optimistically
      // (unlike CLEARING it, which only the server may do): the shared
      // poll takes ownership of the flag from here on.
      suppressWatchdogForNewRecording();
      // `recording` flips on the next poll, which we kick immediately
      // rather than waiting out the interval. No local setRecording —
      // the server is the only thing that says whether we're recording.
      refreshRecordingStatus();
      setScreenshotCount(0);
      setScreenshotEntries([]);
      setScreenshotLoadFailed(new Set());
      setSession(null);
      notifiedCodesRef.current = new Set();
      toast.success("Recording started", { description: `Session ${res.session_id}` });

      // If the user typed a client name that hasn't been persisted yet,
      // create it now so it survives even if this recording is later
      // deleted, and so it syncs to other devices via client_configs.json.
      // Fire-and-forget — failure here shouldn't block the recording.
      const trimmedClient = (client || "").trim();
      if (trimmedClient) {
        const knownKeys = new Set(
          Object.values(clientConfigs)
            .map((c) => (c.display_name || "").trim().toLowerCase())
            .filter(Boolean));
        if (!knownKeys.has(trimmedClient.toLowerCase())) {
          api.setClientConfig(trimmedClient, { export_folder: "" })
            .then(() => api.getClientConfigs().then(setClientConfigs).catch(() => {}))
            .catch((e) => {
              // Surface but don't disrupt — the client is still on the
              // session JSON; this only affects the configured store.
              console.warn("Could not persist new client", trimmedClient, e);
            });
        }
      }
    } catch (e) {
      toast.error(`Start failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const stop = async () => {
    try {
      const res = await api.stopRecording();
      // Deliberately no setRecording(false) / setRecordingActive(false)
      // here. Field report 2026-08-11: local optimism on stop is what
      // let this view show the idle form while the sidebar kept
      // counting "Recording… 20:10". The next poll (kicked immediately
      // below) carries the server's answer to BOTH, and the shared
      // store clears the Tauri shell's RECORDING_ACTIVE flag as part of
      // the same reconciliation.
      refreshRecordingStatus();
      toast.success("Recording saved", { description: res.audio_path });
      // Reload the session into the UI
      const s = await api.getSessionFull(res.session_id);
      setSession(s);
      onSessionsChanged();

      // Auto-process is now BACKEND-OWNED. The /recording/stop endpoint
      // (and every other stop path — sidebar pill, watchdog auto-stop)
      // kicks off the full pipeline server-side when AUTO_PROCESS_AFTER_STOP
      // is on. We used to trigger processFull from here, which meant any
      // stop that didn't go through this handler (auto-stop, sidebar)
      // silently skipped processing. So we no longer call processFull —
      // we just tell the user it's running and let the existing
      // sessions/unprocessed pollers refresh the UI when it finishes.
      try {
        const settings = await api.getSettings();
        if (settings.auto_process_after_stop) {
          toast.info("Auto-processing started", {
            description:
              "Transcribing + extracting in the background. The session " +
              "updates automatically when it finishes (can take several minutes).",
          });
        }
      } catch {
        // Couldn't read settings — nothing to announce. Backend still
        // auto-processes if the setting is on; the user can also click
        // Process manually from the session dialog.
      }
    } catch (e) {
      // The stop request failed — most often because the backend was
      // restarting at that exact moment (field report 2026-08-11). Two
      // rules apply here:
      //
      //   1. Do NOT clear recording state. We genuinely don't know
      //      whether capture stopped, and a live meeting can't be
      //      re-run; guessing "stopped" is how the UI ended up
      //      contradicting itself.
      //   2. Do NOT leave the user stuck either. The shared poll
      //      reconciles to the server the moment it's reachable, so a
      //      backend that restarted (and therefore is NOT recording)
      //      clears the indicator on its own — and clears the shell's
      //      RECORDING_ACTIVE flag with it, so the watchdog doesn't
      //      stay suppressed forever.
      toast.error(`Stop failed: ${e instanceof Error ? e.message : e}`, {
        description:
          "The backend didn't answer — it may be restarting. The recording "
          + "indicator will correct itself automatically within a second of "
          + "the backend coming back. If it's still showing, the recording "
          + "really is still running.",
        duration: 10000,
      });
      refreshRecordingStatus();
    }
  };

  // Capture a screenshot of the user's screen and attach it to the
  // active recording. The capture runs in the Tauri shell (macOS
  // attributes Screen Recording permission to the signed bundle, not
  // the Python sidecar); the backend owns the destination folder and
  // session bookkeeping.
  // Capture `m` (or the whole primary screen when m is null) and attach
  // it to the active recording.
  const captureMonitor = async (dir: string, m: ScreenMonitor | null) => {
    setScreenshotBusy(true);
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const path = await invoke<string>("capture_screenshot", {
        dir,
        x: m ? Math.round(m.x) : 0,
        y: m ? Math.round(m.y) : 0,
        width: m ? Math.round(m.width) : 0,
        height: m ? Math.round(m.height) : 0,
        scale: m ? m.scale : 1,
      });
      const res = await api.attachScreenshot(path);
      setScreenshotCount(res.count);
      // Drive the strip straight off this capture's result — no polling.
      // index is 0-based (count - 1), matching the session's screenshots
      // list position that /sessions/{id}/screenshots/{index} reads.
      setScreenshotEntries((prev) => [
        ...prev,
        { index: res.count - 1, takenAt: Date.now() },
      ]);
      toast.success("Screenshot captured", {
        description: `${res.count} attached — included in the summary to Claude`,
      });
    } catch (e) {
      toast.error(
        `Screenshot failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setScreenshotBusy(false);
    }
  };

  const takeScreenshot = async () => {
    setScreenshotBusy(true);
    try {
      const { dir } = await api.getScreenshotDir();
      // Enumerate displays so the user can pick. availableMonitors is
      // Tauri-only; outside Tauri (plain web dev) we just grab primary.
      let monitors: ScreenMonitor[] = [];
      try {
        const win = await import("@tauri-apps/api/window");
        const list = await win.availableMonitors();
        monitors = list.map((mon) => ({
          name: mon.name ?? null,
          x: mon.position.x,
          y: mon.position.y,
          width: mon.size.width,
          height: mon.size.height,
          scale: mon.scaleFactor,
        }));
      } catch {
        monitors = [];
      }

      if (monitors.length <= 1) {
        // Nothing to choose — capture the one (or primary fallback).
        await captureMonitor(dir, monitors[0] ?? null);
        return;
      }
      // Multiple displays: let the user pick. The dialog drives the
      // actual capture; release the busy state until they choose.
      setScreenshotBusy(false);
      setMonitorPicker({ dir, monitors });
    } catch (e) {
      toast.error(
        `Screenshot failed: ${e instanceof Error ? e.message : String(e)}`);
      setScreenshotBusy(false);
    }
  };

  // Auto-screenshot timer. When `recording` flips to true and the
  // `auto_screenshot_interval_minutes` setting is > 0, fire a
  // captureMonitor() against the primary display every N minutes.
  // Captures use the primary monitor only (no picker UI) — the user
  // already chose multi-display behaviour via the manual button when
  // it matters. Skips silently on capture failure (screen locked, etc.)
  // so the recording isn't interrupted.
  useEffect(() => {
    if (!recording) return;
    let cancelled = false;
    let intervalHandle: ReturnType<typeof setInterval> | null = null;
    (async () => {
      let intervalMin = 0;
      try {
        const s = await api.getSettings();
        intervalMin = Math.max(0, Math.min(60, s.auto_screenshot_interval_minutes || 0));
      } catch {
        return;
      }
      if (intervalMin <= 0 || cancelled) return;
      const fire = async () => {
        if (cancelled) return;
        try {
          const { dir } = await api.getScreenshotDir();
          const win = await import("@tauri-apps/api/window");
          let primary: ScreenMonitor | null = null;
          try {
            const list = await win.availableMonitors();
            if (list.length > 0) {
              const mon = list[0];
              primary = {
                name: mon.name ?? null,
                x: mon.position.x,
                y: mon.position.y,
                width: mon.size.width,
                height: mon.size.height,
                scale: mon.scaleFactor,
              };
            }
          } catch {
            primary = null;
          }
          await captureMonitor(dir, primary);
        } catch {
          // Auto-screenshot is best-effort. A failed capture (screen
          // locked, permission revoked, Tauri restarting) shouldn't
          // surface a toast every N minutes for the user. The manual
          // button still gives the explicit-error UX.
        }
      };
      intervalHandle = setInterval(fire, intervalMin * 60 * 1000);
    })();
    return () => {
      cancelled = true;
      if (intervalHandle != null) clearInterval(intervalHandle);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recording]);

  // Canonical subject key — mirrors backend
  // services/extension_calendar_service.py's `normalize_subject`, which
  // is what the blocklist ALSO matches on (see that service's module
  // docstring). Strips Outlook/OWA's reply/forward/update decoration,
  // collapses whitespace, lowercases. Needed here because extension
  // rows are re-scraped from Outlook Web on every capture and OWA
  // relabels a changed invite "Updated! Weekly Sync" — without this the
  // tile would show a meeting as un-blocked that the backend is (still,
  // correctly) refusing to auto-record.
  const canonicalSubject = (subject: string) => {
    let s = subject || "";
    const prefix = /^\s*(?:re|fw|fwd|updated!?|canceled|cancelled|accepted|declined|tentative)\s*[:!]\s*/i;
    for (;;) {
      const stripped = s.replace(prefix, "");
      if (stripped === s) break;
      s = stripped;
    }
    return s.replace(/\s+/g, " ").trim().toLowerCase();
  };

  // Exact-match block: the user flagged THIS specific subject via the
  // tile's "No auto" toggle. Removable per-meeting. Matched on the raw
  // subject OR its canonical form — same two keys the backend's
  // `is_blocked` tries, so the tile and the trigger loop never disagree
  // about whether a meeting is opted out.
  const isExactBlocked = (subject: string) => {
    const raw = (subject || "").trim().toLowerCase();
    const canon = canonicalSubject(subject);
    return blockedSubjects.some(
      (s) => s.trim().toLowerCase() === raw
        || (!!canon && canonicalSubject(s) === canon));
  };

  // Pattern block: a Settings substring pattern matches this subject.
  // Mirrors the backend's is_blocked() pattern check exactly (case-
  // insensitive substring). Returns the matching pattern (for the
  // tooltip) or null. Not removable per-meeting — the user edits the
  // pattern in Settings.
  const matchingPattern = (subject: string): string | null => {
    const lower = (subject || "").toLowerCase();
    for (const p of blockedPatterns) {
      const pat = (p || "").trim().toLowerCase();
      if (pat && lower.includes(pat)) return p;
    }
    return null;
  };

  // A meeting is blocked if EITHER an exact entry or a pattern matches —
  // same OR the backend applies. Used for the tile's blocked styling.
  const isBlocked = (subject: string) =>
    isExactBlocked(subject) || matchingPattern(subject) !== null;

  const toggleBlock = async (subject: string) => {
    // Pattern-blocked meetings can't be toggled off here — the block
    // comes from a Settings pattern, not a per-meeting flag. Tell the
    // user where to change it instead of silently adding a redundant
    // exact entry that wouldn't un-block anything.
    const pat = matchingPattern(subject);
    if (pat && !isExactBlocked(subject)) {
      toast.info(
        `Skipped by your "${pat}" auto-record pattern. ` +
        `Remove or edit it in Settings → Auto-record skip patterns.`);
      return;
    }
    const blocked = isExactBlocked(subject);
    try {
      const res = blocked
        ? await api.removeAutoRecordBlocklist(subject)
        : await api.addAutoRecordBlocklist(subject);
      setBlockedSubjects(res.subjects);
      setBlockedPatterns(res.patterns);
      toast.success(
        blocked
          ? "Auto-record re-enabled for this meeting"
          : "Won't auto-record this meeting anymore");
    } catch (e) {
      toast.error(
        `Couldn't update: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  // Load the persisted "never auto-record" list once on mount — both the
  // exact subjects and the substring patterns, so tiles can reflect both.
  useEffect(() => {
    api.getAutoRecordBlocklist()
      .then((r) => {
        setBlockedSubjects(r.subjects);
        setBlockedPatterns(r.patterns || []);
      })
      .catch(() => {});
  }, []);

  // Silent auto-refresh when the app window regains focus. When the user
  // tabs back from Outlook after accepting a new meeting, the calendar
  // panel updates without them having to click Refresh. Debounced at
  // 30 seconds so bouncing focus doesn't thrash Outlook COM. The actual
  // fetch + state update lives in the parent (page.tsx) so nav switches
  // don't drop the list.
  useEffect(() => {
    let lastRefreshed = Date.now();
    const onFocus = () => {
      const now = Date.now();
      if (now - lastRefreshed < 30_000) return;
      lastRefreshed = now;
      onRefreshCalendar(true);
      // Also re-pull the blocklist so a pattern the user just added in
      // Settings (a separate view) is reflected on the meeting tiles
      // when they tab back to Record. Cheap, no COM call.
      api.getAutoRecordBlocklist()
        .then((r) => {
          setBlockedSubjects(r.subjects);
          setBlockedPatterns(r.patterns || []);
        })
        .catch(() => {});
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [onRefreshCalendar]);

  const useMeeting = (m: Meeting) => {
    const date = new Date(m.start).toISOString().slice(0, 10);
    setMeetingName(`${m.subject} - ${date}`);
    setAttendees(m.attendees || []);
    // Stash the calendar end time so the auto-stop watchdog can warn
    // / stop us if the meeting runs long. m.end is already an ISO
    // string per the calendar service.
    setScheduledEndIso(m.end || null);

    // Auto-tag Client from attendee email domains using your own
    // tagging history. Intentionally no config — it just learns from
    // whatever you've already tagged. If you tag two meetings with
    // @acme.com attendees to "Acme", the third one auto-fills.
    const suggestion = suggestClientFromAttendees(m.attendees || [], allSessions, client);
    if (suggestion && suggestion !== client) {
      setClient(suggestion);
      toast.info(`Meeting loaded: ${m.subject}`, {
        description: `Auto-tagged client: ${suggestion}`,
      });
    } else {
      toast.info(`Meeting loaded: ${m.subject}`);
    }
  };

  return (
    // space-y-4 (was 6). Design review 2026-08-11: Meeting Details +
    // Audio Devices carried enough slack between them to push Upcoming
    // Meetings below the fold on a 1080p window.
    <div className="mx-auto max-w-5xl space-y-4">
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
          {/* In-bar Co-Pilot toggle. Uses the lightweight
              POST /settings/live-copilot endpoint which (unlike the full
              POST /settings) doesn't refuse during a recording, so the
              user can flip the panel on mid-call. */}
          <div
            className="flex items-center gap-2"
            title="Live Co-Pilot — surfaces clarifying questions / risks / follow-ups every ~45s. Costs an LLM call per tick (~$0.10–$0.20 per hour on Haiku, or $0 with the override set to Ollama)."
          >
            <Sparkles className="h-3.5 w-3.5 text-red-700/80 dark:text-red-300/80" />
            <Label
              htmlFor="copilot-toggle"
              className="text-xs font-medium text-red-900 dark:text-red-200 select-none"
            >
              Co-Pilot
            </Label>
            <Switch
              id="copilot-toggle"
              checked={liveCopilotEnabled}
              onCheckedChange={toggleLiveCopilot}
              disabled={liveCopilotSaving}
            />
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={takeScreenshot}
            disabled={screenshotBusy}
            title="Capture your screen and attach it to this meeting — included as visual context in the summary to Claude"
          >
            {screenshotBusy ? (
              <Loader2 className="h-3.5 w-3.5 mr-2 animate-spin" />
            ) : (
              <Camera className="h-3.5 w-3.5 mr-2" />
            )}
            {screenshotCount > 0 ? `Screenshot (${screenshotCount})` : "Screenshot"}
          </Button>
          <Button variant="destructive" size="sm" onClick={stop}>
            <Square className="h-3.5 w-3.5 mr-2" />
            Stop
          </Button>
        </div>
      )}

      {/* Capture-confidence warning — the single most important banner
          on this page. This is NOT a toast: a toast can be missed while
          the user is looking at the other participants' screen share,
          and by the time they look back the meeting is over and the
          audio is gone. This is why this feature exists in the first
          place — see AGENTS.md "Why this exists". Rendered ABOVE the
          watchdog banner below since "capture may have stopped" is the
          most urgent thing this app can tell you. Reserved red, per the
          design constraints, exactly because this IS the recording-
          failure state. */}
      {recording && recordingStatus?.capture_warning && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-lg border-2 border-red-500 bg-red-50 px-4 py-3 shadow-card dark:border-red-500 dark:bg-red-950/50"
        >
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 animate-pulse text-red-600 dark:text-red-400" />
          <div className="flex-1">
            <div className="text-sm font-semibold text-red-900 dark:text-red-100">
              Capture problem detected
            </div>
            <div className="mt-0.5 text-sm text-red-800 dark:text-red-200">
              {recordingStatus.capture_warning}
            </div>
          </div>
        </div>
      )}

      {/* Live capture meters — the instrument this feature exists to
          build. Legible at a glance: labelled bars + a plain-language
          state per stream, so "is it actually recording" never again
          depends on watching whether the live transcript is still
          producing text. */}
      {recording && (
        <Card className="gap-2 py-3.5">
          <CardContent className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <CaptureMeter
              label="Microphone"
              level={recordingStatus?.mic_level ?? 0}
              state={recordingStatus?.mic_state ?? null}
              kind="mic"
            />
            <CaptureMeter
              label="System Audio"
              level={recordingStatus?.system_level ?? 0}
              state={recordingStatus?.system_state ?? null}
              kind="system"
            />
          </CardContent>
        </Card>
      )}

      {/* Screenshot strip — thumbnails appear as each capture completes
          (manual button or auto-timer both funnel through
          captureMonitor, which pushes here directly; no polling), plus
          the destination folder up front so "where did that go" never
          comes up. Matches the capture-meters card above it: same
          Card/CardContent shell, same muted/border tokens. */}
      {recording && (
        <Card className="gap-2 py-3.5">
          <CardHeader>
            <CardTitle className="flex flex-wrap items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <Camera className="h-4 w-4 text-primary" />
                Screenshots {screenshotEntries.length > 0 && `(${screenshotEntries.length})`}
              </span>
              {screenshotDirPath && (
                <div className="flex min-w-0 items-center gap-1.5 text-xs font-normal text-muted-foreground">
                  <FolderOpen className="h-3.5 w-3.5 shrink-0" />
                  <span className="max-w-[280px] truncate" title={screenshotDirPath}>
                    {screenshotDirPath}
                  </span>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={copyScreenshotDir}
                    title="Copy folder path — no in-app 'open folder' command exists yet, so copy and paste it into your file manager"
                    className="shrink-0"
                  >
                    {screenshotDirCopied ? (
                      <Check className="h-3.5 w-3.5" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                  </Button>
                </div>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {screenshotEntries.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No screenshots yet — click Screenshot above to capture one.
              </p>
            ) : (
              // Single row, horizontal scroll: caps the strip's height
              // and keeps the DOM flat (no wrapping/virtualization
              // needed) even with dozens of captures in a long meeting.
              <div className="flex gap-2 overflow-x-auto pb-1">
                {[...screenshotEntries]
                  .sort((a, b) => a.index - b.index)
                  .map((s) => {
                    const src = screenshotLoadFailed.has(s.index) ? null : screenshotImgSrc(s.index);
                    return (
                      <button
                        key={s.index}
                        type="button"
                        onClick={() => setZoomedScreenshot(s.index)}
                        className="group relative h-16 w-24 shrink-0 overflow-hidden rounded-md border border-border bg-muted/40 transition hover:ring-2 hover:ring-primary"
                        title={`Screenshot ${s.index + 1}${s.takenAt ? " · " + new Date(s.takenAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}`}
                      >
                        {src ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img
                            src={src}
                            alt={`Screenshot ${s.index + 1}`}
                            className="h-full w-full object-cover"
                            loading="lazy"
                            onError={() => markScreenshotLoadFailed(s.index)}
                          />
                        ) : (
                          // Neutral placeholder — a screenshot that was
                          // taken must never render as one that wasn't.
                          // Expected to be rare now that the strip reads
                          // from the live in-memory session (see
                          // screenshotImgSrc), but a request can still
                          // fail — screen-locked capture, a backend
                          // hiccup, the file not finished writing yet.
                          <div className="flex h-full w-full flex-col items-center justify-center gap-0.5 text-muted-foreground">
                            <ImageIcon className="h-4 w-4" />
                          </div>
                        )}
                        <span className="absolute bottom-0.5 right-0.5 rounded bg-black/60 px-1 py-0.5 text-[9px] leading-none text-white">
                          {s.index + 1}
                        </span>
                        {s.takenAt && (
                          <span className="absolute left-0.5 top-0.5 rounded bg-black/60 px-1 py-0.5 text-[9px] leading-none text-white">
                            {new Date(s.takenAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                          </span>
                        )}
                      </button>
                    );
                  })}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Auto-stop watchdog warning banner — appears underneath the
          recording bar when the backend detects dead air, meeting
          overrun, or imminent hard cap. Render order is "most recent
          warning at the top" because the watchdog only emits one or
          two warnings at a time and stacking is rare. */}
      {recording && (watchdogWarnings?.length ?? 0) > 0 && (
        <div className="space-y-2">
          {(watchdogWarnings || []).map((w) => (
            <div
              key={w.code}
              className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2.5 text-sm dark:border-amber-900/60 dark:bg-amber-950/30"
            >
              <span className="mt-0.5 text-amber-600 dark:text-amber-400">⚠</span>
              <div className="flex-1 text-amber-900 dark:text-amber-100">
                {w.message}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Live transcript stream — only mounts during an active recording.
          Subscribes to SSE itself; we just give it the recording flag. */}
      <LiveTranscriptPanel recording={recording} />

      {/* Live Co-Pilot — polls /recording/copilot/tick every ~45s while
          recording (opt-in via Settings.live_copilot_enabled). Renders
          three short bullet lists alongside the transcript. */}
      <CoPilotPanel recording={recording} enabled={liveCopilotEnabled} />

      {/* In-call semantic search — query past meetings without leaving
          the recording view. Reuses the cross-meeting Q&A pipeline; it
          just exposes that capability beside the live transcript. */}
      <LiveSearchPanel recording={recording} />

      {/* Meeting details */}
      <Card className="gap-3 py-3.5">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            Meeting Details
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3.5">
          {/* Row 1: Meeting name (full width) */}
          <div className="space-y-2">
            <Label htmlFor="mtg-name">Meeting Name</Label>
            <Input
              id="mtg-name"
              value={meetingName}
              onChange={(e) => setMeetingName(e.target.value)}
              placeholder="e.g. Design Review — 2026-04-20"
              autoComplete="off"
            />
            {recording && (
              <p className="text-[11px] text-muted-foreground italic">
                Editing during recording — changes auto-save to the active session.
              </p>
            )}
          </div>

          {/* Row 2: Template (full width, since it's a key choice) */}
          <div className="space-y-2">
            <Label>Template</Label>
            <Select value={template} onValueChange={(v) => v && setTemplate(v)}>
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
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="client-input">Client</Label>
              <Input
                id="client-input"
                list="clients-list"
                value={client}
                onChange={(e) => setClient(e.target.value)}
                placeholder="Type new or pick existing"
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
                placeholder="Type new or pick existing"
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
      <Card className="gap-3 py-3.5">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Mic className="h-4 w-4 text-primary" />
            Audio Devices
          </CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {/* Readiness checklist (Part 3) — replaces "watch the live
              transcript and hope" with an explicit pre-flight check.
              Reuses device lists / backend reachability / the calendar
              endpoint already fetched above; no new polling. */}
          {!recording && (
            <div className="md:col-span-2 space-y-1 rounded-lg border border-border bg-muted/30 p-3">
              <div className="flex items-center gap-2 pb-1.5">
                {readyToRecord ? (
                  <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                ) : (
                  <XCircle className="h-4 w-4 text-red-600 dark:text-red-400" />
                )}
                <span
                  className={
                    "text-sm font-semibold " +
                    (readyToRecord
                      ? "text-emerald-700 dark:text-emerald-400"
                      : "text-red-700 dark:text-red-400")
                  }
                >
                  {readyToRecord ? "Ready to record" : "Not ready to record"}
                </span>
              </div>
              <div className="divide-y divide-border/60">
                <ReadinessRow
                  status={micReady ? "pass" : "fail"}
                  title="Microphone"
                  detail={
                    micReady
                      ? (selectedMic?.name ?? "Selected")
                      : micIdx === null
                        ? "No microphone selected"
                        : "Selected microphone not found"
                  }
                  hint="Choose a microphone below before recording."
                />
                <ReadinessRow
                  status={conferenceRoomMode ? "pass" : systemAudioReady ? "pass" : "warn"}
                  title="System audio"
                  detail={
                    conferenceRoomMode
                      ? "Skipped — conference room mode"
                      : systemAudioReady
                        ? (selectedOut?.name ?? "Selected")
                        : "No system-audio device selected"
                  }
                  hint="Select a system-audio device below, or turn on conference room mode for a room setup. Recording still works with mic only — you'll just miss the other side."
                />
                <ReadinessRow
                  status={backendReachable ? "pass" : "fail"}
                  title="Backend"
                  detail={backendReachable ? "Connected" : "Unreachable"}
                  hint="The app automatically restarts a crashed backend — wait a few seconds and this should clear on its own."
                />
                <ReadinessRow
                  status={calendarAvailable ? "pass" : "info"}
                  title="Calendar"
                  detail={
                    calendarAvailable === null
                      ? "Checking…"
                      : calendarAvailable
                        ? "Connected"
                        : "Not connected"
                  }
                  hint="Optional — connect a calendar in Settings for upcoming meetings and auto-record. Recording works fine without it."
                />
              </div>
            </div>
          )}
          <div className="space-y-2">
            <Label>Microphone</Label>
            <Select
              value={micIdx?.toString() ?? ""}
              onValueChange={(v: string | null) => setMicIdx(v ? parseInt(v) : null)}
              disabled={recording}
            >
              <SelectTrigger className="w-full">
                <span className="truncate text-left">
                  {selectedMic ? selectedMic.name : "Select mic..."}
                </span>
              </SelectTrigger>
              <SelectContent>
                {inputDevices.map((d) => (
                  <SelectItem key={d.index} value={d.index.toString()}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label className={conferenceRoomMode ? "opacity-50" : ""}>
              System Audio (loopback)
            </Label>
            <Select
              value={outIdx?.toString() ?? ""}
              onValueChange={(v: string | null) => setOutIdx(v ? parseInt(v) : null)}
              disabled={recording || conferenceRoomMode}
            >
              <SelectTrigger className="w-full">
                <span className="truncate text-left">
                  {conferenceRoomMode
                    ? "Disabled (conference room mode)"
                    : selectedOut ? selectedOut.name : "Select speaker..."}
                </span>
              </SelectTrigger>
              <SelectContent>
                {outputDevices.map((d) => (
                  <SelectItem key={d.index} value={d.index.toString()}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-start gap-3 pt-1">
            <Switch
              id="conference-room-mode"
              checked={conferenceRoomMode}
              disabled={recording}
              onCheckedChange={(v) => {
                setConferenceRoomMode(v);
                try { localStorage.setItem("conferenceRoomMode", v ? "1" : "0"); }
                catch { /* localStorage may be unavailable; toggle still works */ }
              }}
            />
            <div className="space-y-0.5 leading-tight">
              <div className="flex items-center gap-1">
                <Label htmlFor="conference-room-mode" className="cursor-pointer">
                  Conference room mode
                </Label>
                <InfoTip label="About conference room mode">
                  Mic captures everyone in the room. System-audio loopback
                  is skipped — use this when nobody is on speakers and the
                  laptop sits in the middle of the table.
                </InfoTip>
              </div>
              <p className="text-xs text-muted-foreground">
                Mic captures the whole room; system audio is skipped.
              </p>
            </div>
          </div>
          {audioSyncRisk?.level === "warn" && !recording && !conferenceRoomMode && (
            <div className="md:col-span-2 rounded-lg border border-amber-400/60 bg-amber-50 dark:bg-amber-950/30 p-3 text-sm">
              <p className="font-medium text-amber-900 dark:text-amber-200">
                Audio format mismatch — long recordings will drift
              </p>
              <p className="mt-1 text-xs text-amber-900/80 dark:text-amber-200/80">
                {audioSyncRisk.reason}
              </p>
              {audioSyncRisk.fix_hint && (
                <p className="mt-1 text-xs text-amber-900/80 dark:text-amber-200/80">
                  {audioSyncRisk.fix_hint}
                </p>
              )}
              <button
                type="button"
                onClick={() => {
                  openSystemSettings("sound").catch((e) =>
                    toast.error(`Could not open Sound settings: ${e instanceof Error ? e.message : e}`));
                }}
                className="mt-2 inline-flex items-center gap-1.5 rounded-md bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700"
              >
                Open Sound Control Panel
              </button>
            </div>
          )}
        </CardContent>
        {!recording && (
          // Was `px-6 pb-5 pt-1 … pt-4 mt-2` (the duplicate pt- classes
          // meant pt-4 silently won). Tightened to a slim action band so
          // Start Recording stops sitting alone in a tall empty strip.
          <div className="mt-1 flex justify-end border-t border-border/50 px-4 pt-3">
            <Button size="lg" onClick={start} className="bg-red-600 hover:bg-red-700 text-white px-7 h-10">
              <Play className="h-4 w-4 mr-2 fill-current" />
              Start Recording
            </Button>
          </div>
        )}
      </Card>

      {/* Upcoming meetings */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <CalendarIcon className="h-4 w-4 text-primary" />
            Upcoming Meetings
          </CardTitle>
          <CardAction className="flex items-center gap-3">
            <div className="flex items-center gap-2">
              <Switch
                id="auto-record-toggle"
                checked={autoRecord}
                disabled={autoRecordSaving}
                onCheckedChange={handleAutoRecordToggle}
              />
              <Label
                htmlFor="auto-record-toggle"
                className="text-xs font-medium cursor-pointer select-none"
                title={
                  "Auto-start recording at each calendar meeting's scheduled time — from this machine's " +
                  "calendar AND from meetings the Chrome extension sees in Outlook Web. " +
                  "Filters: skip all-day events, and anything you flagged 'No auto'. " +
                  "Manual recordings always win — auto-start won't fire while you're already recording."
                }
              >
                Auto-record
              </Label>
            </div>
            <Button size="sm" variant="outline" onClick={() => onRefreshCalendar(false)} disabled={meetingsLoading}>
              {meetingsLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Refresh"}
            </Button>
          </CardAction>
        </CardHeader>
        {autoRecord && autoRecordNext && (
          <div className="px-6 -mt-2 mb-2 text-xs text-muted-foreground">
            Auto-record on — next:{" "}
            <span className="font-medium text-foreground">
              {autoRecordNext.subject}
            </span>{" "}
            at{" "}
            {new Date(autoRecordNext.start).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </div>
        )}
        <CardContent>
          {meetings.length === 0 ? (
            calendarStatus?.source === "extension" ? (
              // An empty extension-sourced calendar must never render
              // identically to a genuinely free one — that ambiguity is
              // how a whole day of meetings went missing without the
              // user realizing the mode itself was at fault (field
              // report 2026-08-13). Same muted/centered empty-state
              // styling as the plain message below, just with the
              // extra honesty of a last-capture time and a next step.
              <div className="text-sm text-muted-foreground text-center py-8 space-y-1.5">
                <p className="font-medium text-foreground/80">
                  No meetings from the Chrome extension.
                </p>
                <p>
                  {calendarStatus.last_capture_at
                    ? `Last capture: ${timeAgoLabel(calendarStatus.last_capture_at)} · ` +
                      `${calendarStatus.event_count ?? 0} event` +
                      `${(calendarStatus.event_count ?? 0) === 1 ? "" : "s"} on file` +
                      // Which calendar-parse path produced those events —
                      // "structured" (client-side aria-label extraction,
                      // no LLM) vs. "text-fallback" (LLM-recovered after
                      // the extension's own scan came up empty/absent) —
                      // so this never renders identically regardless of
                      // which path ran (field report chain culminating
                      // 2026-08-14).
                      ((calendarStatus.event_count ?? 0) > 0 && calendarStatus.last_import_path
                        ? calendarStatus.last_import_path === "structured"
                          ? " (extension's structured capture)"
                          : " (recovered from text)"
                        : "")
                    : "The extension has never sent a capture."}
                </p>
                <p>
                  Open Outlook Web and click <strong>Capture &amp; Send</strong> in
                  the Meeting Recorder Chrome extension.
                </p>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground text-center py-8">
                No upcoming meetings in the next 7 days, or Outlook not connected.
              </p>
            )
          ) : (
            <div className="space-y-2">
              {meetings.map((m, i) => {
                const start = new Date(m.start);
                const end = new Date(m.end);
                const now = new Date();
                const live = start <= now && now <= end;
                const past = end < now;
                const key = meetingKey(m);
                const open = expandedMeeting === key;
                const det = meetingDetails[key];
                return (
                  <div
                    key={i}
                    className={`rounded-lg border transition-colors ${
                      live ? "bg-red-50 border-red-200 dark:bg-red-950/40 dark:border-red-900"
                        : past ? "opacity-60" : "hover:bg-muted/40"
                    }`}
                  >
                    <div className="flex items-center gap-4 p-3">
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
                      <button
                        type="button"
                        onClick={() => toggleMeetingDetail(m)}
                        className="flex-1 min-w-0 flex items-start gap-2 text-left"
                        title="Show attendees, agenda, and join link"
                      >
                        {open
                          ? <ChevronDown className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground" />
                          : <ChevronRight className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground" />}
                        <span className="min-w-0">
                          <span className="block truncate text-sm font-medium">{m.subject}</span>
                          {m.location && (
                            <span className="block text-xs text-muted-foreground truncate">{m.location}</span>
                          )}
                        </span>
                      </button>
                      {isExtensionSourced(m) && (
                        <Badge
                          variant="outline"
                          className="shrink-0 text-[10px] font-normal text-muted-foreground"
                          title={
                            "Seen by the Chrome extension in Outlook Web, not by the calendar on this machine. " +
                            "Auto-record treats it like any other meeting — the trigger reads the same merged " +
                            "list this panel does. Only the agenda/attendee details are missing, because those " +
                            "come from the local calendar."
                          }
                        >
                          From Outlook Web
                        </Badge>
                      )}
                      <span className="text-xs text-muted-foreground">{m.duration}m</span>
                      {!past && (
                        <>
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => setBriefMeeting(m)}
                            disabled={recording}
                            title="Generate a pre-meeting brief from prior calls with these attendees"
                            className="px-2"
                          >
                            <Sparkles className="h-3.5 w-3.5 mr-1" />
                            Brief
                          </Button>
                          {/* "Manual only" is now reserved for rows
                              auto-record genuinely cannot start. That used
                              to include every extension-sourced row —
                              AutoRecordService polled the LOCAL calendar for
                              its trigger, so an Outlook-Web-only meeting
                              never came up for auto-start and offering a
                              block control would have been theatre. It reads
                              the same merged two-source list as this panel
                              now (backend/services/calendar_feed.py), so
                              extension rows get the real control.

                              What remains honestly manual-only: ALL-DAY
                              events. auto_record_service._is_all_day
                              excludes them from the trigger regardless of
                              source (birthdays, OOO and travel blocks are
                              not meetings), so a block button on one would
                              be the same theatre, just moved. */}
                          {!canAutoRecord(m) ? (
                            <span
                              className="px-2 text-xs text-muted-foreground select-none"
                              title={
                                "Auto-record never fires for all-day events — they're OOO/travel/birthday " +
                                "blocks, not meetings. Start it manually with Use if you need it recorded."
                              }
                            >
                              Manual only
                            </span>
                          ) : (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => toggleBlock(m.subject)}
                            title={
                              matchingPattern(m.subject) && !isExactBlocked(m.subject)
                                ? `Skipped by your "${matchingPattern(m.subject)}" auto-record pattern (Settings → Auto-record skip patterns).`
                                : isExactBlocked(m.subject)
                                  ? "On your never-auto-record list. Click to allow auto-record again."
                                  : "Never auto-record this meeting (applies to the whole recurring series, permanently)"
                            }
                            className={`px-2 ${
                              isBlocked(m.subject)
                                ? "text-amber-600 dark:text-amber-500"
                                : "text-muted-foreground"
                            }`}
                          >
                            <Ban className="h-3.5 w-3.5 mr-1" />
                            {matchingPattern(m.subject) && !isExactBlocked(m.subject)
                              ? "Skipped (pattern)"
                              : isExactBlocked(m.subject)
                                ? "Auto-record off"
                                : "No auto"}
                          </Button>
                          )}
                          <Button size="sm" variant="outline" onClick={() => useMeeting(m)} disabled={recording}>
                            Use
                          </Button>
                        </>
                      )}
                    </div>

                    {open && (
                      <div className="border-t px-4 py-3 space-y-3 text-sm">
                        {det?.loading && (
                          <div className="flex items-center gap-2 text-xs text-muted-foreground">
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            Loading meeting details…
                          </div>
                        )}
                        {det?.error && (
                          <div className="text-xs text-destructive">
                            Couldn&apos;t load details: {det.error}
                          </div>
                        )}
                        {det?.data && (
                          <>
                            {det.data.join_url && (
                              <div className="space-y-1">
                                <button
                                  type="button"
                                  onClick={() => openExternal(det.data!.join_url!)}
                                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90"
                                >
                                  <ExternalLink className="h-3.5 w-3.5" />
                                  Join meeting
                                </button>
                                {/* The raw URL as an actual clickable
                                    link (the app-wide handler opens it
                                    in the real browser) and still
                                    selectable to copy as a fallback. */}
                                <a
                                  href={det.data.join_url}
                                  className="block text-[11px] text-primary underline break-all select-all hover:opacity-80"
                                >
                                  {det.data.join_url}
                                </a>
                              </div>
                            )}
                            {(() => {
                              // The backend's type promises `attendees`
                              // is always an array, but a partial/error
                              // response can omit it. Distinguish that
                              // from a real "no attendees" — an item
                              // that failed to load must never render as
                              // one that simply doesn't exist.
                              const attendeesKnown = Array.isArray(det.data.attendees);
                              const attendees = det.data.attendees ?? [];
                              return (
                                <div>
                                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                                    Attendees{attendeesKnown ? ` (${attendees.length})` : ""}
                                  </div>
                                  {!attendeesKnown ? (
                                    <div className="text-xs text-muted-foreground">Attendee list unavailable.</div>
                                  ) : attendees.length === 0 ? (
                                    <div className="text-xs text-muted-foreground">None listed.</div>
                                  ) : (
                                    <div className="flex flex-wrap gap-1 max-h-28 overflow-auto">
                                      {attendees.map((a, ai) => (
                                        <span
                                          key={ai}
                                          className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-foreground/80"
                                        >
                                          {a}
                                        </span>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              );
                            })()}
                            <div>
                              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                                Agenda / invite body
                              </div>
                              {det.data.body ? (
                                <div className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/40 p-2 text-xs leading-relaxed">
                                  {det.data.body}
                                </div>
                              ) : (
                                <div className="text-xs text-muted-foreground">
                                  (No description on this invite.)
                                </div>
                              )}
                            </div>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Session view after recording — open in dialog for full tabs */}
      {session && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-primary" />
              Just Recorded: {session.display_name || `Session ${session.session_id}`}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex items-center justify-between">
            <div className="text-sm text-muted-foreground">
              Audio saved. Open the session to transcribe, summarize, and extract.
            </div>
            <Button onClick={() => onOpenSession(session.session_id)}>
              Open Session
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Pre-meeting brief — opens when the user clicks Brief on a
          calendar tile. Self-contained: fetches the brief, resolves
          client/project from attendees, and offers a "Use for
          recording" handoff that pre-fills the recording form (same
          effect as clicking Use directly). */}
      <MeetingBriefModal
        open={briefMeeting !== null}
        onOpenChange={(open) => { if (!open) setBriefMeeting(null); }}
        meeting={briefMeeting}
        allSessions={allSessions}
        onOpenSession={(id) => onOpenSession(id)}
        onUseForRecording={(m, c, p) => {
          // Mirror the behaviour of useMeeting() but skip the auto-tag
          // round-trip — the modal already resolved client + project.
          const date = new Date(m.start).toISOString().slice(0, 10);
          setMeetingName(`${m.subject} - ${date}`);
          setAttendees(m.attendees || []);
          setScheduledEndIso(m.end || null);
          if (c) setClient(c);
          if (p) setProject(p);
        }}
      />

      <Dialog
        open={monitorPicker !== null}
        onOpenChange={(open) => { if (!open) setMonitorPicker(null); }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Which screen?</DialogTitle>
            <DialogDescription>
              You have {monitorPicker?.monitors.length ?? 0} displays. Pick the
              one to capture — it&apos;s attached to this meeting and sent to
              Claude as visual context.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            {monitorPicker && monitorPicker.monitors.map((m, i) => {
              const primary = m.x === 0 && m.y === 0;
              const side =
                m.x > 0 ? "right" : m.x < 0 ? "left"
                  : m.y > 0 ? "below" : m.y < 0 ? "above" : "primary";
              return (
                <button
                  key={i}
                  type="button"
                  disabled={screenshotBusy}
                  onClick={async () => {
                    const dir = monitorPicker.dir;
                    setMonitorPicker(null);
                    await captureMonitor(dir, m);
                  }}
                  className="flex w-full items-center justify-between rounded-lg border p-3 text-left transition hover:bg-muted/50 disabled:opacity-60"
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">
                      Display {i + 1}
                      {m.name ? ` — ${m.name}` : ""}
                      {primary && (
                        <span className="ml-2 text-[10px] uppercase tracking-wide text-muted-foreground">
                          primary
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {Math.round(m.width)}×{Math.round(m.height)}
                      {!primary && ` · ${side} of main`}
                    </div>
                  </div>
                  <Camera className="h-4 w-4 shrink-0 text-muted-foreground" />
                </button>
              );
            })}
          </div>
        </DialogContent>
      </Dialog>

      {/* Screenshot zoom view — reuses the same Dialog primitive as the
          monitor picker above rather than a bespoke lightbox. Falls
          back to the same neutral placeholder as the strip thumbnail
          when the image can't be fetched yet. */}
      {(() => {
        const zoomedIndex = zoomedScreenshot;
        const zoomedEntry = zoomedIndex !== null
          ? screenshotEntries.find((s) => s.index === zoomedIndex)
          : undefined;
        const zoomedSrc = zoomedIndex !== null && !screenshotLoadFailed.has(zoomedIndex)
          ? screenshotImgSrc(zoomedIndex)
          : null;
        return (
          <Dialog
            open={zoomedIndex !== null}
            onOpenChange={(open) => { if (!open) setZoomedScreenshot(null); }}
          >
            <DialogContent className="sm:max-w-2xl">
              <DialogHeader>
                <DialogTitle>
                  Screenshot {zoomedIndex !== null ? zoomedIndex + 1 : ""}
                </DialogTitle>
                <DialogDescription>
                  {zoomedEntry
                    ? `Captured at ${new Date(zoomedEntry.takenAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
                    : "Captured during this recording."}
                </DialogDescription>
              </DialogHeader>
              <div className="flex min-h-[16rem] items-center justify-center rounded-lg bg-muted/30 p-2">
                {zoomedSrc ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={zoomedSrc}
                    alt={`Screenshot ${(zoomedIndex ?? 0) + 1}`}
                    className="max-h-[70vh] w-full rounded-md object-contain"
                    onError={() => zoomedIndex !== null && markScreenshotLoadFailed(zoomedIndex)}
                  />
                ) : (
                  <div className="flex flex-col items-center gap-2 py-10 text-muted-foreground">
                    <ImageIcon className="h-8 w-8" />
                    <p className="max-w-xs text-center text-sm">
                      Couldn&apos;t load this image right now — it&apos;s
                      still attached to the recording either way.
                    </p>
                  </div>
                )}
              </div>
            </DialogContent>
          </Dialog>
        );
      })()}
    </div>
  );
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

// "Last capture: 12 min ago" / "2h 15m ago" — used by the extension
// calendar-source empty state so staleness is obvious (field report
// 2026-08-13: a store that stopped updating looked identical to a
// genuinely empty calendar).
function timeAgoLabel(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const mins = Math.max(0, Math.round((Date.now() - t) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m ago`;
}

function formatDur(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * One live capture-confidence meter (Part 2). Reads mic_level/mic_state
 * or system_level/system_state straight off the shared /recording/
 * status poll — no local state, no separate timer. The bar's width
 * transitions smoothly between polls (see the RECORDING_POLL_INTERVAL_MS
 * comment in recording-status.ts for why the poll itself is fast enough
 * that this reads as "live" rather than laggy).
 *
 * Color carries real meaning, not decoration: emerald only while audio
 * is actually flowing, a neutral/muted tone for silence (NOT a
 * failure — a muted mic or a one-sided conversation is normal), and
 * red is reserved for "dead" — chunks have stopped arriving entirely,
 * which is the one state this whole feature exists to catch.
 */
function CaptureMeter({
  label,
  level,
  state,
  kind,
}: {
  label: string;
  level: number;
  state: "flowing" | "silent" | "dead" | null;
  kind: "mic" | "system";
}) {
  const pct = Math.max(0, Math.min(1, level || 0)) * 100;
  const FlowIcon = kind === "mic" ? Mic : Volume2;
  const QuietIcon = kind === "mic" ? MicOff : VolumeX;

  let barClass = "bg-muted-foreground/25";
  let StatusIcon = QuietIcon;
  let iconClass = "text-muted-foreground";
  let stateText =
    kind === "system" ? "Not captured for this recording" : "Waiting for audio…";
  let stateClass = "text-muted-foreground";

  if (state === "flowing") {
    barClass = "bg-emerald-500 dark:bg-emerald-400";
    StatusIcon = FlowIcon;
    iconClass = "text-emerald-600 dark:text-emerald-400";
    stateText = "Flowing";
    stateClass = "text-emerald-700 dark:text-emerald-400";
  } else if (state === "silent") {
    barClass = "bg-muted-foreground/40";
    StatusIcon = QuietIcon;
    iconClass = "text-muted-foreground";
    stateText = "Silent — quiet or muted";
    stateClass = "text-muted-foreground";
  } else if (state === "dead") {
    barClass = "bg-red-500";
    StatusIcon = AlertTriangle;
    iconClass = "text-red-600 dark:text-red-400";
    stateText = "Not receiving audio";
    stateClass = "text-red-700 dark:text-red-400 font-medium";
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <StatusIcon className={`h-4 w-4 shrink-0 ${iconClass}`} />
          {label}
        </div>
        <span className={`text-xs whitespace-nowrap ${stateClass}`}>{stateText}</span>
      </div>
      {/* The meter itself — deliberately large enough (h-3) to read at
          a glance from across a desk, per the design constraints. */}
      <div className="h-3 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={`h-full rounded-full transition-[width] duration-200 ease-out ${barClass}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

type ReadinessStatus = "pass" | "warn" | "fail" | "info";

/**
 * One row of the pre-recording readiness checklist (Part 3). `status`
 * drives both the icon and color; `hint` (the "here's what to do" line)
 * only renders for anything short of a clean pass, per the design spec.
 */
function ReadinessRow({
  status,
  title,
  detail,
  hint,
}: {
  status: ReadinessStatus;
  title: string;
  detail: string;
  hint?: string;
}) {
  const cfg: Record<ReadinessStatus, { Icon: typeof CheckCircle2; cls: string }> = {
    pass: { Icon: CheckCircle2, cls: "text-emerald-600 dark:text-emerald-400" },
    warn: { Icon: AlertTriangle, cls: "text-amber-600 dark:text-amber-400" },
    fail: { Icon: XCircle, cls: "text-red-600 dark:text-red-400" },
    // Calendar-disconnected lands here, never on "fail" — degrading
    // gracefully means it must never read as something blocking
    // recording. See AGENTS.md Part 3.
    info: { Icon: Info, cls: "text-muted-foreground" },
  };
  const { Icon, cls } = cfg[status];

  return (
    <div className="flex items-start gap-2.5 py-1.5">
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${cls}`} />
      <div className="min-w-0 flex-1">
        <div className="text-sm">
          <span className="font-medium">{title}</span>
          <span className="text-muted-foreground"> — {detail}</span>
        </div>
        {hint && status !== "pass" && (
          <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>
        )}
      </div>
    </div>
  );
}

/**
 * Pick a client for a new calendar meeting based on attendee email
 * domains that have historically been tagged to an existing client.
 *
 * Strategy: for each existing session, match on *external* email
 * domains (anything that isn't the user's own domain — more below).
 * Sum up per-client matches across all sessions, pick the winner.
 *
 * Why external only: every session the user is in includes their own
 * work email (e.g. @your-company.com). Counting that would make every
 * meeting "look like" every client the user has worked with. Internal
 * domains are detected as "the domain that appears most across ALL
 * sessions" — your own email — and filtered out.
 *
 * Returns null when there's no clear match (no overlapping domains,
 * or a tie) — in that case the user types the client manually like
 * they always have.
 */
function suggestClientFromAttendees(
  meetingAttendees: string[],
  allSessions: SessionSummary[],
  currentClient: string,
): string | null {
  if (currentClient.trim()) return null; // don't override an existing pick
  const meetingDomains = extractDomains(meetingAttendees);
  if (meetingDomains.size === 0) return null;

  // Detect the user's own domain as the one that shows up in the most
  // sessions overall. This is self-calibrating — works regardless of
  // company, no config needed.
  const sessionsWithDomain = new Map<string, number>();
  for (const s of allSessions) {
    const d = extractDomains(s.attendees || []);
    for (const dom of d) {
      sessionsWithDomain.set(dom, (sessionsWithDomain.get(dom) ?? 0) + 1);
    }
  }
  const ownDomain = [...sessionsWithDomain.entries()]
    .sort((a, b) => b[1] - a[1])[0]?.[0];

  // Score each client by how many prior sessions share an external
  // domain with the new meeting.
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
  const ranked = [...scores.entries()].sort((a, b) => b[1] - a[1]);
  // Tie → punt, let the user pick. Clear winner → use it.
  if (ranked.length > 1 && ranked[0][1] === ranked[1][1]) return null;
  return ranked[0][0];
}

function extractDomains(addresses: string[]): Set<string> {
  const out = new Set<string>();
  for (const a of addresses) {
    const at = a.lastIndexOf("@");
    if (at < 0) continue;
    const domain = a.slice(at + 1).trim().toLowerCase();
    if (domain) out.add(domain);
  }
  return out;
}

// Fire an OS-native notification for an auto-stop watchdog event.
// Mirrors the calendar-monitor pattern: dynamic-import the Tauri
// notification plugin so the web build doesn't pull it in, request
// permission lazily, then send. Best-effort — errors are silently
// swallowed; the in-app banner is the canonical surfacing of warnings,
// and the toast layer also fires alongside.
async function fireNativeNotification(code: string, message: string) {
  try {
    const { sendNotification, isPermissionGranted, requestPermission } =
      await import("@tauri-apps/plugin-notification");
    let granted = await isPermissionGranted();
    if (!granted) {
      const perm = await requestPermission();
      granted = perm === "granted";
    }
    if (!granted) return;
    // Keep titles short and consistent so users can distinguish auto-
    // stop alerts from calendar pre-meeting pings at a glance.
    const title = code.endsWith("_stop") || code === "hard_cap_hit"
      ? "Recording auto-stopped"
      : "Recording warning";
    await sendNotification({ title, body: message });
  } catch {
    // Plugin not available in this context (web preview, etc.) —
    // banner + toast are still showing inside the app.
  }
}
