/**
 * The Co-Pilot cost estimate has to be the real price.
 *
 * Haiku 4.5 was priced at Haiku 3's rate ($0.25 / $1.25 per million
 * tokens instead of $1 / $5), and the dated model ID Settings stores
 * ("claude-haiku-4-5-20251001") didn't match the table at all.
 */

import { describe, expect, it } from "vitest";

import { estimateCopilotCost, hasKnownRate, rateKey } from "./copilot-cost";

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

  it("prices Haiku 5.5 at $0.10 in / $0.50 out per million", () => {
    const e = estimateCopilotCost({
      ...base, model: "claude-haiku-5-5",
      wideIntervalSec: 30, hotIntervalSec: 30 });
    // A tenth of Haiku 4.5's $0.708 at the same cadence.
    expect(e.currentHourlyUsd).toBeCloseTo(0.0708, 4);
  });

  it("prices Opus 4.7 at $5 / $25, not Opus 4.1's $15 / $75", () => {
    const e = estimateCopilotCost({
      ...base, model: "claude-opus-4-7",
      wideIntervalSec: 60, hotIntervalSec: 0 });
    // 60 x (2600 x $5/M + 200 x $25/M) = $1.08
    expect(e.currentHourlyUsd).toBeCloseTo(1.08, 3);
  });

  it("a model this build doesn't know is priced from what the user entered", () => {
    const model = "claude-haiku-6";
    expect(hasKnownRate("anthropic", model)).toBe(false);
    const unknown = estimateCopilotCost({
      ...base, model, wideIntervalSec: 60, hotIntervalSec: 0 });
    expect(unknown.currentHourlyUsd).toBeNull();
    const priced = estimateCopilotCost({
      ...base, model, wideIntervalSec: 60, hotIntervalSec: 0,
      customRates: { [rateKey("anthropic", model)]: { in: 0.1, out: 0.5 } } });
    // Entered per MILLION tokens: 60 x (2600 x $0.1/M + 200 x $0.5/M)
    expect(priced.currentHourlyUsd).toBeCloseTo(0.0216, 4);
  });

  it("an entered price never overrides a known one", () => {
    const e = estimateCopilotCost({
      ...base, model: "claude-haiku-5-5", wideIntervalSec: 60, hotIntervalSec: 0,
      customRates: { [rateKey("anthropic", "claude-haiku-5-5")]: { in: 99, out: 99 } } });
    expect(e.currentHourlyUsd).toBeCloseTo(0.0216, 4);
  });
});
