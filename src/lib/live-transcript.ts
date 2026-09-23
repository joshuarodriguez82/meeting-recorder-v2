/**
 * Applying one message from /recording/transcript/stream to the live
 * transcript on screen.
 *
 * Most messages are new segments. Two are corrections to what is
 * already shown (backend/core/live_transcriber.py):
 *
 *  - `retract` — segments to withdraw. The same speech was heard by both
 *    the microphone and system audio (speakers into the mic, or the
 *    user's voice routed into system audio) and transcribed twice, as
 *    two people saying the same words at once. The quieter copy is the
 *    leak (backend/core/live_dedup.py); when it was already on screen,
 *    it is withdrawn here.
 *  - `relabel` — every line under one speaker label belongs under
 *    another: the live speaker tracker found two labels to be one
 *    person (backend/core/live_speakers.py).
 *
 * Pure, so it is testable without a DOM (see vitest.config.ts).
 */

export type LiveSegment = {
  id?: number;
  start: number;
  end: number;
  text: string;
  speaker?: "you" | "them" | "room";
  speaker_label?: string;
};

export type LiveRetract = { type: "retract"; ids: number[] };
export type LiveRelabel = { type: "relabel"; from: string; to: string };
export type LiveMessage = LiveSegment | LiveRetract | LiveRelabel;

function isRetract(m: LiveMessage): m is LiveRetract {
  return (m as LiveRetract).type === "retract";
}

function isRelabel(m: LiveMessage): m is LiveRelabel {
  return (m as LiveRelabel).type === "relabel";
}

/** The segment list after one stream message. Returns `prev` itself
 *  when nothing changed, so React skips the re-render. */
export function applyLiveMessage<S extends LiveSegment>(
  prev: S[],
  msg: LiveMessage,
): S[] {
  if (isRetract(msg)) {
    const drop = new Set(msg.ids);
    const next = prev.filter((s) => s.id === undefined || !drop.has(s.id));
    return next.length === prev.length ? prev : next;
  }
  if (isRelabel(msg)) {
    if (!prev.some((s) => s.speaker_label === msg.from)) return prev;
    return prev.map((s) =>
      s.speaker_label === msg.from ? { ...s, speaker_label: msg.to } : s,
    );
  }
  const seg = msg as S;
  // The history hydrate on mount can overlap the first stream events;
  // an id (or, from an older backend, start/end/text) identifies a
  // repeat. Bounded to the recent tail so appends stay O(1).
  const tail = prev.slice(-50);
  const dup = tail.some((s) =>
    s.id !== undefined && seg.id !== undefined
      ? s.id === seg.id
      : s.start === seg.start && s.end === seg.end && s.text === seg.text,
  );
  return dup ? prev : [...prev, seg];
}
