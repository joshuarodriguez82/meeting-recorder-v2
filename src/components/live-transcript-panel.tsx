"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Mic } from "lucide-react";
import { api } from "@/lib/api";

// Live transcript panel.
//
// Subscribes to /recording/transcript/stream (Server-Sent Events) while
// the user is actively recording, appends each incoming segment to a
// scrolling list, and auto-scrolls to the bottom unless the user has
// scrolled up to read older text.
//
// We keep this view distinct from the canonical post-stop transcript:
// live segments arrive from non-overlapping 15s windows so boundary
// words sometimes get split between two segments. The "real" transcript
// produced by /process is what gets persisted on the session.
//
// Connection lifecycle:
//   - Open EventSource when recording starts AND the user hasn't
//     disabled the feature in Settings → Workflow.
//   - On 'done' event (sent when LiveTranscriber.stop() fires its
//     sentinel), close the connection.
//   - On manual unmount (user navigates away), close on cleanup.
//   - On error, retry up to 3 times with exponential backoff before
//     giving up. The backend's 5s heartbeat will catch most stalls.

const BACKEND = "http://127.0.0.1:17645";

type Segment = {
  start: number;
  end: number;
  text: string;
};

export function LiveTranscriptPanel({ recording }: { recording: boolean }) {
  const [segments, setSegments] = useState<Segment[]>([]);
  // Whether the SSE connection is currently open. Drives the "Listening
  // for speech…" placeholder vs the connecting spinner.
  const [connected, setConnected] = useState(false);
  // Whether the user has scrolled up to read older content. When true
  // we suppress auto-scroll on new segments so we don't rip them away
  // from what they're reading.
  const [autoStick, setAutoStick] = useState(true);
  // Whether live transcription is enabled in user settings. null = not
  // yet fetched; true/false = known. We don't render anything until we
  // know the answer so a flicker of "connecting…" doesn't appear for
  // users who've disabled the feature.
  const [liveEnabled, setLiveEnabled] = useState<boolean | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Re-fetch the setting every time recording starts, so toggling the
  // setting in another window between recordings is respected without
  // an app restart.
  useEffect(() => {
    if (!recording) return;
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getSettings();
        if (!cancelled) setLiveEnabled(s.live_transcription_enabled);
      } catch {
        // If settings fetch fails (e.g. backend bouncing) default to
        // the historical behavior of trying to subscribe — better than
        // silently denying the user the feature on a transient blip.
        if (!cancelled) setLiveEnabled(true);
      }
    })();
    return () => { cancelled = true; };
  }, [recording]);

  // Reset when a new recording starts so the previous session's text
  // isn't mixed in.
  useEffect(() => {
    if (recording) {
      setSegments([]);
      setAutoStick(true);
    }
  }, [recording]);

  // Open / close the EventSource based on `recording` AND `liveEnabled`.
  useEffect(() => {
    if (!recording || liveEnabled !== true) {
      setConnected(false);
      return;
    }
    let es: EventSource | null = null;
    let attempt = 0;
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      es = new EventSource(`${BACKEND}/recording/transcript/stream`);

      es.onopen = () => {
        attempt = 0;
        setConnected(true);
      };
      es.onmessage = (e) => {
        try {
          const seg: Segment = JSON.parse(e.data);
          setSegments((prev) => [...prev, seg]);
        } catch {
          // ignore malformed event
        }
      };
      es.addEventListener("done", () => {
        es?.close();
        setConnected(false);
      });
      es.onerror = () => {
        es?.close();
        setConnected(false);
        if (cancelled) return;
        // Backoff retry — covers transient backend respawns (the
        // watchdog can take 5s to bring it back up). Bail after 3
        // attempts and let the user manually restart by hitting Stop +
        // Record again.
        attempt += 1;
        if (attempt > 3) return;
        const delay = Math.min(8000, 500 * 2 ** attempt);
        setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      es?.close();
    };
  }, [recording, liveEnabled]);

  // Auto-scroll handling. We attach a scroll listener on the panel and
  // toggle autoStick based on whether the user is near the bottom.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const distFromBottom = el.scrollHeight - (el.scrollTop + el.clientHeight);
      // Within 40px of the bottom counts as "stuck" — accommodates
      // sub-pixel scroll rounding and touch-pad inertial overshoot.
      setAutoStick(distFromBottom < 40);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // When new segments arrive and the user is still pinned to the bottom,
  // scroll to the new bottom. If they've scrolled up to read, leave
  // their position alone.
  useEffect(() => {
    if (!autoStick) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [segments, autoStick]);

  // Don't render anything when:
  //   - the user isn't currently recording, OR
  //   - the user has disabled live transcription in Settings, OR
  //   - we're still fetching the setting (avoid a flash of "connecting…")
  if (!recording || liveEnabled !== true) return null;

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <Mic className="h-4 w-4 text-primary" />
        Live transcript
        {connected ? (
          <span className="text-[10px] uppercase tracking-wide text-primary">
            ● live
          </span>
        ) : (
          <span className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            connecting
          </span>
        )}
        {!autoStick && (
          <button
            onClick={() => {
              const el = scrollRef.current;
              if (el) el.scrollTop = el.scrollHeight;
              setAutoStick(true);
            }}
            className="ml-auto text-[11px] text-primary hover:underline"
          >
            Jump to latest ↓
          </button>
        )}
      </div>
      {/* Fixed height (not max-h) so the panel never grows or shrinks
          as segments accumulate — long calls keep the rest of the page
          stable. Always-visible scrollbar via the ::-webkit-scrollbar
          arbitrary-variant classes (default macOS hides scrollbars
          which made the panel feel like it was pushing things down
          when it was actually scrolling). 24rem ≈ 8 visible segments. */}
      <div
        ref={scrollRef}
        className={
          "h-96 overflow-y-scroll rounded-md bg-muted/40 p-3 text-sm leading-relaxed"
          + " [scrollbar-gutter:stable]"
          + " [&::-webkit-scrollbar]:w-2"
          + " [&::-webkit-scrollbar-track]:bg-transparent"
          + " [&::-webkit-scrollbar-thumb]:bg-muted-foreground/40"
          + " [&::-webkit-scrollbar-thumb]:rounded-full"
          + " [&::-webkit-scrollbar-thumb:hover]:bg-muted-foreground/60"
        }
      >
        {segments.length === 0 ? (
          <p className="text-muted-foreground text-xs italic">
            {connected
              ? "Listening for speech… first words appear ~15 seconds after you start talking."
              : "Connecting to the backend…"}
          </p>
        ) : (
          <div className="space-y-1.5">
            {segments.map((seg, i) => (
              <div key={i} className="flex gap-3">
                <span className="font-mono text-[10px] text-muted-foreground tabular-nums shrink-0 pt-1">
                  {formatTime(seg.start)}
                </span>
                <span className="break-words">{seg.text}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <p className="text-[10px] text-muted-foreground italic">
        Live preview. Speaker labels and the canonical transcript appear
        after you stop and process the recording.
      </p>
    </div>
  );
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
