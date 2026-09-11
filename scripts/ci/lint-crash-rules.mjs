#!/usr/bin/env node
/**
 * Fail CI on the lint rules that mean "this crashes", and only those.
 *
 * WHY NOT JUST RUN ESLINT
 * -----------------------
 * pr-checks.yml deliberately has no eslint step, and the reasoning is
 * right: the repo carries ~39 pre-existing errors from a newer
 * eslint-config-next ruleset, in files unrelated to any given change.
 * A gate that is red on every PR for debt it did not introduce is a
 * gate everyone learns to ignore — the same broken-window failure the
 * security baselines exist to avoid.
 *
 * So this gates on a SHORT LIST of rules whose violations are runtime
 * crashes rather than style, and ignores everything else. The existing
 * debt stays invisible here; a new crash does not.
 *
 * WHAT PUT IT HERE (field repro 2026-09-11)
 * -----------------------------------------
 * The Speakers tab read a `useState` value one line above the
 * `useState` call that produced it. Every render threw "Cannot access
 * 'gone' before initialization", and the window showed WebView2's own
 * "This page couldn't load" — the whole app, from one hoisted const.
 *
 * `next build` is the frontend gate and it passed: `tsc` does not flag
 * a reference inside a closure, because a closure COULD run after the
 * declaration, and nothing tells it that `.filter()` runs this one
 * immediately. ESLint checks where the reference sits rather than
 * reasoning about when the closure fires, which is the distinction
 * that catches it.
 *
 * ADDING A RULE HERE
 * ------------------
 * The bar is: a violation is a crash or a silently wrong result at
 * runtime, not a preference. Anything softer belongs in the ambient
 * config where it can be fixed gradually.
 *
 * THE BASELINE
 * ------------
 * Turning these rules on surfaced five sites that predate them. Each
 * was read and none is a live crash — the reference sits inside a
 * function that runs after the declaration is initialised, or the
 * "hook" is an ordinary handler whose name begins with `use`. They are
 * listed below so the gate blocks anything NEW from its first day,
 * rather than being switched off until the debt is cleared. Same
 * bargain as the security baselines: pre-existing debt stays visible
 * and does not fail a PR that did not cause it.
 *
 * Entries are file + rule, deliberately WITHOUT the line number, so
 * adding an import above a known site does not resurrect it as new —
 * and so that a genuinely new violation in the same file still fails,
 * because the count is compared too.
 */

import { spawnSync } from "node:child_process";

/** Rules whose violations crash at runtime. Keep this list short. */
const CRASH_RULES = new Set([
  // Reading a const/let before its declaration is a TypeError the
  // moment that line runs.
  "@typescript-eslint/no-use-before-define",
  "no-use-before-define",
  // Calling something that isn't callable, and unreachable code after
  // a return — both have shipped here as real defects.
  "no-obj-calls",
  "no-unreachable",
  // A hook called conditionally desynchronises React's hook order and
  // throws on the next render.
  "react-hooks/rules-of-hooks",
]);

/**
 * Known pre-existing sites: "<repo-relative path>|<ruleId>" -> count.
 * Verified by hand on 2026-09-11; none is a live crash. Lower the
 * count when you fix one; delete the entry when it reaches zero.
 */
const BASELINE = new Map([
  // `PAGE_HREF` and `page` are read inside function bodies that run
  // well after module initialisation.
  ["chrome-extension/tests/background.test.js|@typescript-eslint/no-use-before-define", 2],
  // `SEEN_KEY` likewise — both reads are inside functions.
  ["src/lib/useUnprocessedSessions.ts|@typescript-eslint/no-use-before-define", 2],
  // `useMeeting` is an ordinary click handler, not a hook; the rule
  // matches on the `use` prefix. Worth renaming, but renaming it is
  // not a hotfix.
  ["src/components/record-view.tsx|react-hooks/rules-of-hooks", 1],
]);

const result = spawnSync(
  "npx",
  ["eslint", ".", "-f", "json"],
  { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
);

// eslint exits non-zero whenever ANY rule fires, which here is always
// (the pre-existing debt). The exit code is therefore not the signal —
// the parsed report is. A report we cannot parse IS fatal, though:
// "couldn't read it" must never render as "nothing there".
let report;
try {
  report = JSON.parse(result.stdout);
} catch {
  console.error("::error::eslint produced no parsable report — the lint " +
                "run did not complete");
  console.error(result.stderr?.slice(0, 4000) ?? "");
  process.exit(1);
}

const root = process.cwd();
const byKey = new Map();   // key -> [description, ...]
for (const file of report) {
  const rel = file.filePath.startsWith(root)
    ? file.filePath.slice(root.length + 1).split("\\").join("/")
    : file.filePath;
  for (const m of file.messages ?? []) {
    if (!m.ruleId || !CRASH_RULES.has(m.ruleId)) continue;
    const key = `${rel}|${m.ruleId}`;
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(`${rel}:${m.line}:${m.column}  ${m.message}`);
  }
}

const newOnes = [];
for (const [key, hits] of byKey) {
  const allowed = BASELINE.get(key) ?? 0;
  if (hits.length > allowed) newOnes.push(...hits.slice(allowed));
}

const stale = [];
for (const [key, allowed] of BASELINE) {
  const seen = (byKey.get(key) ?? []).length;
  if (seen < allowed) stale.push(`${key} (baseline ${allowed}, found ${seen})`);
}

const scanned = report.length;
if (newOnes.length === 0) {
  console.log(`crash-rule lint: ${scanned} file(s), no new violations.`);
  if (stale.length) {
    console.log("Baseline can be tightened — fewer violations than " +
                "recorded:");
    for (const s of stale) console.log(`  ${s}`);
  }
  process.exit(0);
}

console.error(`::error::${newOnes.length} NEW crash-class lint ` +
              `violation(s). These throw at runtime; tsc does not ` +
              `catch them.`);
for (const c of newOnes) console.error(`  ${c}`);
process.exit(1);
