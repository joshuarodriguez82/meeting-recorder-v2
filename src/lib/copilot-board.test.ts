/**
 * The Co-Pilot board reads as one list, not a pile of ticks.
 *
 * The state below is not hand-written: it was captured from the real
 * GET /recording/copilot/state handler (backend/tests/test_copilot_v2.py
 * `_state_payload`), and test_the_panel_fixture_is_what_the_endpoint_sends
 * holds it to that handler.
 */

import { describe, expect, it } from "vitest";

import type { CoPilotBoardItem, CoPilotState } from "./api";
import { boardView, errorMessage, statusLine } from "./copilot-board";
import STATE from "./__fixtures__/copilot-state.json";

const state = STATE as unknown as CoPilotState;

describe("boardView on the captured state", () => {
  const view = boardView(state.board);

  it("lists only open items as things to act on", () => {
    const open = view.open.flatMap((g) => g.items);
    expect(open.every((i) => i.status === "open")).toBe(true);
    expect(view.openCount).toBe(open.length);
  });

  it("keeps the user's verdicts out of the way, not deleted", () => {
    expect(view.handled.map((i) => i.status).sort())
      .toEqual(["dismissed", "done", "saved"]);
  });

  it("groups by kind in a fixed order", () => {
    const order = view.open.map((g) => g.kind);
    expect([...order].sort((a, b) =>
      ["clarifying_questions", "risks", "follow_ups"].indexOf(a)
      - ["clarifying_questions", "risks", "follow_ups"].indexOf(b)))
      .toEqual(order);
  });
});

describe("ordering", () => {
  const item = (over: Partial<CoPilotBoardItem>): CoPilotBoardItem => ({
    id: Math.random().toString(36).slice(2), kind: "risks", text: "x",
    status: "open", first_seen: "2026-10-01T10:00:00",
    last_seen: "2026-10-01T10:00:00", times_suggested: 1, fresh: false,
    ...over,
  });

  it("puts what the latest update raised first", () => {
    const old = item({ text: "old", last_seen: "2026-10-01T10:05:00" });
    const fresh = item({ text: "fresh", fresh: true });
    expect(boardView([old, fresh]).open[0].items[0].text).toBe("fresh");
  });

  it("then the most recently raised", () => {
    const a = item({ text: "a", last_seen: "2026-10-01T10:01:00" });
    const b = item({ text: "b", last_seen: "2026-10-01T10:09:00" });
    expect(boardView([a, b]).open[0].items.map((i) => i.text))
      .toEqual(["b", "a"]);
  });

  it("an empty board shows nothing open", () => {
    expect(boardView([]).openCount).toBe(0);
  });
});

describe("status and errors", () => {
  it("says paused when paused", () => {
    expect(statusLine({ ...state, paused: true })).toBe("Paused");
  });

  it("says nothing when the Co-Pilot is not running", () => {
    expect(statusLine({ ...state, active: false })).toBe("");
    expect(statusLine(null)).toBe("");
  });

  it("shows when the next update is due", () => {
    expect(statusLine({ ...state, next_tick_in_s: 12 })).toContain("next in 12s");
  });

  it("explains every error code the backend sends", () => {
    for (const code of ["timeout", "unreachable", "error", "no_output"] as const) {
      expect(errorMessage(code)).toBeTruthy();
    }
    expect(errorMessage(null)).toBeNull();
  });
});
