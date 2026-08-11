"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, openExternal, openSystemSettings, setRecordingActive, type AudioDevice, type AudioSyncRisk, type Meeting, type RecordingStatus, type SessionFull, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import {
  Calendar as CalendarIcon,
  Sparkles,
  Loader2,
  Square,
  Play,
  Mic,
  FileText,
  Camera,
  Ban,
  ChevronRight,
  ChevronDown,
  ExternalLink,
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

  const [recording, setRecording] = useState(false);
  const [duration, setDuration] = useState(0);
  // The active recording's session_id, captured from /recording/status.
  // Lets mid-recording form edits PATCH the right session without
  // having to ask the backend again on every keystroke.
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  // Screenshots captured during the current recording. Count is shown
  // on the button; the files themselves are attached server-side and
  // fed to the summarizer as visual context.
  const [screenshotBusy, setScreenshotBusy] = useState(false);
  const [screenshotCount, setScreenshotCount] = useState(0);
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

  const toggleMeetingDetail = (m: Meeting) => {
    const key = meetingKey(m);
    if (expandedMeeting === key) {
      setExpandedMeeting(null);
      return;
    }
    setExpandedMeeting(key);
    if (meetingDetails[key]?.data || meetingDetails[key]?.loading) return;
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
  const [modelsLoading, setModelsLoading] = useState(false);
  // Auto-stop watchdog warnings, polled from /recording/status while
  // recording. Used to render a banner under the recording bar and
  // to fire native OS notifications (once per code per session, so
  // we don't spam the user with the same toast every second).
  const [watchdogWarnings, setWatchdogWarnings] = useState<RecordingStatus["warnings"]>([]);
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

  // Load initial data. Calendar data is owned by the parent (page.tsx)
  // so it survives nav switches; we only load local things here.
  useEffect(() => {
    (async () => {
      try {
        // Fast-path: everything local (device list, templates, sessions)
        const [devices, tpls, status, sessionsList, cfgs] = await Promise.all([
          api.getAudioDevices(),
          api.getTemplates(),
          api.recordingStatus(),
          api.listSessions().catch(() => []),
          api.getClientConfigs().catch(() => ({} as Record<string, { export_folder: string; display_name?: string }>)),
        ]);
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

        setRecording(status.is_recording);
        setDuration(status.duration_s);
        setModelsLoading(status.models_loading);

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

  // Look up the currently selected device objects for display.
  const selectedMic = inputDevices.find((d) => d.index === micIdx);
  const selectedOut = outputDevices.find((d) => d.index === outIdx);

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

  // Poll recording status ALWAYS — not just when this view thinks
  // it's recording. Auto-record starts a recording without this view
  // knowing; without continuous polling the form would stay visible
  // even though a recording is in progress. CRITICAL for the
  // 4h17m-orphan scenario where auto-record fired and the user had
  // no UI signal it was happening.
  useEffect(() => {
    const t = setInterval(async () => {
      try {
        const s = await api.recordingStatus();
        setDuration(s.duration_s);
        // Auto-stop watchdog warnings — render banners + fire native
        // notifications. Codes are stable per condition so we only
        // notify once per code per recording session (the user
        // doesn't want a beep every second while the meeting runs over).
        const warns = s.warnings || [];
        setWatchdogWarnings(warns);
        for (const w of warns) {
          if (notifiedCodesRef.current.has(w.code)) continue;
          notifiedCodesRef.current.add(w.code);
          fireNativeNotification(w.code, w.message);
        }
        // Sync local recording state with backend on EVERY tick.
        // This handles both:
        //   - The rising edge: an external trigger (auto-record from
        //     calendar, /recording/start fired from somewhere else)
        //     starts a recording without this view initiating it.
        //   - The falling edge: watchdog auto-stop, parent-PID
        //     deadman switch, user clicked Stop in sidebar.
        if (s.is_recording !== recording) {
          setRecording(s.is_recording);
          if (s.is_recording) {
            // Rising edge: auto-record (or other external start)
            // just fired. Pull the meeting name + scope hints the
            // backend set when it started this recording so the form
            // reflects them.
            if (s.auto_record_subject) {
              setMeetingName((current) =>
                current.trim() ? current : s.auto_record_subject!);
            }
            if (s.session_id) setActiveSessionId(s.session_id);
            // Reset notification dedupe + warning history for the
            // newly-detected recording.
            notifiedCodesRef.current = new Set();
            setWatchdogWarnings([]);
          } else {
            // Falling edge — clear the captured session id.
            setActiveSessionId(null);
          }
        }
        // Keep the captured session_id fresh in case we missed the
        // rising edge (e.g. view mounted after recording was already
        // in progress).
        if (s.is_recording && s.session_id && !activeSessionId) {
          setActiveSessionId(s.session_id);
        }
        if (!s.is_recording) {
          // If the watchdog auto-stopped us, the last poll's warning
          // is what we want to surface as an explanatory toast — the
          // user wasn't watching the screen, that's the whole point.
          const stopWarn = warns.find(
            (w) => w.code.endsWith("_stop") || w.code === "hard_cap_hit"
          );
          if (stopWarn && recording) {
            toast.warning("Recording auto-stopped", {
              description: stopWarn.message,
            });
            // Auto-stopped sessions still need the post-stop processing
            // path to kick off (auto_process / refresh sessions list).
            onSessionsChanged();
          }
        }
      } catch {}
    }, 1000);
    return () => clearInterval(t);
  }, [recording, activeSessionId, onSessionsChanged]);

  // Poll for model readiness
  useEffect(() => {
    if (!modelsLoading) return;
    const t = setInterval(async () => {
      try {
        const s = await api.recordingStatus();
        setModelsLoading(s.models_loading);
        if (!s.models_loading) clearInterval(t);
      } catch {}
    }, 3000);
    return () => clearInterval(t);
  }, [modelsLoading]);

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
      setRecording(true);
      // Suppress the shell's watchdog kill for the duration of the
      // recording — an unresponsive backend may still be capturing
      // audio, and this meeting can't be re-run.
      void setRecordingActive(true);
      setDuration(0);
      setScreenshotCount(0);
      setSession(null);
      setWatchdogWarnings([]);
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
      setRecording(false);
      void setRecordingActive(false);
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
      toast.error(`Stop failed: ${e instanceof Error ? e.message : e}`);
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

  // Exact-match block: the user flagged THIS specific subject via the
  // tile's "No auto" toggle. Removable per-meeting.
  const isExactBlocked = (subject: string) =>
    blockedSubjects.some(
      (s) => s.trim().toLowerCase() === (subject || "").trim().toLowerCase());

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
          <CardTitle className="flex items-center gap-2 text-base">
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
          <CardTitle className="flex items-center gap-2 text-base">
            <Mic className="h-4 w-4 text-primary" />
            Audio Devices
          </CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 md:grid-cols-2 gap-3">
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
              <Label htmlFor="conference-room-mode" className="cursor-pointer">
                Conference room mode
              </Label>
              <p className="text-xs text-muted-foreground">
                Mic captures everyone in the room. System-audio loopback
                is skipped — use this when nobody is on speakers and the
                laptop sits in the middle of the table.
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
          <CardTitle className="flex items-center gap-2 text-base">
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
                  "Auto-start recording at each calendar meeting's scheduled time. " +
                  "Filters: skip all-day events; require a Teams/Zoom/Meet link. " +
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
            <p className="text-sm text-muted-foreground text-center py-8">
              No upcoming meetings in the next 7 days, or Outlook not connected.
            </p>
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
                            <div>
                              <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-1">
                                Attendees ({det.data.attendees.length})
                              </div>
                              {det.data.attendees.length === 0 ? (
                                <div className="text-xs text-muted-foreground">None listed.</div>
                              ) : (
                                <div className="flex flex-wrap gap-1 max-h-28 overflow-auto">
                                  {det.data.attendees.map((a, ai) => (
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
            <CardTitle className="flex items-center gap-2 text-base">
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

function formatDur(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
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
 * work email (e.g. @ttecdigital.com). Counting that would make every
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
