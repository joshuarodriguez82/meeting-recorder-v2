import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { ACCENTS, DEFAULT_THEME, isDark, parseTheme } from "./theme";

describe("appearance preferences", () => {
  it("defaults to light teal, the look the app has always had", () => {
    expect(DEFAULT_THEME).toEqual({ mode: "light", accent: "teal" });
    expect(parseTheme(null)).toEqual(DEFAULT_THEME);
    expect(parseTheme("not json")).toEqual(DEFAULT_THEME);
  });

  it("keeps what it knows and drops what it doesn't", () => {
    expect(parseTheme('{"mode":"dark","accent":"ember"}'))
      .toEqual({ mode: "dark", accent: "ember" });
    // A value from a newer build must not break this one.
    expect(parseTheme('{"mode":"sepia","accent":"neon"}')).toEqual(DEFAULT_THEME);
    expect(parseTheme('{"mode":"system"}')).toEqual({ mode: "system", accent: "teal" });
  });

  it("resolves system against the OS", () => {
    expect(isDark("system", true)).toBe(true);
    expect(isDark("system", false)).toBe(false);
    expect(isDark("dark", false)).toBe(true);
    expect(isDark("light", true)).toBe(false);
  });
});

describe("the pre-paint script and the stylesheet agree with theme.ts", () => {
  const root = join(__dirname, "..", "..");
  const init = readFileSync(join(root, "public", "theme-init.js"), "utf8");
  const css = readFileSync(join(root, "src", "app", "globals.css"), "utf8");

  it("knows every colour theme", () => {
    for (const a of ACCENTS.filter((x) => x.id !== "teal")) {
      expect(init).toContain(`"${a.id}"`);
      expect(css).toContain(`:root[data-accent="${a.id}"]:not(.dark)`);
      expect(css).toContain(`.dark[data-accent="${a.id}"]`);
    }
    expect(init).toContain('"mr-theme"');
  });

  it("only goes dark for dark, or system on a dark OS", () => {
    expect(init).toMatch(/t\.mode === "dark"/);
    expect(init).toMatch(/t\.mode === "system"/);
  });
});
