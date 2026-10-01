/**
 * The meeting's Co-Pilot follow-ups group like the exported file:
 * still open first, then partly answered, then answered.
 */

import { describe, expect, it } from "vitest";

import type { CoPilotBoardItem } from "./api";
import { followUps, openAsText } from "./copilot-followups";

const item = (over: Partial<CoPilotBoardItem>): CoPilotBoardItem => ({
  id: Math.random().toString(36).slice(2), kind: "clarifying_questions",
  text: "q", status: "open", first_seen: "", last_seen: "",
  times_suggested: 1, fresh: false, ...over,
});

describe("followUps", () => {
  const board = [
    item({ text: "How many agents?", resolution: "answered", answer: "400" }),
    item({ text: "Need CTI on day one?", resolution: "open" }),
    item({ text: "Send routing workbook", kind: "follow_ups", resolution: "partly" }),
    item({ text: "Porting lead time", kind: "risks" }),
    item({ text: "Vendor lock-in", status: "dismissed", resolution: "open" }),
  ];
  const f = followUps(board);

  it("groups by what the call settled", () => {
    expect(f.open.map((i) => i.text)).toEqual(["Need CTI on day one?"]);
    expect(f.partly.map((i) => i.text)).toEqual(["Send routing workbook"]);
    expect(f.answered.map((i) => i.text)).toEqual(["How many agents?"]);
    expect(f.checked).toBe(true);
  });

  it("leaves out risks and anything dismissed", () => {
    const all = [...f.open, ...f.partly, ...f.answered].map((i) => i.text);
    expect(all).not.toContain("Porting lead time");
    expect(all).not.toContain("Vendor lock-in");
  });

  it("copies what's left to send", () => {
    expect(openAsText(f)).toBe("- Need CTI on day one?\n- Send routing workbook");
  });

  it("an unchecked board shows everything as open, unchecked", () => {
    const g = followUps([item({ text: "a" }), item({ text: "b" })]);
    expect(g.open).toHaveLength(2);
    expect(g.checked).toBe(false);
  });

  it("an item marked done during the call is not open", () => {
    expect(followUps([item({ status: "done" })]).open).toHaveLength(0);
  });
});
