import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { UNATTRIBUTED_LABEL, countPeople, isUnattributed } from "./speakers";

describe("speaker count", () => {
  it("leaves the unattributed lines out of the count", () => {
    expect(countPeople({ SPEAKER_00: {}, SPEAKER_03: {}, [UNATTRIBUTED_LABEL]: {} })).toBe(2);
    expect(countPeople({})).toBe(0);
    expect(countPeople(undefined)).toBe(0);
  });

  it("knows the unattributed label", () => {
    expect(isUnattributed("SPEAKER_UNKNOWN")).toBe(true);
    expect(isUnattributed("SPEAKER_01")).toBe(false);
  });

  it("uses the backend's label", () => {
    const py = readFileSync("backend/core/speaker_merge.py", "utf8");
    expect(py).toContain(`UNATTRIBUTED_LABEL = "${UNATTRIBUTED_LABEL}"`);
  });
});
