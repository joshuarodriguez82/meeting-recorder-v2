import { describe, it, expect } from "vitest";
import {
  normalizeName, isMerge, isRenameable, pluralMeetings,
} from "./client-admin";

// These cases mirror backend/tests/test_client_admin.py::plan_rename.
// If the two ever disagree the button lies about what it is about to
// do, which is the whole failure this module exists to prevent.

describe("normalizeName", () => {
  it("trims and case-folds", () => {
    expect(normalizeName("  Acme  ")).toBe("acme");
  });

  it("survives empty input", () => {
    expect(normalizeName("")).toBe("");
  });
});

describe("isMerge", () => {
  const existing = ["Acme", "Globex", "Initech"];

  it("is a merge when the target already exists", () => {
    expect(isMerge("Acme Corp", "Globex", existing)).toBe(true);
  });

  it("matches the target case-insensitively, like the backend key", () => {
    expect(isMerge("Acme Corp", "globex", existing)).toBe(true);
    expect(isMerge("Acme Corp", "  GLOBEX ", existing)).toBe(true);
  });

  it("is NOT a merge when only the capitalisation changes", () => {
    // "acme" -> "Acme" is one client wearing better capitalisation.
    // Calling it a merge would report work that never happens.
    expect(isMerge("acme", "Acme", existing)).toBe(false);
  });

  it("is NOT a merge for a brand-new name", () => {
    expect(isMerge("Acme", "Umbrella", existing)).toBe(false);
  });

  it("is NOT a merge for an empty target", () => {
    expect(isMerge("Acme", "   ", existing)).toBe(false);
  });

  it("handles the misspelling case this feature was built for", () => {
    // A typo holding no folders, renamed onto the real record.
    const names = ["Northwind", "Nortwind"];
    expect(isMerge("Nortwind", "Northwind", names)).toBe(true);
  });
});

describe("isRenameable", () => {
  it("rejects an unchanged name", () => {
    expect(isRenameable("Acme", "Acme")).toBe(false);
  });

  it("rejects whitespace-only input", () => {
    expect(isRenameable("Acme", "   ")).toBe(false);
  });

  it("accepts a changed, non-empty name", () => {
    expect(isRenameable("Acme", "Globex")).toBe(true);
  });

  it("accepts a re-casing, which is a real rename", () => {
    expect(isRenameable("acme", "Acme")).toBe(true);
  });
});

describe("pluralMeetings", () => {
  it("singularises exactly one", () => {
    expect(pluralMeetings(1)).toBe("1 meeting");
  });

  it("pluralises zero and many", () => {
    expect(pluralMeetings(0)).toBe("0 meetings");
    expect(pluralMeetings(34)).toBe("34 meetings");
  });
});
