/**
 * The Co-Pilot cost estimate has to be the real price.
 *
 * Haiku 4.5 was priced at Haiku 3's rate ($0.25 / $1.25 per million
 * tokens instead of $1 / $5), and the dated model ID Settings stores
 * ("claude-haiku-4-5-20251001") didn't match the table at all.
 */

import { describe, expect, it } from "vitest";

import { estimateCopilotCost } from "./copilot-cost";

const base = { provider: "anthropic", baseUrl: "" };

describe("estimateCopilotCost", () => {
  it("prices the dated Haiku 4.5 ID Settings stores", () => {
    const e = estimateCopilotCost({
      ...base, model: "claude-haiku-4-5-20251001",
      wideIntervalSec: 30, hotIntervalSec: 30 });
    expect(e.currentHourlyUsd).not.toBeNull();
  });

  it("uses Haiku 4.5's real price ($1 in / $5 out per million)", () => {
    const e = estimateCopilotCost({
      ...base, model: "claude-haiku-4-5",
      wideIntervalSec: 60, hotIntervalSec: 0 });
    // 60 wide ticks/h x (2600 in x $1/M + 200 out x $5/M) = $0.216
    expect(e.currentHourlyUsd).toBeCloseTo(0.216, 3);
  });

  it("a 30s main + 30s hot cadence is four calls a minute", () => {
    const e = estimateCopilotCost({
      ...base, model: "claude-haiku-4-5",
      wideIntervalSec: 30, hotIntervalSec: 30 });
    expect(e.callsPerMinute).toBe(4);
    // 120 x (2600 x $1/M + 200 x $5/M) + 120 x (1800 x $1/M + 100 x $5/M)
    expect(e.currentHourlyUsd).toBeCloseTo(0.708, 3);
  });
});
