/**
 * ONE source of truth for "is a recording in progress right now".
 *
 * Field report 2026-08-11 (stuck "Recording…" indicator): the user
 * pressed Stop while the backend was restarting. The stop request
 * failed. From then on the sidebar showed "Recording… 20:10" ticking
 * upward while the Record page simultaneously showed the idle
 * pre-recording form with a Start Recording button — two parts of the
 * same app disagreeing about whether a meeting was being captured.
 *
 * That was possible because THREE independent things each polled
 * `/recording/status` on their own cadence and each kept their own
 * copy of the answer:
 *
 *   - src/app/page.tsx (2s interval)   → the sidebar badge + elapsed timer
 *   - src/components/record-view.tsx   → its own `recording` boolean (1s)
 *   - record-view again, for models_loading (3s)
 *
 * Three pollers, three local states, three chances to drift apart — and
 * a fourth copy of the truth living in the Rust shell as
 * RECORDING_ACTIVE, pushed down ad hoc from whichever component
 * happened to notice a change. Any request that failed, any interval
 * that got torn down mid-flight, any state update that landed out of
 * order left one copy stale forever, because nothing ever went back
 * and reconciled them.
 *
 * This module replaces all of that with a single poller whose result
 * every consumer subscribes to. Consequences that matter:
 *
 *   1. The sidebar indicator and the Record view read the SAME object,
 *      so they cannot disagree. Not "are kept in sync" — they are
 *      literally the same value.
 *   2. Every successful poll reconciles the Rust-side RECORDING_ACTIVE
 *      flag against the server's answer (see `_reconcileShellFlag`).
 *      A failed Stop can no longer leave the shell's watchdog
 *      suppressed indefinitely, which was the quiet half of this bug:
 *      the visible badge is annoying, a permanently disarmed watchdog
 *      means a wedged backend never gets replaced.
 *   3. The server is the ONLY thing that can clear recording state.
 *      Local optimism ("I clicked Stop, so we must be stopped") is what
 *      allowed the two halves of the UI to disagree in the first place.
 *
 * Deliberately NOT done: clearing recording state when the backend is
 * unreachable. A recording genuinely in progress while the backend
 * briefly restarts must keep showing as recording — the capture threads
 * may well still be running, and the meeting can't be re-run. We hold
 * the last known state through an outage and only ever act on an
 * affirmative `is_recording: false` from a poll that actually
 * succeeded.
 */

import { useSyncExternalStore } from "react";
import { api, setRecordingActive, type RecordingStatus } from "./api";

export interface RecordingStatusSnapshot {
  /**
   * The most recent SUCCESSFUL poll result, or null before the first
   * one lands. Held across backend outages on purpose — see the
   * module docstring's "Deliberately NOT done".
   */
  status: RecordingStatus | null;
  /** Whether the most recent poll attempt succeeded. */
  reachable: boolean;
  /**
   * Consecutive failed polls. The "Reconnecting to backend…" banner
   * waits for 2 so a single dropped request during a normal respawn
   * doesn't flash a scary message at the user.
   */
  consecutiveFailures: number;
}

const POLL_INTERVAL_MS = 1000;

let snapshot: RecordingStatusSnapshot = {
  status: null,
  reachable: true,
  consecutiveFailures: 0,
};

// Stable object for the server/prerender pass. Next.js static-exports
// this app, so getSnapshot must not be called with a freshly allocated
// object during SSR or React throws an infinite-loop warning.
const SERVER_SNAPSHOT: RecordingStatusSnapshot = {
  status: null,
  reachable: true,
  consecutiveFailures: 0,
};

const subscribers = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let inFlight = false;

/**
 * Last value we pushed into the Tauri shell's RECORDING_ACTIVE flag,
 * or null if we've never pushed. Null (rather than false) on purpose:
 * the first poll of the app's life must push whatever the server says,
 * including `false`, so a flag left set by a previous UI state gets
 * cleared rather than assumed-correct.
 */
let lastPushedShellFlag: boolean | null = null;

function emit() {
  for (const cb of subscribers) cb();
}

/**
 * Push recording state down to the Tauri shell, but only when it
 * actually changes — this is a Tauri IPC call and the poll runs every
 * second.
 *
 * The shell's watchdog kills an unresponsive backend so it can be
 * replaced. That's correct when idle and catastrophic mid-recording,
 * so the UI suppresses it via this flag. The failure mode this
 * function exists to prevent: a stop that errors out leaves the flag
 * stuck `true`, the watchdog stays suppressed for the rest of the app's
 * life, and a genuinely wedged backend is never replaced. Reconciling
 * on every successful poll means the flag can only be wrong for as long
 * as the backend is unreachable.
 */
function reconcileShellFlag(isRecording: boolean) {
  if (lastPushedShellFlag === isRecording) return;
  lastPushedShellFlag = isRecording;
  void setRecordingActive(isRecording);
}

async function poll() {
  // Guard against overlapping requests if the backend is slow enough
  // that a poll outlives its interval.
  if (inFlight) return;
  inFlight = true;
  try {
    const status = await api.recordingStatus();
    // The server has spoken. This is the reconciliation point for
    // BOTH the visible indicator and the shell flag — including the
    // case where a Stop request failed and local state still thinks a
    // recording is running.
    reconcileShellFlag(!!status.is_recording);
    snapshot = { status, reachable: true, consecutiveFailures: 0 };
  } catch {
    // Backend unreachable — almost always a watchdog respawn. Keep the
    // last known status: a real recording must NOT be cleared just
    // because we briefly can't reach the process capturing it.
    snapshot = {
      status: snapshot.status,
      reachable: false,
      consecutiveFailures: snapshot.consecutiveFailures + 1,
    };
  } finally {
    inFlight = false;
  }
  emit();
}

function subscribe(cb: () => void): () => void {
  subscribers.add(cb);
  if (timer === null) {
    void poll();
    timer = setInterval(() => { void poll(); }, POLL_INTERVAL_MS);
  }
  return () => {
    subscribers.delete(cb);
    if (subscribers.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function getSnapshot(): RecordingStatusSnapshot {
  return snapshot;
}

function getServerSnapshot(): RecordingStatusSnapshot {
  return SERVER_SNAPSHOT;
}

/**
 * Subscribe to the shared recording status. Every consumer gets the
 * same object, so the sidebar indicator and the Record view cannot
 * disagree about whether a recording is running.
 */
export function useRecordingStatus(): RecordingStatusSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

/**
 * Force an immediate poll. Call after start/stop so the UI reflects the
 * change without waiting out the interval — the response is still the
 * server's answer, not an optimistic local guess.
 */
export function refreshRecordingStatus(): void {
  void poll();
}

/**
 * Suppress the shell watchdog immediately, ahead of the first poll that
 * will observe the new recording. Start is the one place local
 * optimism is safe: over-suppressing the watchdog for one second is
 * harmless, whereas letting it fire in the gap between "capture
 * started" and "our next poll noticed" could kill a live recording.
 *
 * Note there is no matching "clear it now" helper. Clearing is the
 * server's call, always — that asymmetry IS the fix for field report
 * 2026-08-11.
 */
export function suppressWatchdogForNewRecording(): void {
  lastPushedShellFlag = true;
  void setRecordingActive(true);
}
