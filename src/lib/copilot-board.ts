/**
 * What the Co-Pilot panel shows, from GET /recording/copilot/state.
 *
 * The panel used to render every tick as its own card, newest first; a
 * long call became dozens of near-identical cards and nothing could be
 * marked asked or dismissed. The backend now keeps one board entry per
 * suggestion with the user's verdict on it (backend/core/copilot_board.py);
 * this module decides how that board reads:
 *
 *  - open items, grouped by kind, newest-raised first, then the ones
 *    the model keeps coming back to;
 *  - handled items (done / saved / dismissed) out of the way, below.
 *
 * Pure, so it is testable without a DOM (see vitest.config.ts).
 */

import type {
  CoPilotBoardItem, CoPilotErrorCode, CoPilotKind, CoPilotState,
} from "./api";

export const KIND_ORDER: CoPilotKind[] = [
  "clarifying_questions", "risks", "follow_ups",
];

export const KIND_TITLE: Record<CoPilotKind, string> = {
  clarifying_questions: "Questions to ask",
  risks: "Risks & assumptions",
  follow_ups: "Follow-ups",
};

export interface BoardView {
  open: { kind: CoPilotKind; title: string; items: CoPilotBoardItem[] }[];
  handled: CoPilotBoardItem[];
  openCount: number;
}

function newestFirst(a: CoPilotBoardItem, b: CoPilotBoardItem): number {
  if (a.fresh !== b.fresh) return a.fresh ? -1 : 1;
  if (a.last_seen !== b.last_seen) return a.last_seen < b.last_seen ? 1 : -1;
  return b.times_suggested - a.times_suggested;
}

export function boardView(items: CoPilotBoardItem[]): BoardView {
  const open = KIND_ORDER.map((kind) => ({
    kind,
    title: KIND_TITLE[kind],
    items: items.filter((i) => i.kind === kind && i.status === "open")
      .sort(newestFirst),
  })).filter((g) => g.items.length > 0);
  const handled = items.filter((i) => i.status !== "open").sort(newestFirst);
  return {
    open,
    handled,
    openCount: open.reduce((n, g) => n + g.items.length, 0),
  };
}

/** The status line under the board, in the user's terms. */
export function statusLine(state: CoPilotState | null): string {
  if (!state || !state.active) return "";
  if (state.paused) return "Paused";
  const parts: string[] = [];
  if (state.last_tick_at) {
    const t = new Date(state.last_tick_at);
    if (!Number.isNaN(t.getTime())) {
      parts.push(`Updated ${t.toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit" })}`);
    }
  } else {
    parts.push("Listening");
  }
  if (state.next_tick_in_s !== null && state.next_tick_in_s !== undefined) {
    parts.push(`next in ${state.next_tick_in_s}s`);
  }
  return parts.join(" · ");
}

/** Why the Co-Pilot went quiet, or null when it didn't. */
export function errorMessage(code: CoPilotErrorCode | null | undefined): string | null {
  switch (code) {
    case null:
    case undefined:
      return null;
    case "timeout":
      return "The Co-Pilot model is responding too slowly. If you're on a "
        + "local model like Ollama it may be overloaded — coaching resumes "
        + "when it catches up.";
    case "unreachable":
      return "The Co-Pilot can't reach its model. If you use Ollama, make "
        + "sure it's running; Settings → Diagnostics can check.";
    case "no_output":
      return "The Co-Pilot model returned nothing usable. If it's a "
        + "'thinking' model, try a different one in Settings.";
    default:
      return "The Co-Pilot model call failed — it will try again on the "
        + "next update.";
  }
}
