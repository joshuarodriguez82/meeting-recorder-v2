/**
 * The live view must apply corrections, not append them.
 *
 * Field report (2026-09): the live transcript showed one sentence as two
 * people at once ("You" and "Speaker N", same words), and one far-end
 * person split across two Speaker labels. The backend now sends a
 * `retract` for a duplicate already on screen and a `relabel` when two
 * labels turn out to be one person. Before this helper, both live panels
 * appended every stream message as a segment — a correction would have
 * rendered as a blank line.
 *
 * The stream below is not hand-written: it was captured from the real
 * LiveTranscriber and SSE serializer (backend/tests/test_live_dedup.py),
 * and test_the_frontend_fixture_is_what_the_stream_sends holds it to
 * that producer.
 */

import { describe, expect, it } from "vitest";

import {
  applyLiveMessage,
  type LiveMessage,
  type LiveSegment,
} from "./live-transcript";
import STREAM from "./__fixtures__/live-transcript-stream.json";

const stream = STREAM as LiveMessage[];

function replay(messages: LiveMessage[]): LiveSegment[] {
  return messages.reduce<LiveSegment[]>(applyLiveMessage, []);
}

describe("replaying the captured stream", () => {
  const shown = replay(stream);

  it("shows the far-end sentence once, under the far end", () => {
    const copies = shown.filter((s) => s.text.includes("ship the release"));
    expect(copies).toHaveLength(1);
    expect(copies[0].speaker).toBe("them");
  });

  it("removes the leaked copy that was already on screen", () => {
    const retract = stream.find(
      (m) => (m as { type?: string }).type === "retract",
    ) as { ids: number[] };
    for (const id of retract.ids) {
      expect(shown.some((s) => s.id === id)).toBe(false);
    }
  });

  it("puts one far-end voice under one label", () => {
    const labels = new Set(
      shown.filter((s) => s.speaker === "them").map((s) => s.speaker_label),
    );
    expect([...labels]).toEqual(["Speaker 1"]);
  });

  it("never renders a correction as a line", () => {
    expect(shown.every((s) => typeof s.text === "string")).toBe(true);
  });

  it("keeps the user's own lines", () => {
    expect(shown.filter((s) => s.speaker === "you")).toHaveLength(3);
  });
});

describe("applyLiveMessage", () => {
  const a: LiveSegment = { id: 1, start: 0, end: 1, text: "a", speaker: "you" };
  const b: LiveSegment = {
    id: 2, start: 1, end: 2, text: "b", speaker: "them",
    speaker_label: "Speaker 2",
  };

  it("ignores a segment it already has (hydrate overlapping the stream)", () => {
    const prev = [a];
    expect(applyLiveMessage(prev, { ...a })).toBe(prev);
  });

  it("keeps two segments that differ only by id", () => {
    const twin = { ...a, id: 9 };
    expect(applyLiveMessage([a], twin)).toHaveLength(2);
  });

  it("still dedupes id-less segments from an older backend", () => {
    const old = { start: 0, end: 1, text: "a" };
    expect(applyLiveMessage([old], { ...old })).toHaveLength(1);
  });

  it("returns the same list when a correction changes nothing", () => {
    const prev = [a, b];
    expect(applyLiveMessage(prev, { type: "retract", ids: [99] })).toBe(prev);
    expect(
      applyLiveMessage(prev, { type: "relabel", from: "Speaker 7", to: "X" }),
    ).toBe(prev);
  });

  it("relabels only the named label", () => {
    const next = applyLiveMessage([a, b], {
      type: "relabel", from: "Speaker 2", to: "Speaker 1",
    });
    expect(next.map((s) => s.speaker_label)).toEqual([undefined, "Speaker 1"]);
  });
});
