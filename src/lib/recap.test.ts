import { describe, expect, it } from "vitest";
import { callClock, insertStamp, parseRecap } from "./recap";

describe("parseRecap", () => {
  it("reads headings and bullets", () => {
    const md = "## Discussed\n- Pricing for **Acme**\n* Rollout\n\n## Decided\n1. Go with option B\nPlain line";
    expect(parseRecap(md)).toEqual([
      { kind: "heading", text: "Discussed" },
      { kind: "bullet", text: "Pricing for Acme" },
      { kind: "bullet", text: "Rollout" },
      { kind: "heading", text: "Decided" },
      { kind: "bullet", text: "Go with option B" },
      { kind: "text", text: "Plain line" },
    ]);
  });

  it("is empty for nothing", () => {
    expect(parseRecap("")).toEqual([]);
  });
});

describe("callClock", () => {
  it("formats a position in the call", () => {
    expect(callClock(0)).toBe("0:00");
    expect(callClock(2535)).toBe("42:15");
    expect(callClock(3723)).toBe("1:02:03");
  });
});

describe("insertStamp", () => {
  it("puts the stamp at the start of a line at the caret", () => {
    expect(insertStamp("", 0, 75)).toEqual({ text: "[1:15] ", caret: 7 });
    const out = insertStamp("First point", 11, 75);
    expect(out.text).toBe("First point\n[1:15] ");
    expect(out.caret).toBe(out.text.length);
    expect(insertStamp("a\n", 2, 5).text).toBe("a\n[0:05] ");
  });
});
