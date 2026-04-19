"use client";

import { useEffect, useRef } from "react";
import { api, type Meeting } from "@/lib/api";
import { toast } from "sonner";

interface Props {
  enabled: boolean;
  minutesBefore: number;
  onStart: (meeting: Meeting) => void;
}

/**
 * Background poll: every 30s checks today's meetings; when one is within
 * `minutesBefore` of starting, fires BOTH a native Windows Action Center
 * toast (via the browser Notification API — routes through WebView2 to
 * the real Windows toast system so it surfaces even when the app is
 * minimized/behind other windows) AND an in-app sonner toast as a
 * fallback inside the current window.
 */
async function fireNativeNotification(
  meeting: Meeting,
  minutesLeft: number,
): Promise<void> {
  try {
    // Use Tauri's native notification plugin — routes through the
    // Windows Action Center (toast provider). Works even when the app
    // is minimized or on another virtual desktop.
    const [{ sendNotification, isPermissionGranted, requestPermission }] =
      await Promise.all([
        import("@tauri-apps/plugin-notification"),
      ]);
    let granted = await isPermissionGranted();
    if (!granted) {
      const perm = await requestPermission();
      granted = perm === "granted";
    }
    if (!granted) return;
    await sendNotification({
      title: "Meeting Starting Soon",
      body: `${meeting.subject} — starts in ~${minutesLeft} min`,
    });
  } catch (e) {
    // Tauri plugin not available (e.g. dev in browser) — in-app toast
    // still fires as fallback.
    console.warn("Native notification failed:", e);
  }
}

async function bringAppForward(): Promise<void> {
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    const w = getCurrentWindow();
    await w.unminimize().catch(() => {});
    await w.show().catch(() => {});
    await w.setFocus().catch(() => {});
  } catch {
    try { window.focus(); } catch {}
  }
}

export function CalendarMonitor({ enabled, minutesBefore, onStart }: Props) {
  const notified = useRef<Set<string>>(new Set());
  const dismissed = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!enabled || minutesBefore <= 0) return;

    // Request notification permission once on mount via the Tauri
    // plugin. No-op if already granted.
    (async () => {
      try {
        const { isPermissionGranted, requestPermission } =
          await import("@tauri-apps/plugin-notification");
        const granted = await isPermissionGranted();
        if (!granted) await requestPermission();
      } catch {
        /* plugin unavailable in dev browser */
      }
    })();

    let firstRun = true;
    const check = async () => {
      try {
        // First poll after launch bypasses the backend's 5-min cache
        // (the pre-warm may have populated it before the user added a
        // meeting in Outlook). Subsequent polls use the cache.
        const meetings = await api.getUpcomingMeetings(24, firstRun);
        firstRun = false;
        const now = Date.now();
        for (const m of meetings) {
          const key = `${m.subject}|${m.start}`;
          if (notified.current.has(key) || dismissed.current.has(key)) continue;
          const start = new Date(m.start).getTime();
          const secondsUntil = (start - now) / 1000;
          if (secondsUntil >= 0 && secondsUntil <= minutesBefore * 60) {
            notified.current.add(key);
            const minsLeft = Math.max(1, Math.round(secondsUntil / 60));
            // Native Windows toast — appears in Action Center even if
            // the app window is minimized or behind other windows.
            fireNativeNotification(m, minsLeft);
            // In-app toast as a belt-and-suspenders fallback so the
            // user still sees it if the Action Center is silenced. The
            // Start Recording button also unminimizes + focuses the
            // window so the user doesn't have to Alt-Tab manually.
            toast("📅 Meeting Starting Soon", {
              description: `${m.subject} — starts in ~${minsLeft} min`,
              duration: 60000,
              action: {
                label: "Start Recording",
                onClick: () => { bringAppForward(); onStart(m); },
              },
              cancel: {
                label: "Dismiss",
                onClick: () => dismissed.current.add(key),
              },
            });
          }
        }
      } catch {
        // silently skip — calendar might be unreachable
      }
    };

    // First check ~10s after mount — backend pre-warm finishes in 1-2s
    // and we want the user to get a notification quickly for meetings
    // they just added in Outlook. Then poll every 30s so freshly-added
    // meetings get picked up within half a minute.
    const firstCheck = setTimeout(check, 10000);
    const id = setInterval(check, 30000);
    return () => { clearTimeout(firstCheck); clearInterval(id); };
  }, [enabled, minutesBefore, onStart]);

  return null;
}
