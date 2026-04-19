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
 * Background poll: every 60s checks today's meetings; when one is within
 * `minutesBefore` of starting, fires a toast with Start/Dismiss actions.
 */
export function CalendarMonitor({ enabled, minutesBefore, onStart }: Props) {
  const notified = useRef<Set<string>>(new Set());
  const dismissed = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!enabled || minutesBefore <= 0) return;

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
            toast("📅 Meeting Starting Soon", {
              description: `${m.subject} — starts in ~${minsLeft} min`,
              duration: 60000,
              action: {
                label: "Start Recording",
                onClick: () => onStart(m),
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
