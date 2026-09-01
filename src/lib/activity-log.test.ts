import { describe, it, expect } from "vitest";
import {
  appendEvent, unreadCount, newestId, relativeTime, stepLabel,
  MAX_EVENTS, type ActivityEvent, type PipelinePayload,
  type PipelineStage,
} from "./activity-log";

const T0 = 1_756_700_000_000;

function log(...inputs: Array<Partial<ActivityEvent> & { text: string }>) {
  let events: ActivityEvent[] = [];
  inputs.forEach((i, n) => {
    events = appendEvent(events, {
      kind: i.kind ?? "info",
      text: i.text,
      detail: i.detail,
      at: i.at ?? T0 + n * 1000,
    });
  });
  return events;
}

describe("appendEvent", () => {
  it("puts the newest event first", () => {
    const events = log({ text: "first" }, { text: "second" });
    expect(events.map((e) => e.text)).toEqual(["second", "first"]);
  });

  it("assigns increasing ids", () => {
    const events = log({ text: "a" }, { text: "b" }, { text: "c" });
    expect(events.map((e) => e.id)).toEqual([3, 2, 1]);
  });

  it("collapses an immediate repeat instead of flooding the log", () => {
    // Status polling re-delivers the same message every second or two.
    // Without this the day's real events sit under a hundred copies of
    // one line.
    const events = log(
      { text: "Transcribing…" },
      { text: "Transcribing…" },
      { text: "Transcribing…" },
    );
    expect(events).toHaveLength(1);
    expect(events[0].repeats).toBe(3);
  });

  it("keeps the newest timestamp when it collapses a repeat", () => {
    const events = log(
      { text: "Transcribing…", at: T0 },
      { text: "Transcribing…", at: T0 + 60_000 },
    );
    expect(events[0].at).toBe(T0 + 60_000);
  });

  it("records the same event happening AGAIN later as news", () => {
    // Recurrence is not repetition: a second export really is a second
    // thing that happened, and hiding it would make the log lie by
    // omission.
    const events = log(
      { text: "Export complete" },
      { text: "Transcribing…" },
      { text: "Export complete" },
    );
    expect(events.map((e) => e.text)).toEqual([
      "Export complete", "Transcribing…", "Export complete",
    ]);
  });

  it("treats a differing detail as a different event", () => {
    const events = log(
      { text: "Export complete", detail: "Acme" },
      { text: "Export complete", detail: "Globex" },
    );
    expect(events).toHaveLength(2);
  });

  it("treats a differing kind as a different event", () => {
    let events = appendEvent([], { kind: "success", text: "Done", at: T0 });
    events = appendEvent(events, { kind: "error", text: "Done", at: T0 + 1 });
    expect(events).toHaveLength(2);
  });

  it("ignores a blank event", () => {
    // An empty row in the panel is bad; an empty row that replaces the
    // "nothing yet" empty state with something that looks like activity
    // is worse.
    let events = appendEvent([], { kind: "info", text: "   ", at: T0 });
    expect(events).toEqual([]);
    events = appendEvent(events, { kind: "info", text: "", at: T0 });
    expect(events).toEqual([]);
  });

  it("caps the log so a long-running window cannot leak", () => {
    let events: ActivityEvent[] = [];
    for (let i = 0; i < MAX_EVENTS + 25; i++) {
      events = appendEvent(events, {
        kind: "info", text: `event ${i}`, at: T0 + i,
      });
    }
    expect(events).toHaveLength(MAX_EVENTS);
    // The cap drops the OLDEST, never the newest.
    expect(events[0].text).toBe(`event ${MAX_EVENTS + 24}`);
  });

  it("does not mutate the array it was given", () => {
    const before = log({ text: "a" });
    const snapshot = [...before];
    appendEvent(before, { kind: "info", text: "b", at: T0 });
    expect(before).toEqual(snapshot);
  });

  it("trims whitespace off the text it stores", () => {
    const events = log({ text: "  Transcribing…  " });
    expect(events[0].text).toBe("Transcribing…");
  });

  it("drops an empty detail rather than storing a blank second line", () => {
    const events = log({ text: "Done", detail: "   " });
    expect(events[0].detail).toBeUndefined();
  });
});

describe("unreadCount", () => {
  it("counts everything when nothing has been acknowledged", () => {
    expect(unreadCount(log({ text: "a" }, { text: "b" }), 0)).toBe(2);
  });

  it("counts only what arrived after the acknowledged id", () => {
    const events = log({ text: "a" }, { text: "b" }, { text: "c" });
    expect(unreadCount(events, 1)).toBe(2);
  });

  it("is zero once the newest has been acknowledged", () => {
    const events = log({ text: "a" }, { text: "b" });
    expect(unreadCount(events, newestId(events))).toBe(0);
  });

  it("is zero for an empty log", () => {
    expect(unreadCount([], 0)).toBe(0);
    expect(newestId([])).toBe(0);
  });
});

describe("relativeTime", () => {
  it("reads as 'now' for something that just happened", () => {
    expect(relativeTime(T0, T0)).toBe("now");
    expect(relativeTime(T0, T0 + 20_000)).toBe("now");
  });

  it("never shows '0m' for something a minute old", () => {
    expect(relativeTime(T0, T0 + 50_000)).toBe("1m");
  });

  it("scales through minutes, hours and days", () => {
    expect(relativeTime(T0, T0 + 5 * 60_000)).toBe("5m");
    expect(relativeTime(T0, T0 + 2 * 3_600_000)).toBe("2h");
    expect(relativeTime(T0, T0 + 3 * 86_400_000)).toBe("3d");
  });

  it("does not go negative on a clock that stepped backwards", () => {
    expect(relativeTime(T0 + 60_000, T0)).toBe("now");
  });
});

describe("stepLabel", () => {
  const pipeline = (states: Array<PipelineStage["state"]>): PipelinePayload => ({
    stages: states.map((state, i) => ({
      key: `s${i}`, label: `Stage ${i}`, state,
    })),
    label: "", percent: 0, active: null, error: null, done: false,
  });

  it("names the running step and the total", () => {
    expect(stepLabel(pipeline(["done", "active", "pending"])))
      .toBe("Step 2 of 3");
  });

  it("is null when nothing is running", () => {
    expect(stepLabel(pipeline(["done", "done", "done"]))).toBeNull();
    expect(stepLabel(pipeline(["pending", "pending"]))).toBeNull();
  });

  it("is null for a missing or empty pipeline", () => {
    expect(stepLabel(null)).toBeNull();
    expect(stepLabel(undefined)).toBeNull();
    expect(stepLabel(pipeline([]))).toBeNull();
  });

  it("is null when a stage failed rather than claiming a step is running", () => {
    expect(stepLabel(pipeline(["done", "failed", "pending"]))).toBeNull();
  });
});
