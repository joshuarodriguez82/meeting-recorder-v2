// Tiny cross-platform Python runner used by `npm run zip-bundle`.
// Tries `python3`, then `python` — first one that responds wins.
// Exits with the script's own exit code so npm sees failures.
//
// We need this because the Windows Python launcher exposes `python.exe`
// only, while macOS has `python3` (no plain `python` symlink unless the
// user installed Homebrew Python with a special flag). zip-bundle.py
// only uses the stdlib so any Python 3.x interpreter works.

const { spawnSync } = require("node:child_process");
const path = require("node:path");

const script = process.argv[2];
if (!script) {
    console.error("usage: node run-python.js <script.py> [args...]");
    process.exit(2);
}
const extraArgs = process.argv.slice(3);

const repoRoot = path.resolve(__dirname, "..");
const candidates = process.platform === "win32"
    ? ["py", "python", "python3"]
    : ["python3", "python"];

function tryRun(cmd) {
    // Probe the interpreter quickly with -V before handing it the real script,
    // so we don't fail on a missing binary mid-zip.
    const probe = spawnSync(cmd, ["-V"], { stdio: "ignore" });
    if (probe.error || probe.status !== 0) return null;
    const result = spawnSync(cmd, [script, ...extraArgs], {
        cwd: repoRoot,
        stdio: "inherit",
    });
    return result;
}

for (const cmd of candidates) {
    const r = tryRun(cmd);
    if (r === null) continue;
    process.exit(r.status ?? 1);
}

console.error(
    `Could not find a Python interpreter. Tried: ${candidates.join(", ")}.\n` +
    `Install Python (e.g. \`brew install python@3.13\` on macOS) and retry.`,
);
process.exit(1);
