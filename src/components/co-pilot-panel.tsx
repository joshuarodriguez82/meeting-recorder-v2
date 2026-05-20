"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Pause, Play, RefreshCw, Sparkles } from "lucide-react";
import { api, ApiError, type CoPilotTickResponse } from "@/lib/api";
import { Button } from "@/components/ui/button";

// Live Co-Pilot panel.
//
// While a recording is in progress AND the user has opted into
// "Live Co-Pilot" in Settings, this panel polls
// POST /recording/copilot/tick every ~45 s. The backend reads the last
// ~10 min of live-transcript segments and asks the configured LLM
// (default Anthropic Haiku) for three short bullet lists:
//
//   * clarifying_questions — what to ask now to fill gaps
//   * risks                — unspoken assumptions / flags
//   * follow_ups           — concrete next-step suggestions
//
// Each list is capped at three short bullets. The panel only mounts
// when `recording && enabled`, so the polling lifecycle is automatic.
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
  const [result, setResult] = useState<CoPilotTickResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [paused, setPaused] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  // Track in-flight ticks so a manual "Refresh now" doesn't overlap
  // with the timer-driven one. Ref (not state) so the latest value is
  // visible inside the timer callback without re-creating the interval.
  const inFlight = useRef(false);

  const tick = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    try {
      const r = await api.copilotTick();
      setResult(r);
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

  const sections: Array<{ title: string; key: keyof CoPilotTickResponse }> = [
    { title: "Clarifying questions", key: "clarifying_questions" },
    { title: "Risks & assumptions", key: "risks" },
    { title: "Suggested follow-ups", key: "follow_ups" },
  ];

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

      <div className="space-y-3">
        {sections.map(({ title, key }) => {
          const items = (result?.[key] as string[] | undefined) ?? [];
          return (
            <div key={key} className="space-y-1.5">
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {title}
              </p>
              {items.length === 0 ? (
                <p className="text-xs italic text-muted-foreground">
                  {result === null
                    ? "Waiting for the first tick…"
                    : "Nothing here right now."}
                </p>
              ) : (
                <ul className="space-y-1">
                  {items.map((s, i) => (
                    <li key={i} className="text-sm leading-snug flex gap-2">
                      <span className="text-muted-foreground select-none">
                        •
                      </span>
                      <span>{s}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>
          {paused
            ? "Paused"
            : `Refreshing every ${Math.round(POLL_INTERVAL_MS / 1000)}s`}
          {result?.segment_count
            ? ` · ${result.segment_count} recent segments`
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
