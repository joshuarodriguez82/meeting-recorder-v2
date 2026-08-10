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
// live segments arrive from speech-boundary (VAD) chunks by default —
// or, when the backend's live_vad_enabled setting is off or VAD fails
// at runtime, from non-overlapping 15s fixed windows — so boundary
// words sometimes get split between two segments either way. The "real"
// transcript produced by /process is what gets persisted on the
// session.
//
// Connection lifecycle:
//   - Open EventSource when recording starts AND the user hasn't
//     disabled the feature in Settings → Workflow.
//   - On 'done' event (sent when LiveTranscriber.stop() fires its
//     sentinel), close the connection.
//   - On manual unmount (user navigates away), close on cleanup.
//   - On error, retry up to 3 times with exponential backoff before
//     giving up. The backend's 5s heartbeat will catch most stalls.

type Speaker = "you" | "them" | "room";

type Segment = {
  start: number;
  end: number;
  text: string;
  // "you"  = your mic (default mode)
  // "them" = system-audio loopback (everyone else on the call)
  // "room" = conference-room mode: mic captures multiple in-room
  //          people and nobody is on speakers, so labelling individual
  //          live segments as "you" would be misleading. Pyannote
  //          splits the canonical post-stop transcript into proper
  //          per-speaker labels.
  // Older backends (before dual-stream live transcription) didn't tag
  // segments — those render without a label.
  speaker?: Speaker;
  // Fine-grained live speaker split (field report 2026-08-10, Zoom
  // notetaker parity) — set ONLY on "them" segments, when the backend
  // has speechbrain/torch available AND has accumulated enough audio
  // to fingerprint the voice. "Speaker 2" for an unrecognized voice, or
  // a known SpeakerProfile's real display name (e.g. "Maria Chen") when
  // it matches. Never set on "you"/"room" segments — the speaker
  // field's existing meaning is unchanged, this is purely additive.
  // Absent = render the plain "Them" badge exactly as before.
  speaker_label?: string;
};

// Small fixed palette for distinct live speakers, cycled by index so a
// 6th+ concurrent speaker still gets a (repeated) color rather than an
// unstyled fallback. Chosen to sit alongside the existing "you" (primary)
// / "room" (amber) / "them" (muted) tokens without colliding with either.
const SPEAKER_COLORS = [
  "bg-sky-500/15 text-sky-700 dark:text-sky-400",
  "bg-violet-500/15 text-violet-700 dark:text-violet-400",
  "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  "bg-rose-500/15 text-rose-700 dark:text-rose-400",
  "bg-orange-500/15 text-orange-700 dark:text-orange-400",
  "bg-teal-500/15 text-teal-700 dark:text-teal-400",
];

// Stable color assignment per distinct speaker_label string, in
// first-seen order, so "Speaker 2" doesn't change color as more
// segments arrive. Module-level cache is fine — labels are meaningful
// only within one recording's live preview, and the panel unmounts
// (and its segments reset) between recordings.
const speakerColorCache = new Map<string, string>();
function colorForSpeakerLabel(label: string): string {
  let color = speakerColorCache.get(label);
  if (!color) {
    color = SPEAKER_COLORS[speakerColorCache.size % SPEAKER_COLORS.length];
    speakerColorCache.set(label, color);
  }
  return color;
}

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

  // Hydrate the panel from the backend's segment history whenever this
  // component (re-)mounts while a recording is active. This is what
  // makes the live transcript "persistent" across tab switches —
  // RecordView unmounts when the user navigates away, and on the way
  // back in we fast-rebuild the segment list from /recording/
  // transcript/history instead of starting from a blank slate and
  // catching only segments produced after the SSE re-subscribe.
  //
  // The endpoint 409s when nothing's recording, which is fine — we just
  // start empty and the SSE block below does nothing until a recording
  // starts. When a recording is in progress, the history is whatever
  // segments the LiveTranscriber's bounded deque (2000 entries) has at
  // request time — enough for an entire ~2-3h dense meeting.
  //
  // Why this isn't `if (recording)` only: the SSE could fire its first
  // segment before our history fetch resolves, leaving a brief window
  // where we'd duplicate. The append-with-dedupe in the SSE handler
  // below handles that.
  useEffect(() => {
    if (!recording) {
      // Fresh recording about to start (or just stopped) — drop the
      // previous session's segments so they don't bleed into the next,
      // and forget speaker->color assignments so "Speaker 1" in the
      // next meeting doesn't inherit a color from an unrelated person
      // in this one.
      setSegments([]);
      setAutoStick(true);
      speakerColorCache.clear();
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await api.transcriptHistory();
        if (cancelled) return;
        if (r.segments && r.segments.length) {
          setSegments(r.segments as Segment[]);
          setAutoStick(true);
        }
      } catch {
        // 409 = no active recording on the backend (race with stop).
        // Anything else (network, parse) — leave segments empty, the
        // SSE catch-up will fill in from now forward.
      }
    })();
    return () => { cancelled = true; };
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

    const connect = async () => {
      if (cancelled) return;
      // Backend port is OS-picked at app startup (lib.rs::pick_free_port)
      // and exposed via the get_backend_port Tauri command. We MUST
      // resolve it dynamically — the old hardcoded 17645 only worked
      // by accident on pre-dynamic-port builds.
      const baseUrl = await api.getBaseUrl();
      // EventSource can't set headers — token goes via query param.
      const authQ = await api.authQuery();
      if (cancelled) return;
      es = new EventSource(`${baseUrl}/recording/transcript/stream${authQ}`);

      es.onopen = () => {
        attempt = 0;
        setConnected(true);
      };
      es.onmessage = (e) => {
        try {
          const seg: Segment = JSON.parse(e.data);
          setSegments((prev) => {
            // Dedupe against the most recent ~50 entries. The history
            // hydrate that runs on mount may overlap with the first
            // few SSE events in flight at subscribe time; matching on
            // (start, end, text) is a tight enough key that a real
            // distinct segment with the same shape essentially can't
            // happen, and bounding the lookup keeps appends O(1).
            const tail = prev.slice(-50);
            const dup = tail.some(
              (s) =>
                s.start === seg.start &&
                s.end === seg.end &&
                s.text === seg.text,
            );
            return dup ? prev : [...prev, seg];
          });
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
              ? "Listening for speech… first words appear a few seconds after you start talking."
              : "Connecting to the backend…"}
          </p>
        ) : (
          <div className="space-y-1.5">
            {segments.map((seg, i) => (
              <div key={i} className="flex gap-3">
                <span className="font-mono text-[10px] text-muted-foreground tabular-nums shrink-0 pt-1">
                  {formatTime(seg.start)}
                </span>
                {seg.speaker ? (
                  <span
                    className={
                      "shrink-0 px-1.5 rounded text-[10px] font-medium uppercase tracking-wide self-start mt-1 "
                      + (seg.speaker === "you"
                        ? "bg-primary/15 text-primary"
                        : seg.speaker === "room"
                        ? "bg-amber-500/15 text-amber-700 dark:text-amber-400"
                        // "them" with a fine-grained speaker_label gets
                        // its own distinct, stable color instead of the
                        // generic muted "Them" styling — see
                        // colorForSpeakerLabel above.
                        : seg.speaker === "them" && seg.speaker_label
                        ? colorForSpeakerLabel(seg.speaker_label)
                        : "bg-muted-foreground/15 text-foreground/70")
                    }
                  >
                    {seg.speaker === "you" ? "You"
                      : seg.speaker === "room" ? "Room"
                      : seg.speaker === "them" && seg.speaker_label
                      ? seg.speaker_label
                      : "Them"}
                  </span>
                ) : null}
                <span className="break-words">{seg.text}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <p className="text-[10px] text-muted-foreground italic">
        Live preview. <span className="font-medium">You</span> = your mic,{" "}
        <span className="font-medium">Them</span> = system audio. Final
        speaker attribution and the canonical transcript appear after you
        stop and process the recording.
      </p>
    </div>
  );
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
