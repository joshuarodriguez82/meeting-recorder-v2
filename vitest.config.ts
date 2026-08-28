/**
 * Frontend test harness.
 *
 * Added after v2.72.0 shipped a Settings snippet that used POSIX line
 * continuation — a trailing backslash, which cmd.exe treats as the
 * entire command. The first Windows user to paste it registered a
 * broken MCP server and could not overwrite it. The backend half of
 * that feature had 21 tests; the string the user actually pastes had
 * none, because there was nowhere to put one.
 *
 * jsdom rather than a browser: everything worth asserting here is
 * string- and DOM-shaped. A real browser belongs in an E2E rig, which
 * is a separate piece of work.
 *
 * No @vitejs/plugin-react: it pulls @rolldown/plugin-babel, which wants
 * @babel/core ^8 and conflicts with what Next 16 already resolves here.
 * The plugin exists for Fast Refresh in a dev server, which this does
 * not run — vitest's own esbuild transform compiles TSX from tsconfig's
 * `jsx` setting, which is all the tests need. Forcing the peer instead
 * would have put a disputed resolution in the lockfile the release
 * build uses.
 */
import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    // Mirrors tsconfig's `@/*` -> `src/*`, so tests import modules by
    // the same specifier the app does and a path change breaks both
    // together rather than only at runtime.
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // The Rust and Python suites are the slow ones; keep this instant
    // so there is no reason to skip it locally.
    globals: true,
  },
});
