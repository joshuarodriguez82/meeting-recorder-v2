/**
 * A device re-scan must not silently switch the selected microphone.
 *
 * Re-scanning (so a Bluetooth headset connected after launch appears —
 * field report 2026-09-23) renumbers devices; the app selects by index.
 */

import { describe, expect, it } from "vitest";

import { reselectByName } from "./audio-devices";

const before = [
  { index: 0, name: "MacBook Pro Microphone" },
  { index: 1, name: "External USB Mic" },
];
// The headset connected, and everything shifted by one.
const after = [
  { index: 0, name: "Wireless Headphones" },
  { index: 1, name: "MacBook Pro Microphone" },
  { index: 2, name: "External USB Mic" },
];

describe("reselectByName", () => {
  it("follows the selected device to its new index", () => {
    expect(reselectByName(before, 1, after)).toBe(2);
  });

  it("does not jump to whatever took the old index", () => {
    expect(reselectByName(before, 0, after)).not.toBe(0);
    expect(reselectByName(before, 0, after)).toBe(1);
  });

  it("uses the saved choice when the current one is unknown", () => {
    expect(reselectByName([], null, after, "External USB Mic")).toBe(2);
  });

  it("falls back to the first device when the selected one is gone", () => {
    expect(reselectByName(after, 0, before)).toBe(0);
  });

  it("returns null when there is nothing to select", () => {
    expect(reselectByName(before, 0, [])).toBeNull();
  });
});
