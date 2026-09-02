/**
 * The activity centre's event store.
 *
 * WHY THIS EXISTS
 * ---------------
 * The sidebar carried a single-line status strip that showed whatever
 * string the backend last set, truncated to fit. It had three problems,
 * and only the first one was visible:
 *
 *   1. It garbled text. Two stage labels arrived welded together and
 *      the strip truncated the result mid-word ("Transcription
 *      completeIdentifying s…"). Fixed at source — see
 *      services/pipeline_progress.py.
 *
 *   2. It had no memory. Each message overwrote the last, so anything
 *      that happened while you were on another tab was gone. A user
 *      who looked away during processing had no way to learn whether
 *      it succeeded.
 *
 *   3. It could not be read. One truncated line has room for a fragment
 *      of a sentence and nothing else — no stage, no progress, no time.
 *
 * This module is the fix for (2): a bounded, ordered log of what
 * actually happened, which the panel renders and the strip summarises.
 *
 * DESIGN RULES
 * ------------
 * **Pure and synchronous.** No clock of its own — `now` is passed in —
 * so ordering, capping and dedupe are testable without waiting or
 * mocking timers. The frontend suite runs in a node environment with no
 * DOM, which is precisely why the logic worth testing lives here rather
 * than inside a component.
 *
 * **Deduped on repeat, not on recurrence.** Status polling re-delivers
 * the same message every second or two. Logging each one would bury the
 * day's real events under a hundred copies of one line. But the SAME
 * event happening again later is news — a second export, a second
 * failure — so dedupe only collapses an immediate repeat of the newest
 * entry, and records that it repeated rather than dropping it silently.
 *
 * **Bounded.** This lives in memory for the life of the window; an
 * unbounded log is a leak with a UI attached.
 */

/** Kinds carry the semantic colour and icon; the panel maps them. */
export type ActivityKind = "progress" | "success" | "error" | "info";

export interface ActivityEvent {
  /** Stable within a session; assigned on append. */
  id: number;
  kind: ActivityKind;
  /** One line, already human-readable. Never a raw stage token. */
  text: string;
  /** Optional second line — the detail a one-line strip had no room for. */
  detail?: string;
  /** Epoch ms, supplied by the caller. */
  at: number;
  /** How many times this entry arrived back-to-back. 1 for most. */
  repeats: number;
}

/** Entries kept. Roughly a long working day of real events. */
export const MAX_EVENTS = 50;

/**
 * How informative a kind is, for collapsing one message that changes
 * state. A step that looked fine and then failed has to end up red;
 * nothing may quietly downgrade it back.
 */
const KIND_RANK: Record<ActivityKind, number> = {
  info: 0,
  progress: 1,
  success: 2,
  error: 3,
};

function rank(kind: ActivityKind): number {
  return KIND_RANK[kind] ?? 0;
}

export interface AppendInput {
  kind: ActivityKind;
  text: string;
  detail?: string;
  at: number;
}

/**
 * Append an event, newest first.
 *
 * An input identical to the newest entry (same kind, text and detail)
 * bumps that entry's `repeats` and timestamp instead of adding a row —
 * status polling would otherwise flood the log with one message.
 * Returns a new array; the input is never mutated, so React state
 * updates stay honest about having changed.
 */
export function appendEvent(
  events: readonly ActivityEvent[],
  input: AppendInput,
): ActivityEvent[] {
  const text = (input.text || "").trim();
  // A blank event is not an event. Recording one would put an empty row
  // in the panel and, worse, reset the "nothing has happened yet"
  // empty state into something that looks like activity.
  if (!text) return events as ActivityEvent[];

  const head = events[0];
  if (head && head.text === text
      && (head.detail || "") === (input.detail || "")) {
    // Matched on TEXT, not on text-and-kind. One message routinely
    // arrives twice as its run finishes — once while the pipeline is
    // busy, once when it reports done — and the old rule, which
    // required the kind to match too, put both in the list. The panel
    // showed "Processing complete." twice, one blue and one green
    // (screenshot 2026-09-02). That is one thing that happened.
    const upgraded = rank(input.kind) > rank(head.kind) ? input.kind : head.kind;
    const bumped: ActivityEvent = {
      ...head,
      kind: upgraded,
      at: input.at,
      // A state change is not a recurrence. Showing "×2" beside a
      // message that happened once would be a small, confident lie.
      repeats: input.kind === head.kind ? head.repeats + 1 : head.repeats,
    };
    return [bumped, ...events.slice(1)];
  }

  const next: ActivityEvent = {
    id: (head?.id ?? 0) + 1,
    kind: input.kind,
    text,
    detail: input.detail?.trim() || undefined,
    at: input.at,
    repeats: 1,
  };
  return [next, ...events].slice(0, MAX_EVENTS);
}

/**
 * Events the user has not seen, given the id they last acknowledged.
 *
 * Counts by id rather than by a per-event "read" flag so that opening
 * the panel is one write, not fifty — and so a burst arriving while the
 * panel is open cannot be marked read before it was rendered.
 */
export function unreadCount(
  events: readonly ActivityEvent[],
  lastSeenId: number,
): number {
  return events.filter((e) => e.id > lastSeenId).length;
}

/** The newest id, for acknowledging. 0 when the log is empty. */
export function newestId(events: readonly ActivityEvent[]): number {
  return events[0]?.id ?? 0;
}

/**
 * Compact relative time: "now", "4m", "2h", "3d".
 *
 * Short by design — it sits in a narrow column beside the text, and the
 * exact timestamp is available on hover. `now` is a parameter for the
 * same reason the rest of this module takes one.
 */
export function relativeTime(at: number, now: number): string {
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 45) return "now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${Math.max(1, minutes)}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** A stage as the backend reports it. */
export interface PipelineStage {
  key: string;
  label: string;
  state: "pending" | "active" | "done" | "failed";
}

export interface PipelinePayload {
  stages: PipelineStage[];
  label: string;
  percent: number;
  active: string | null;
  error: string | null;
  done: boolean;
}

/**
 * "Step 2 of 3" for the running stage, or null when nothing is running.
 *
 * The single most requested thing a progress display can say and the
 * one a lone sentence could never answer.
 */
export function stepLabel(pipeline: PipelinePayload | null | undefined): string | null {
  const stages = pipeline?.stages;
  if (!stages?.length) return null;
  const index = stages.findIndex((s) => s.state === "active");
  if (index < 0) return null;
  return `Step ${index + 1} of ${stages.length}`;
}
