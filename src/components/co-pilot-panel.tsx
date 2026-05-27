"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Loader2, Pause, Play, RefreshCw, Sparkles, Copy, Check,
  Save, CheckSquare, Lightbulb, StickyNote,
} from "lucide-react";
import { toast } from "sonner";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api, ApiError, type CoPilotTickResponse } from "@/lib/api";
import { Button } from "@/components/ui/button";

// Live Co-Pilot panel.
//
// While a recording is in progress AND the user has opted into
// "Live Co-Pilot" in Settings (or via the in-bar toggle on the Record
// view), this panel polls POST /recording/copilot/tick every ~45 s.
// The backend reads the last ~10 min of live-transcript segments and
// asks the configured LLM (default Anthropic Haiku) for three short
// bullet lists:
//
//   * clarifying_questions — what to ask now to fill gaps
//   * risks                — unspoken assumptions / flags
//   * follow_ups           — concrete next-step suggestions
//
// Each tick's bullets are also appended to the active session on the
// backend so the coaching record survives past the recording — every
// tick is rendered here in a scrolling history (newest at the top)
// instead of replacing the previous one, matching how the live
// transcript accumulates segments rather than overwriting.
//
// On mount we fetch GET /recording/copilot/history so a mid-recording
// page reload doesn't blank the panel.
//
// Errors are silenced on the screen: a 403 means the user toggled the
// feature off while recording (we just stop polling), a 409 means the
// recording ended (same), and transient network failures just leave
// the previous result on screen until the next tick.

// Fallback intervals — used only until settings load. Real values
// come from settings.live_copilot_wide_interval_sec and
// live_copilot_hot_interval_sec; the user can dial them in
// Settings → Live Co-Pilot.
const DEFAULT_WIDE_INTERVAL_SEC = 45;
const DEFAULT_HOT_INTERVAL_SEC = 0; // hot tier off by default

interface Props {
  recording: boolean;
  enabled: boolean;
}

export function CoPilotPanel({ recording, enabled }: Props) {
  // Newest-first list of every tick we've shown. The backend also
  // persists each one onto the active session, so this list is purely
  // a render cache; the canonical history lives in session.copilot_ticks
  // once the recording stops.
  const [ticks, setTicks] = useState<CoPilotTickResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [paused, setPaused] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  // Surfaced when a manual Refresh-now click returns an empty tick.
  // Cleared on the next non-empty tick or the next manual click.
  const [refreshNote, setRefreshNote] = useState<string | null>(null);
  // Active persona + meeting-type. Hydrated from /settings on mount so
  // the dropdowns reflect what the backend will actually use for the
  // next tick. Changing a dropdown POSTs /settings/copilot-active so
  // the next 45s tick picks up the new framing — no app restart needed.
  const [activeMode, setActiveMode] = useState<string>("SA");
  const [activeType, setActiveType] = useState<string>("General");
  const [modes, setModes] = useState<string[]>([]);
  const [meetingTypes, setMeetingTypes] = useState<string[]>([]);
  // Polling intervals (seconds). Hydrated from settings on mount; the
  // user can change them in Settings and they take effect at the next
  // panel re-mount (next recording, basically — interval changes
  // mid-recording don't currently rebuild the poll timers).
  const [wideSec, setWideSec] = useState<number>(DEFAULT_WIDE_INTERVAL_SEC);
  const [hotSec, setHotSec] = useState<number>(DEFAULT_HOT_INTERVAL_SEC);

  // Load mode + meeting-type names and the current settings selection
  // once the panel mounts in a recording. Best-effort — if either fails
  // we fall through to the SA/General defaults already in state and the
  // dropdowns just show those single entries until the next reload.
  useEffect(() => {
    if (!recording || !enabled) return;
    let cancelled = false;
    (async () => {
      try {
        const [m, t, s] = await Promise.all([
          api.getCopilotModes(),
          api.getCopilotMeetingTypes(),
          api.getSettings(),
        ]);
        if (cancelled) return;
        setModes(m.map((x) => x.name));
        setMeetingTypes(t.map((x) => x.name));
        if (s.live_copilot_mode) setActiveMode(s.live_copilot_mode);
        if (s.live_copilot_meeting_type) setActiveType(s.live_copilot_meeting_type);
        if (typeof s.live_copilot_wide_interval_sec === "number"
            && s.live_copilot_wide_interval_sec > 0) {
          setWideSec(s.live_copilot_wide_interval_sec);
        }
        if (typeof s.live_copilot_hot_interval_sec === "number") {
          setHotSec(s.live_copilot_hot_interval_sec);
        }
      } catch {
        // Library load failed — leave defaults; dropdowns will be empty
        // and just show the SA/General current choice.
      }
    })();
    return () => { cancelled = true; };
  }, [recording, enabled]);

  const changeMode = async (next: string | null) => {
    if (!next || next === activeMode) return;
    const prev = activeMode;
    setActiveMode(next);
    try {
      await api.setCopilotActive(next, undefined);
      toast.success(`Co-pilot mode: ${next}`);
    } catch (e) {
      setActiveMode(prev);
      toast.error(`Couldn't set mode: ${e instanceof Error ? e.message : e}`);
    }
  };

  const changeType = async (next: string | null) => {
    if (!next || next === activeType) return;
    const prev = activeType;
    setActiveType(next);
    try {
      await api.setCopilotActive(undefined, next);
      toast.success(`Meeting type: ${next}`);
    } catch (e) {
      setActiveType(prev);
      toast.error(`Couldn't set type: ${e instanceof Error ? e.message : e}`);
    }
  };
  // Track in-flight ticks so a manual "Refresh now" doesn't overlap
  // with the timer-driven one. Ref (not state) so the latest value is
  // visible inside the timer callback without re-creating the interval.
  const inFlight = useRef(false);
  // Track whether we've already hydrated history on mount so a fast
  // re-render (e.g. parent state churn) doesn't refetch.
  const hydrated = useRef(false);

  // On mount (and any time the recording toggles back on), pull the
  // persisted tick history so a page reload mid-call rehydrates the
  // panel instead of starting from scratch. Best-effort; failures just
  // mean the user waits ~45 s for the first live tick.
  useEffect(() => {
    if (!recording || !enabled || hydrated.current) return;
    hydrated.current = true;
    (async () => {
      try {
        const r = await api.copilotHistory();
        if (r.ticks && r.ticks.length) {
          // Newest first matches the live render order.
          setTicks([...r.ticks].reverse());
        }
      } catch {
        // No persisted history (no recording yet, fresh session) — fine.
      }
    })();
  }, [recording, enabled]);

  const tick = useCallback(async (manual = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    if (manual) setRefreshNote(null);
    try {
      const r = await api.copilotTick();
      // Drop empty ticks so the history doesn't fill up with no-ops
      // before anyone has spoken. The backend already filters these
      // before persisting, but the panel polls faster than meaningful
      // speech sometimes happens, so this is a second guard.
      const isEmpty =
        r.clarifying_questions.length === 0 &&
        r.risks.length === 0 &&
        r.follow_ups.length === 0;
      if (!isEmpty) {
        setTicks((prev) => [r, ...prev]);
        setRefreshNote(null);
      } else if (manual) {
        // User clicked Refresh and got nothing — say so, otherwise it
        // looks like the button is broken.
        setRefreshNote("No new coaching content since the last tick.");
      }
      setLastError(null);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 403 || e.status === 409)) {
        // Feature disabled or no active recording — stop trying, the
        // parent will unmount us shortly anyway.
        setLastError(null);
      } else if (e instanceof ApiError && e.status === 429) {
        setLastError("Rate-limited by the LLM — retrying on the next tick.");
      } else {
        setLastError(e instanceof Error ? e.message : "Tick failed");
      }
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, []);

  // Wide poll — full window, slower cadence, runs always while
  // recording + enabled + not paused. First tick fires on the next
  // macrotask so users see something quickly.
  useEffect(() => {
    if (!recording || !enabled || paused) return;
    const ms = Math.max(15, wideSec) * 1000;
    const kick = setTimeout(() => void tick(), 0);
    const id = setInterval(tick, ms);
    return () => {
      clearTimeout(kick);
      clearInterval(id);
    };
  }, [recording, enabled, paused, tick, wideSec]);

  // Hot poll — narrow window, faster cadence, runs only when the user
  // has opted in (hotSec > 0). Calls the same setTicks pipeline as the
  // wide tick (responses share the CoPilotTickResponse shape) so the
  // dedupe + render logic doesn't have to branch. Most hot ticks
  // return empty arrays and are no-ops by design.
  useEffect(() => {
    if (!recording || !enabled || paused) return;
    if (!hotSec || hotSec <= 0) return;
    const ms = Math.max(5, hotSec) * 1000;
    const hotTick = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const r = await api.copilotHotTick();
        const isEmpty =
          r.clarifying_questions.length === 0 &&
          r.risks.length === 0 &&
          r.follow_ups.length === 0;
        if (!isEmpty) setTicks((prev) => [r, ...prev]);
      } catch {
        // Hot-tick failures are silent — wide tick will report any
        // real issue (auth / rate-limit / etc). The 90s window also
        // sees a lot of "really nothing here", so noise from this
        // loop would mostly be uninformative.
      } finally {
        inFlight.current = false;
      }
    };
    const id = setInterval(hotTick, ms);
    return () => clearInterval(id);
  }, [recording, enabled, paused, hotSec]);

  if (!recording || !enabled) return null;

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium flex-wrap">
        <Sparkles className="h-4 w-4 text-primary" />
        Live Co-Pilot
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
          beta
        </span>
        {loading && (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        )}

        {/* Persona + meeting-type pickers. Both updates persist to
            config.env and take effect on the NEXT tick — current
            in-flight tick keeps its prompt. Names render compact in
            the header; full prompt editing lives in Settings. */}
        <Select value={activeMode} onValueChange={changeMode}>
          <SelectTrigger className="h-7 w-32 text-xs" title="Co-pilot persona">
            <SelectValue placeholder="Mode" />
          </SelectTrigger>
          <SelectContent>
            {(modes.length ? modes : [activeMode]).map((m) => (
              <SelectItem key={m} value={m}>{m}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={activeType} onValueChange={changeType}>
          <SelectTrigger className="h-7 w-40 text-xs" title="Meeting type">
            <SelectValue placeholder="Meeting type" />
          </SelectTrigger>
          <SelectContent>
            {(meetingTypes.length ? meetingTypes : [activeType]).map((t) => (
              <SelectItem key={t} value={t}>{t}</SelectItem>
            ))}
          </SelectContent>
        </Select>

        <span className="text-[10px] text-muted-foreground">
          {ticks.length > 0
            ? `${ticks.length} update${ticks.length === 1 ? "" : "s"}`
            : ""}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void tick(true)}
            disabled={loading || paused}
            title="Refresh now"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPaused((p) => !p)}
            title={paused ? "Resume" : "Pause"}
          >
            {paused ? (
              <Play className="h-3.5 w-3.5" />
            ) : (
              <Pause className="h-3.5 w-3.5" />
            )}
          </Button>
        </div>
      </div>

      {/* Scrollable history. Capped at a comfortable single-screen
          height so the panel doesn't push the live transcript or other
          recording-page sections off the page on long calls. */}
      <div className="max-h-[28rem] overflow-y-auto pr-1 space-y-3">
        {ticks.length === 0 ? (
          <p className="text-xs italic text-muted-foreground py-2">
            Waiting for the first tick…
          </p>
        ) : (
          ticks.map((t, i) => <TickCard key={t.generated_at + i} tick={t} />)
        )}
      </div>

      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>
          {paused
            ? "Paused"
            : (hotSec > 0
                ? `Refreshing every ${wideSec}s (wide) + ${hotSec}s (hot)`
                : `Refreshing every ${wideSec}s`)}
          {ticks[0]?.segment_count
            ? ` · ${ticks[0].segment_count} recent segments`
            : ""}
        </span>
        {lastError ? (
          <span className="text-amber-600 dark:text-amber-400">
            {lastError}
          </span>
        ) : refreshNote ? (
          <span className="text-muted-foreground italic">{refreshNote}</span>
        ) : null}
      </div>
    </div>
  );
}

// A single tick rendered as three labeled bullet lists with the
// timestamp it was generated. Empty categories are hidden (rather than
// showing "Nothing here right now") to keep the history dense.
type SaveKind = "follow_up" | "decision" | "note";

function TickCard({ tick }: { tick: CoPilotTickResponse }) {
  // Each section knows the most natural Save target for its bullets —
  // a clarifying question is something the SA should ASK (follow-up),
  // a risk is something to TRACK as a decision-to-make, a suggested
  // follow-up is also a follow-up. The dropdown still offers all three
  // so the user can override per-bullet.
  const sections: Array<{
    title: string;
    key: keyof CoPilotTickResponse;
    defaultKind: SaveKind;
  }> = [
    { title: "Clarifying questions", key: "clarifying_questions", defaultKind: "follow_up" },
    { title: "Risks & assumptions",  key: "risks",                defaultKind: "decision"  },
    { title: "Suggested follow-ups", key: "follow_ups",           defaultKind: "follow_up" },
  ];
  const generated = tick.generated_at
    ? new Date(tick.generated_at).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "";

  // Build a plain-text copy of the whole tick formatted for paste-into-
  // notes use. Empty sections are skipped so the clipboard isn't padded
  // with blank headers.
  const copyText = sections
    .map(({ title, key }) => {
      const items = (tick[key] as string[] | undefined) ?? [];
      if (items.length === 0) return "";
      return `${title}:\n${items.map((s) => `  • ${s}`).join("\n")}`;
    })
    .filter(Boolean)
    .join("\n\n");

  return (
    <div className="rounded-md border bg-muted/30 p-3 space-y-2">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wide text-muted-foreground">
        <span>{generated}</span>
        <div className="flex items-center gap-2">
          {tick.segment_count > 0 && <span>{tick.segment_count} segments</span>}
          <TickCopyButton text={copyText} />
        </div>
      </div>
      {sections.map(({ title, key, defaultKind }) => {
        const items = (tick[key] as string[] | undefined) ?? [];
        if (items.length === 0) return null;
        return (
          <div key={key} className="space-y-1">
            <div className="flex items-center justify-between">
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {title}
              </p>
              <TickCopyButton
                text={`${title}:\n${items.map((s) => `  • ${s}`).join("\n")}`}
                ariaLabel={`Copy ${title}`}
              />
            </div>
            <ul className="space-y-1">
              {items.map((s, i) => (
                <li key={i} className="text-sm leading-snug flex gap-2 group items-start">
                  <span className="text-muted-foreground select-none mt-0.5">•</span>
                  <span className="flex-1">{s}</span>
                  <TickSaveButton text={s} defaultKind={defaultKind} />
                  <TickCopyButton
                    text={s}
                    ariaLabel="Copy bullet"
                    subtle
                  />
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

// Tiny inline Copy button used at three levels inside TickCard: whole
// tick (header), one section, one bullet. `subtle` hides until row hover
// so bullet-level buttons don't clutter the panel until the user goes
// looking for one.
// Per-bullet save action. Click the icon to save with the section's
// default kind; click the chevron-ish menu to override (follow-up vs
// decision vs note). All three append to the active session's
// corresponding field — show up in their respective tabs post-process.
// Hover-only so the panel doesn't get visually noisy.
function TickSaveButton({
  text, defaultKind,
}: { text: string; defaultKind: SaveKind }) {
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  const doSave = async (kind: SaveKind) => {
    if (!text || busy) return;
    setBusy(true);
    try {
      await api.saveCopilotSuggestion(kind, text);
      setSaved(true);
      const label = kind === "follow_up" ? "follow-up"
        : kind === "decision" ? "decision"
        : "note";
      toast.success(`Saved as ${label}`);
      setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="Save bullet"
        title="Save to follow-ups / decisions / notes"
        className="opacity-0 group-hover:opacity-100 inline-flex items-center justify-center h-5 w-5 rounded text-muted-foreground hover:bg-muted hover:text-foreground transition-opacity bg-transparent border-0 cursor-pointer"
      >
        {saved
          ? <Check className="h-3 w-3 text-primary" />
          : <Save className="h-3 w-3" />}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-44">
        <DropdownMenuItem onClick={() => void doSave("follow_up")}>
          <CheckSquare className="h-3.5 w-3.5 mr-2 text-primary" />
          As follow-up
          {defaultKind === "follow_up" && (
            <span className="ml-auto text-[9px] uppercase tracking-wide text-muted-foreground">
              default
            </span>
          )}
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => void doSave("decision")}>
          <Lightbulb className="h-3.5 w-3.5 mr-2 text-amber-500" />
          As decision
          {defaultKind === "decision" && (
            <span className="ml-auto text-[9px] uppercase tracking-wide text-muted-foreground">
              default
            </span>
          )}
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => void doSave("note")}>
          <StickyNote className="h-3.5 w-3.5 mr-2 text-muted-foreground" />
          To my notes
          {defaultKind === "note" && (
            <span className="ml-auto text-[9px] uppercase tracking-wide text-muted-foreground">
              default
            </span>
          )}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function TickCopyButton({
  text, ariaLabel = "Copy", subtle = false,
}: { text: string; ariaLabel?: string; subtle?: boolean }) {
  const [copied, setCopied] = useState(false);
  const onClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!text) return;
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
      toast.success("Copied");
      setTimeout(() => setCopied(false), 1200);
    } catch (err) {
      toast.error(`Copy failed: ${err instanceof Error ? err.message : err}`);
    }
  };
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      title={ariaLabel}
      className={
        "inline-flex items-center justify-center h-5 w-5 rounded text-muted-foreground "
        + "hover:bg-muted hover:text-foreground transition-opacity "
        + (subtle ? "opacity-0 group-hover:opacity-100" : "opacity-70 hover:opacity-100")
      }
    >
      {copied
        ? <Check className="h-3 w-3" />
        : <Copy className="h-3 w-3" />}
    </button>
  );
}
