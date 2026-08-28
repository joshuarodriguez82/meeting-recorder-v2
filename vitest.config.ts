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
 * NO DOM ENVIRONMENT YET, and that is a deliberate hold rather than an
 * oversight. jsdom 30 needs Node 22 (its undici calls
 * `webidl.util.markAsUncloneable`); every workflow here — including
 * release.yml, which builds the shipping artifacts — pins Node 20.
 * Adding component tests therefore means either pinning an older jsdom
 * or bumping the Node that produces releases, and neither belongs in a
 * change that rides along with a release. Everything worth asserting
 * today is string-shaped, so this runs in plain Node.
 *
 * To add component tests later, decide the Node question first, then
 * reinstall jsdom + @testing-library and either flip `environment` or
 * put `// @vitest-environment jsdom` at the top of the files that need
 * a document.
 *
 * No @vitejs/plugin-react either: it pulls @rolldown/plugin-babel,
 * which wants @babel/core ^8 and conflicts with what Next 16 resolves
 * here. The plugin exists for dev-server Fast Refresh, which this never
 * runs — vitest's esbuild transform compiles TSX from tsconfig, which
 * is all the tests need. Forcing the peer would have put a disputed
 * resolution into the lockfile the release build uses.
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
    environment: "node",
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // The Rust and Python suites are the slow ones; keep this instant
    // so there is no reason to skip it locally.
    globals: true,
  },
});
