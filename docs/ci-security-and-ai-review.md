# CI: static security analysis + scheduled AI code review

Two workflows added alongside the existing four. Neither one duplicates
`dependency-audit.yml`, and neither one touches `release.yml`.

| Workflow | What it looks at | Can it fail a build? |
| --- | --- | --- |
| `dependency-audit.yml` (existing) | **Third-party** dependency CVEs | No — reports only, always exits 0 |
| `security-scan.yml` (new) | **First-party** source, statically | Yes — but only on findings absent from a committed baseline |
| `ai-code-review.yml` (new) | A bounded recent diff, reviewed by Claude | No — advisory, skips cleanly when unconfigured |

---

## 1. `security-scan.yml` — static analysis of our own code

Before this, nothing analysed the code we wrote; only the code we depend on.

Triggers: every pull request, every push to `main`, Mondays 07:00 UTC, and
`workflow_dispatch`.

### Scanners and their real baselines

Measured on the tree at **v2.32.0** (`db2d830`):

| Job | Tool | Scope | Baselined findings |
| --- | --- | --- | --- |
| `python-bandit` | bandit 1.9.4 | `backend/ scripts/ tools/ setup.py zip-bundle.py make_shortcut.py` (~39,200 LOC) | **159** — 3 HIGH, 10 MEDIUM, 146 LOW |
| `javascript-semgrep` | semgrep 1.173.0 (`p/security-audit`, `p/secrets`, `p/owasp-top-ten`, 365 rules) | `backend src chrome-extension mobile scripts tools src-tauri` (196 files) | **6** — all WARNING |
| `rust-clippy` | `cargo clippy` (stable) | `src-tauri` (2,591 LOC, first-party only) | **5** warnings |

Of bandit's 159, **156 are pre-existing application code** and 3 are in
`scripts/ci/ai_code_review.py` itself — `B404`/`B603`/`B607` for its `git`
subprocess call, which passes an argument list (never a shell string) and
resolves `git` from `PATH` the way every CI script does. Those three were
caught by this very gate on its first run against the new script, which is the
mechanism working as designed.

The 156 pre-existing findings break down as:

```
57  B110  try_except_pass                      13  B112  try_except_continue
32  B106  hardcoded_password_funcarg (tests)   16  B603  subprocess_without_shell_equals_true
14  B404  import subprocess                     5  B105  hardcoded_password_string
 4  B108  hardcoded_tmp_directory (tests)       3  B310  urllib audit          [MEDIUM]
 3  B324  weak SHA1 hash                [HIGH]  3  B607  partial executable path
 2  B102  exec_used (tests)             [MEDIUM] 2  B403  import pickle
 1  B606  start_process_with_no_shell           1  B608  hardcoded_sql_expression [MEDIUM]
```

The three HIGH findings are `hashlib.sha1()` in `document_service.py:114`,
`item_status_service.py:69`, and `prep_brief_cache_service.py:48` — all cache
keys, not signatures, so they are almost certainly fine as-is and want
`usedforsecurity=False`. Semgrep independently flags the same three.

### `B101` is the one skipped rule, and why

Bandit reports **1,824** findings on the application tree with everything on.
**1,678 of them (92%) are `B101 assert_used`**, almost entirely pytest
assertions in `backend/tests/` — which is what a pytest assertion *is*. Only 3
`B101` hits are in non-test code. Leaving it on would bury the 146 findings
that carry signal under a 1,678-line wall of noise, and no amount of
baselining makes that report readable again.

The risk `B101` guards against — asserts vanishing under `python -O` — does not
apply: `src-tauri/src/lib.rs` launches the sidecar as a plain `python
server.py`, never with `-O`, and pytest rewrites test asserts itself.

Nothing else is skipped. `B404`/`B603`/`B607` (subprocess), `B105`/`B106`
(secrets), `B108` (temp paths), `B403` (pickle) and `B324` (weak hashes) all
stay on — those are the rules that matter for a backend that spawns ffmpeg,
walks user-supplied paths, and holds provider API keys. Config lives in
[`scripts/ci/bandit.yaml`](../scripts/ci/bandit.yaml).

> **Editing `exclude_dirs` in that file:** bandit matches those entries as
> **substrings of the full path**, not as path components. An entry of `out`
> silently swallowed `outlook_web_scraper.py`, `_calendar_outlook.py` and
> `_follow_up_email_outlook.py` — 139 findings instead of 156. Keep entries
> long and distinctive, and re-check the count after any edit.

### Why not CodeQL

GitHub's default-setup CodeQL requires either a public repository or GitHub
Advanced Security. Neither can be assumed here, and a CodeQL job that silently
no-ops on a private repo without GHAS is worse than no job — it looks like
coverage that isn't there. semgrep OSS runs anywhere.

### Why no second `cargo audit`

Already covered: `dependency-audit.yml`'s `rust-audit` job runs
`cargo audit --file src-tauri/Cargo.lock`. This workflow adds `cargo clippy`
over our own crate instead, which `cargo audit` never looks at.

### Known gaps, stated plainly

- **clippy runs on Linux only.** The `#[cfg]`-gated macOS and Windows branches
  of `src-tauri/src/lib.rs` are not linted here. `pr-checks.yml` already
  `cargo check`s all three platforms; three clippy targets would roughly triple
  the job for lint-grade findings.
- **semgrep skips `backend/tests/`** (64 files) via its default
  `.semgrepignore`. Bandit deliberately does scan tests, which covers it.
- **semgrep's rulesets come from the registry** and evolve independently of the
  pinned semgrep version. A registry rule addition can surface a new finding on
  a PR that changed nothing. When that happens, the finding is real — triage it,
  then either fix it or refresh the baseline.

---

## How the baseline works

A brand-new scanner on a mature codebase reports a wall of pre-existing
findings. **A check that is red the day it lands gets ignored within a week —
strictly worse than no check.** So the gate is not "zero findings", it is
"no findings absent from the committed baseline".

Baselines live in `scripts/ci/baselines/{bandit,semgrep,clippy}.json` and are
plain, reviewable JSON: one entry per accepted finding, with its rule, file,
line, severity and message.

### The fingerprint

Each finding is identified by

```
sha256( rule | file path | normalized source line | occurrence number )[:16]
```

Deliberately **excluding the line number**. Line numbers churn every time
someone adds an import; the offending code does not. Verified: inserting three
lines at the top of `document_service.py` (shifting every finding in the file)
produced **0 new findings**.

The occurrence number means N byte-identical findings in one file need N
baseline entries. Without it, three identical `urllib.request.urlopen(url)`
calls in `server.py` collapse to one fingerprint and a *fourth* added later
would be silently swallowed.

### Verifying it actually catches things

A file with four planted defects (`shell=True`, `hashlib.md5`, a hardcoded
token, `import subprocess`) was dropped into `backend/services/`:

```
### bandit — first-party static security scan

163 finding(s) in this tree; 4 not in the baseline.

#### :rotating_light: 4 NEW finding(s)

| Severity | Rule | Location | Message |
| --- | --- | --- | --- |
| HIGH | `B324` | `backend/services/planted_vuln.py:12` | Use of weak MD5 hash for security. |
| HIGH | `B602` | `backend/services/planted_vuln.py:8`  | subprocess call with shell=True identified. |
| LOW  | `B404` | `backend/services/planted_vuln.py:2`  | Consider possible security implications… |
| LOW  | `B105` | `backend/services/planted_vuln.py:4`  | Possible hardcoded password: 'hunter2-…' |

EXIT=1
```

### Refreshing a baseline

Never automatic. Refreshing is a reviewable commit, on purpose — the diff shows
exactly which findings someone decided to accept.

```sh
pip install 'bandit==1.9.4' 'semgrep==1.173.0'

# Python
bandit -c scripts/ci/bandit.yaml -f json -o bandit-report.json -q \
  -r backend scripts tools setup.py zip-bundle.py make_shortcut.py
python scripts/ci/security_baseline.py update --tool bandit \
  --report bandit-report.json --baseline scripts/ci/baselines/bandit.json

# JS/TS + cross-language
semgrep scan --config=p/security-audit --config=p/secrets \
  --config=p/owasp-top-ten --metrics=off --json -o semgrep-report.json \
  backend src chrome-extension mobile scripts tools src-tauri
python scripts/ci/security_baseline.py update --tool semgrep \
  --report semgrep-report.json --baseline scripts/ci/baselines/semgrep.json

# Rust (needs the Tauri system deps and the two build stubs — see the workflow)
cargo clippy --manifest-path src-tauri/Cargo.toml --message-format=json \
  > clippy-report.json
python scripts/ci/security_baseline.py update --tool clippy \
  --report clippy-report.json --baseline scripts/ci/baselines/clippy.json
```

Then **read the diff** before committing. An added entry means "we are
accepting this"; a removed entry means the finding is gone and the baseline
just got tighter.

Run `check` instead of `update` to see the comparison without writing
anything. `check` exits 1 on new findings, 0 otherwise.

### Bumping a scanner version

Scanner versions are pinned in `security-scan.yml` (`BANDIT_VERSION`,
`SEMGREP_VERSION`). An unpinned scanner picks up new rules on its own release
cadence and turns the check red on a PR that changed nothing — the same
broken-window failure the baseline exists to prevent. **Bump the pin and
refresh the baselines in the same dedicated PR**, so the new findings land as a
reviewable diff rather than as a surprise on someone else's branch.

---

## 2. `ai-code-review.yml` — scheduled AI review

Runs daily at 07:00 UTC and on `workflow_dispatch`. One request per run.
It opens a GitHub issue when it finds something; it never opens a PR and
never modifies code.

### Required secret

**`ANTHROPIC_API_KEY`** — Settings → Secrets and variables → Actions → New
repository secret.

Without it the workflow **skips with a clear message and exits green.** It
never fails a build and never blocks a release. Verified:

```
$ ANTHROPIC_API_KEY= python scripts/ci/ai_code_review.py --result r.json
### AI code review — skipped

`ANTHROPIC_API_KEY` is not set for this repository, so no review ran.
This is a skip, not a failure. …
$ cat r.json
{"status": "skipped-no-key", "findings": 0, …}
```

### Model and cost

Default **`claude-sonnet-5`** ($3 / $15 per MTok; $2 / $10 introductory through
2026-08-31). Sonnet rather than Haiku because the dominant failure mode of an
automated reviewer on this repo is a *confidently wrong* finding, and a stronger
reviewer is the mitigation. Sonnet rather than Opus because the quality
difference does not justify 1.7x on a job that runs unattended every day.
The model is a `workflow_dispatch` choice input — `claude-haiku-4-5` and
`claude-opus-5` are one dropdown away.

Measured on this repo's real history (v2.31.0 → v2.32.0, a 2-day window:
12 files, 125,398 prompt chars ≈ **32,380 input tokens**), assuming ~5,000
output tokens including adaptive thinking:

| Model | Typical run | Worst case (at the 350k-char cap, ~89k in / 8k out) |
| --- | --- | --- |
| `claude-haiku-4-5` | **$0.06** | $0.13 |
| `claude-sonnet-5` (intro) | **$0.12** | $0.26 |
| `claude-sonnet-5` (standard) | **$0.17** | $0.39 |
| `claude-opus-5` | $0.29 | $0.65 |

Absolute weekly ceiling on the default model, if all seven runs hit the size
cap: **~$2.71**. Realistically this repo has 3–4 active days a week, so
**well under a dollar per week.**

For contrast, the multi-agent review that prompted this design consumed 14% of
a weekly subscription limit in one run. The differences: this runs on CI
against an API key (never in an interactive session), issues exactly one
request, uses a mid-tier model, and caps its input.

### How the diff is bounded

Two independent bounds, plus an exclusion list.

**1. Time window.** Default `--since-days 1`. That is measured, not guessed —
this repo squash-merges several release PRs per day:

| Window | Files | Diff chars |
| --- | --- | --- |
| 1 day | 0 | 0 |
| 2 days | 12 | 125,227 |
| 5 days | 88 | 867,569 |
| 7 days | 118 | 1,345,147 |

A weekly cadence would be truncated on essentially every run. A daily one fits.
**On a day with no changes the script exits before it constructs a client — no
request, no spend.**

**2. Hard character cap.** `--max-chars` (default 350,000 ≈ 90k tokens) with a
per-file cap of 60,000. Excluded outright: lockfiles, `node_modules/`,
build output, release notes, the security baselines, and binary/media files.

**On a very large diff**, files are packed **smallest-first** — which maximizes
the number of files reviewed *completely*, at the cost of dropping the largest
changes. That trade is stated because it is a real one: the biggest diffs are
often the interesting ones. What makes it safe is that nothing is hidden:

- The **prompt** names every dropped file and instructs the model not to reason
  about them or treat their absence as evidence.
- The **report** opens with a warning listing them and the words
  "This review is partial — treat it as such."

The remedy for a truncated run is to dispatch it again with a shorter window.

A diff we could not show must never read as a diff that wasn't there.

### Guarding against confident fiction

A prior audit of this repo filed two "P0 release blockers" that were **both
already implemented** in the code it reviewed — a `require_backend_token`
middleware and a parent-PID watchdog — and cited endpoint paths that do not
exist. The system prompt names that incident and requires, for every finding:

- an exact **quoted line** with `file:line` — no quote, no finding;
- **no** function, endpoint, module, config key or flag that does not appear in
  the supplied material;
- an explicit **`why_not_already_handled`** field recording what was checked to
  rule out existing handling;
- a **`confidence`** of `verified` or `unverified`, where anything depending on
  code outside the diff must be `unverified`. Unverified findings render with a
  visible "_depends on code not in this diff_" badge.

It is told plainly that zero findings is a common and acceptable result, and
not to pad.

### The three repo-specific heuristics in the prompt

Each has cost this repo a release, and none are caught by its linters or its
779-test suite:

1. **A result you could not read must never render as a result that isn't
   there.** One branch where three are needed: present, absent, and
   not-yet-knowable. `except: return []`, `unwrap_or_default()`, `?? []`, and
   falsy checks that cannot tell "0 items" from "could not load".
2. **Test fixtures that supply an input production cannot supply.** The parser
   bug that shipped past 779 green tests because every fixture provided a
   `columnDateIso` that real Outlook Web never sends.
3. **Error messages that name a cause the code hasn't established.** "Microphone
   not found" on a branch reached for any non-zero exit code.

Findings are categorized against these, so a pattern that keeps recurring is
visible across issues.

### Local development

The script never needs to run locally, and shouldn't — it exists to keep this
work off the maintainer's interactive session. To exercise the plumbing without
credentials:

```sh
python -m pytest scripts/ci/test_ai_code_review.py -q   # 25 tests, no network
```

The tests use a stubbed client and cover diff bounding, the truncation
disclosure, the structured-output request shape, the text-mode fallback, the
refusal and empty-response paths, report rendering, missing-key degradation,
and the baseline fingerprinting.
