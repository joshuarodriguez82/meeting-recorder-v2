/**
 * The meeting's Co-Pilot follow-ups, from the board saved on the session.
 *
 * After processing, each Co-Pilot question and follow-up is checked
 * against the transcript (backend/core/copilot_followups.py). This groups
 * them the same way the exported copilot_followups_<meeting>.md does —
 * still open first, because those are what's left to send.
 *
 * Pure, so it is testable without a DOM.
 */

import type { CoPilotBoardItem } from "./api";

const CHECKED = new Set(["clarifying_questions", "follow_ups"]);

export interface FollowUps {
  open: CoPilotBoardItem[];
  partly: CoPilotBoardItem[];
  answered: CoPilotBoardItem[];
  checked: boolean;
}

export function followUps(board: CoPilotBoardItem[] | undefined): FollowUps {
  const items = (board ?? []).filter(
    (i) => CHECKED.has(i.kind) && i.status !== "dismissed");
  return {
    open: items.filter((i) =>
      (i.resolution === "open" || !i.resolution) && i.status !== "done"),
    partly: items.filter((i) => i.resolution === "partly"),
    answered: items.filter((i) => i.resolution === "answered"),
    checked: items.some((i) => !!i.resolution),
  };
}

/** Open items as plain text, for pasting into a follow-up email. */
export function openAsText(f: FollowUps): string {
  return [...f.open, ...f.partly].map((i) => `- ${i.text}`).join("\n");
}
