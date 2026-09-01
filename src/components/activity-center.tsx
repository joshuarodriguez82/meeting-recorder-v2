"use client";

/**
 * The activity centre — what replaced the sidebar status strip.
 *
 * WHAT WAS THERE (screenshot, 2026-09-01)
 * ---------------------------------------
 * A one-line strip showing whatever string the backend last set, hard
 * `truncate`d to the sidebar width. The user's words for it: "words
 * merged together, cut off … looks like we put it together and forgot
 * all about it."
 *
 * They were right, and there were three separate faults behind it:
 *
 *   1. The text was garbled at source. Two stage labels arrived welded
 *      into one string and the strip truncated the result mid-word
 *      ("Transcription completeIdentifying s…"). Fixed in
 *      services/pipeline_progress.py — the backend now models stages
 *      and renders a string, rather than building the string by
 *      substitution and hoping.
 *
 *   2. It had no memory. Every message overwrote the last, so anything
 *      that happened while you were on another tab was simply gone.
 *      Now there is a bounded log (lib/activity-log.ts) and a panel.
 *
 *   3. It could not answer the questions people actually have. "Is it
 *      nearly done?" and "did it work?" are unanswerable by one
 *      truncated sentence. Now: named stage, step N of M, a real
 *      progress bar, and a per-stage state list.
 *
 * DESIGN
 * ------
 * The strip is the summary and the panel is the detail — the strip
 * never truncates a word again because it wraps to two lines and the
 * full text is always one click away.
 *
 * Colour carries state and is the only place this component is loud:
 * running is the app's primary, done is green, failed is destructive.
 * Every one of those is defined for light and dark, because the strip
 * sits on the sidebar's own surface in both.
 *
 * Collapsed sidebar keeps a progress ring and the unread dot rather
 * than dropping the signal entirely, which is what the old strip did.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Popover, PopoverContent, PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import {
  Loader2, CheckCircle2, AlertTriangle, Circle, Activity, Check,
} from "lucide-react";
import {
  appendEvent, unreadCount, newestId, relativeTime, stepLabel,
  type ActivityEvent, type ActivityKind, type PipelinePayload,
} from "@/lib/activity-log";

export interface ActivityCenterProps {
  /** Structured stages, when the backend supplies them. */
  pipeline?: PipelinePayload | null;
  /** The one-line fallback for a backend that predates `pipeline`. */
  statusText: string;
  /** True while the backend is genuinely working. */
  busy: boolean;
  collapsed: boolean;
}

const KIND_STYLES: Record<ActivityKind, { dot: string; text: string }> = {
  progress: { dot: "bg-primary", text: "text-foreground" },
  success: {
    dot: "bg-emerald-500",
    text: "text-emerald-700 dark:text-emerald-400",
  },
  error: { dot: "bg-destructive", text: "text-destructive" },
  info: { dot: "bg-muted-foreground/50", text: "text-muted-foreground" },
};

function StageRow({ stage }: { stage: PipelinePayload["stages"][number] }) {
  const icon =
    stage.state === "done" ? (
      <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
    ) : stage.state === "active" ? (
      <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
    ) : stage.state === "failed" ? (
      <AlertTriangle className="h-3.5 w-3.5 text-destructive" />
    ) : (
      <Circle className="h-3.5 w-3.5 text-muted-foreground/40" />
    );
  return (
    <div className="flex items-center gap-2 py-1 text-xs">
      <span className="shrink-0">{icon}</span>
      <span
        className={
          stage.state === "pending"
            ? "text-muted-foreground/60"
            : stage.state === "failed"
              ? "text-destructive"
              : "text-foreground"
        }
      >
        {stage.label}
      </span>
    </div>
  );
}

export function ActivityCenter({
  pipeline, statusText, busy, collapsed,
}: ActivityCenterProps) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [lastSeen, setLastSeen] = useState(0);
  const [open, setOpen] = useState(false);
  // Re-render on a slow tick so relative times stay honest without
  // making every status poll a full re-render of the list.
  const [now, setNow] = useState(() => Date.now());
  const lastKeyRef = useRef<string>("");

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const headline = pipeline?.label || statusText;
  const error = pipeline?.error || null;
  const percent = pipeline?.percent ?? 0;
  const step = stepLabel(pipeline);

  // Fold status changes into the log. Keyed so an unchanged poll does
  // no work at all — appendEvent would collapse the repeat anyway, but
  // setState on every tick would still re-render the whole sidebar.
  useEffect(() => {
    const kind: ActivityKind = error
      ? "error"
      : busy
        ? "progress"
        : pipeline?.done
          ? "success"
          : "info";
    const text = error || headline;
    const key = `${kind}|${text}`;
    if (!text || key === lastKeyRef.current) return;
    lastKeyRef.current = key;
    setEvents((prev) => appendEvent(prev, { kind, text, at: Date.now() }));
  }, [headline, error, busy, pipeline?.done]);

  const unread = useMemo(
    () => unreadCount(events, lastSeen), [events, lastSeen]);

  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) setLastSeen(newestId(events));
  };

  // Nothing has happened and nothing is happening: show nothing rather
  // than an empty chrome that implies something should be there.
  if (!headline && !events.length) return null;

  const state: "running" | "failed" | "done" =
    error ? "failed" : busy ? "running" : "done";

  const accent =
    state === "failed"
      ? "bg-destructive"
      : state === "running"
        ? "bg-primary"
        : "bg-emerald-500";

  const trigger = collapsed ? (
    <PopoverTrigger
      aria-label={headline || "Activity"}
      title={headline}
      className="relative flex w-full items-center justify-center border-b border-border py-2.5 transition-colors hover:bg-accent/50"
    >
      {state === "running" ? (
        <Loader2 className="h-4 w-4 animate-spin text-primary" />
      ) : state === "failed" ? (
        <AlertTriangle className="h-4 w-4 text-destructive" />
      ) : (
        <Activity className="h-4 w-4 text-muted-foreground" />
      )}
      {unread > 0 && (
        <span className={`absolute right-2 top-2 h-2 w-2 rounded-full ${accent}`} />
      )}
    </PopoverTrigger>
  ) : (
    <PopoverTrigger className="w-full border-b border-border px-4 py-2.5 text-left transition-colors hover:bg-accent/40">
      <div className="flex items-start gap-2">
        <span className="mt-0.5 shrink-0">
          {state === "running" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
          ) : state === "failed" ? (
            <AlertTriangle className="h-3.5 w-3.5 text-destructive" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
          )}
        </span>
        <div className="min-w-0 flex-1">
          {/* Wraps to two lines instead of truncating. The old strip
              cut a word in half rather than use the space below it. */}
          <p
            className={`text-xs leading-snug line-clamp-2 ${
              state === "failed" ? "text-destructive" : "text-foreground"
            }`}
          >
            {error || headline}
          </p>
          {(step || unread > 0) && (
            <p className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {step}
              {step && unread > 0 ? " · " : ""}
              {unread > 0 ? `${unread} new` : ""}
            </p>
          )}
        </div>
      </div>
      {state === "running" && (
        <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-muted">
          <div
            className={`h-full rounded-full ${accent} transition-[width] duration-500`}
            style={{ width: `${Math.max(4, percent)}%` }}
          />
        </div>
      )}
    </PopoverTrigger>
  );

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      {trigger}
      <PopoverContent align="start" side="right" className="w-80 p-0">
        <div className="border-b px-3 py-2">
          <p className="text-sm font-medium">Activity</p>
          <p className="text-xs text-muted-foreground">
            {state === "running"
              ? "Processing this meeting"
              : state === "failed"
                ? "Last run did not finish"
                : "Nothing running"}
          </p>
        </div>

        {pipeline?.stages?.length ? (
          <div className="border-b px-3 py-2">
            {pipeline.stages.map((stage) => (
              <StageRow key={stage.key} stage={stage} />
            ))}
            {error && (
              <p className="mt-1 text-xs text-destructive">{error}</p>
            )}
          </div>
        ) : null}

        <ScrollArea className="max-h-64">
          <div className="px-3 py-2">
            {events.length === 0 ? (
              <p className="py-4 text-center text-xs text-muted-foreground">
                Nothing yet today.
              </p>
            ) : (
              events.map((event) => (
                <div key={event.id} className="flex gap-2 py-1.5">
                  <span
                    className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${KIND_STYLES[event.kind].dot}`}
                  />
                  <div className="min-w-0 flex-1">
                    <p className={`text-xs leading-snug ${KIND_STYLES[event.kind].text}`}>
                      {event.text}
                      {event.repeats > 1 && (
                        <span className="ml-1 text-muted-foreground">
                          ×{event.repeats}
                        </span>
                      )}
                    </p>
                    {event.detail && (
                      <p className="text-[11px] text-muted-foreground">
                        {event.detail}
                      </p>
                    )}
                  </div>
                  <span
                    className="shrink-0 text-[10px] tabular-nums text-muted-foreground"
                    title={new Date(event.at).toLocaleString()}
                  >
                    {relativeTime(event.at, now)}
                  </span>
                </div>
              ))
            )}
          </div>
        </ScrollArea>

        {events.length > 0 && (
          <div className="border-t px-3 py-1.5">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-full text-xs"
              onClick={() => setEvents([])}
            >
              Clear
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
