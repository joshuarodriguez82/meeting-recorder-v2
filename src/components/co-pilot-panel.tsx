"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Pause, Play, RefreshCw, Sparkles } from "lucide-react";
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

const POLL_INTERVAL_MS = 45_000;

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

  const tick = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
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
      }
      setLastError(null);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 403 || e.status === 409)) {
        // Feature disabled or no active recording — stop trying, the
        // parent will unmount us shortly anyway.
        setLastError(null);
      } else {
        setLastError(e instanceof Error ? e.message : "Tick failed");
      }
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, []);

  // Poll while mounted + not paused. First tick fires immediately so
  // users see something within the first 45 s, not at the 45 s mark.
  useEffect(() => {
    if (!recording || !enabled || paused) return;
    // Fire the first tick on the next macrotask so the synchronous
    // setLoading(true) inside tick() doesn't run inside the effect
    // body — that triggers react-hooks/set-state-in-effect. The user
    // perceived delay is sub-frame.
    const kick = setTimeout(() => void tick(), 0);
    const id = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      clearTimeout(kick);
      clearInterval(id);
    };
  }, [recording, enabled, paused, tick]);

  if (!recording || !enabled) return null;

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <Sparkles className="h-4 w-4 text-primary" />
        Live Co-Pilot
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
          beta
        </span>
        {loading && (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
        )}
        <span className="ml-2 text-[10px] text-muted-foreground">
          {ticks.length > 0
            ? `${ticks.length} update${ticks.length === 1 ? "" : "s"}`
            : ""}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void tick()}
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
            : `Refreshing every ${Math.round(POLL_INTERVAL_MS / 1000)}s`}
          {ticks[0]?.segment_count
            ? ` · ${ticks[0].segment_count} recent segments`
            : ""}
        </span>
        {lastError && (
          <span className="text-amber-600 dark:text-amber-400">
            {lastError}
          </span>
        )}
      </div>
    </div>
  );
}

// A single tick rendered as three labeled bullet lists with the
// timestamp it was generated. Empty categories are hidden (rather than
// showing "Nothing here right now") to keep the history dense.
function TickCard({ tick }: { tick: CoPilotTickResponse }) {
  const sections: Array<{ title: string; key: keyof CoPilotTickResponse }> = [
    { title: "Clarifying questions", key: "clarifying_questions" },
    { title: "Risks & assumptions", key: "risks" },
    { title: "Suggested follow-ups", key: "follow_ups" },
  ];
  const generated = tick.generated_at
    ? new Date(tick.generated_at).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "";

  return (
    <div className="rounded-md border bg-muted/30 p-3 space-y-2">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wide text-muted-foreground">
        <span>{generated}</span>
        {tick.segment_count > 0 && <span>{tick.segment_count} segments</span>}
      </div>
      {sections.map(({ title, key }) => {
        const items = (tick[key] as string[] | undefined) ?? [];
        if (items.length === 0) return null;
        return (
          <div key={key} className="space-y-1">
            <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
              {title}
            </p>
            <ul className="space-y-1">
              {items.map((s, i) => (
                <li key={i} className="text-sm leading-snug flex gap-2">
                  <span className="text-muted-foreground select-none">•</span>
                  <span>{s}</span>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}
