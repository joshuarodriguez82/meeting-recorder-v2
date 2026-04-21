"use client";

import { useEffect, useRef, useState } from "react";
import { api, type UnprocessedSession } from "./api";

const POLL_INTERVAL_MS = 60_000;

/**
 * Polls `/sessions/unprocessed` on an interval and fires a Windows toast
 * notification the first time a new unprocessed session appears.
 *
 * The "seen set" lives in localStorage so an SA who dismisses the toast
 * doesn't get renotified on every app relaunch for the same session. Only
 * *new* sessions since the last seen snapshot fire a notification.
 *
 * Backend polling is cheap — the endpoint reads session list from disk
 * which is already cached. 60 s cadence is plenty for a notification UX.
 */
export function useUnprocessedSessions(enabled: boolean) {
  const [items, setItems] = useState<UnprocessedSession[]>([]);
  const seenIdsRef = useRef<Set<string>>(loadSeen());
  // Track whether the very first tick has completed. We suppress
  // notifications on that first pass — if the user relaunches the app
  // with 3 existing unprocessed sessions, we don't want to bombard them.
  // They already know those exist; only *new* arrivals get toasts.
  const primedRef = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    const fetchOnce = async () => {
      try {
        const list = await api.listUnprocessed();
        if (cancelled) return;
        setItems(list);

        if (!primedRef.current) {
          // First poll — seed the seen set with whatever's already there,
          // don't notify. Subsequent polls will notify on anything not
          // in this set.
          list.forEach((s) => seenIdsRef.current.add(s.session_id));
          saveSeen(seenIdsRef.current);
          primedRef.current = true;
          return;
        }

        const freshOnes = list.filter(
          (s) => !seenIdsRef.current.has(s.session_id),
        );
        if (freshOnes.length > 0) {
          freshOnes.forEach((s) => seenIdsRef.current.add(s.session_id));
          saveSeen(seenIdsRef.current);
          await fireToast(freshOnes);
        }
      } catch {
        // Backend hiccup — skip this tick, keep the last-good list. The
        // sidebar badge would already be showing whatever count we had.
      }
    };

    fetchOnce();
    const id = setInterval(fetchOnce, POLL_INTERVAL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, [enabled]);

  return { items, count: items.length };
}

function loadSeen(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(SEEN_KEY);
    if (!raw) return new Set();
    return new Set(JSON.parse(raw) as string[]);
  } catch {
    return new Set();
  }
}

function saveSeen(s: Set<string>) {
  if (typeof window === "undefined") return;
  try {
    // Cap at 200 entries so this doesn't grow unbounded — the set is
    // only there to suppress duplicate notifications, not to be an
    // archive. After 200 distinct sessions, oldest entries get evicted.
    const arr = Array.from(s);
    const capped = arr.slice(Math.max(0, arr.length - 200));
    window.localStorage.setItem(SEEN_KEY, JSON.stringify(capped));
  } catch {
    /* quota exceeded — ignore */
  }
}

const SEEN_KEY = "mr:unprocessed_seen_v1";

async function fireToast(freshOnes: UnprocessedSession[]) {
  // Lazy import so the hook works in any SSR / non-Tauri context too.
  try {
    const { isPermissionGranted, requestPermission, sendNotification } =
      await import("@tauri-apps/plugin-notification");

    let granted = await isPermissionGranted();
    if (!granted) {
      const perm = await requestPermission();
      granted = perm === "granted";
    }
    if (!granted) return;

    const count = freshOnes.length;
    const title = count === 1
      ? "1 session awaiting processing"
      : `${count} sessions awaiting processing`;
    const first = freshOnes[0];
    const body = count === 1
      ? `"${first.display_name}" — open Meeting Recorder to process`
      : `"${first.display_name}" and ${count - 1} more. Open Meeting Recorder to process.`;
    sendNotification({ title, body });
  } catch {
    // Notification API unavailable (e.g. running `next dev` in a browser
    // tab). The sidebar badge is still visible, so the user knows.
  }
}
