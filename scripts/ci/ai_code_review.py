#!/usr/bin/env python3
"""Scheduled AI code review of a bounded diff. Runs on CI only — never locally.

Reads a git diff, asks Claude to review it, and writes a markdown report plus a
machine-readable result file. It does NOT touch GitHub itself: the calling
workflow decides whether to open an issue or post a PR comment. Keeping the
network split that way means the whole review path can be unit-tested with a
stubbed client and no credentials.

Design constraints this file exists to satisfy:

1. COST. The repo owner burned 14% of a weekly limit on one multi-agent review.
   This runs on CI against an API key, on a mid-tier model, over a diff capped
   in bytes — not the whole repo, and never in an interactive session.

2. BOUNDED INPUT. Default scope is "commits in the last N days" (or an
   explicit --base, e.g. a PR merge-base). --since-days defaults to 1, not 7,
   and that is measured, not guessed: this repo squash-merges several release
   PRs per day, so on the tree at v2.32.0 the trailing windows are

       1 day     0 files          0 chars
       2 days   12 files    125,227 chars
       5 days   88 files    867,569 chars
       7 days  118 files  1,345,147 chars

   A weekly cadence would therefore be truncated essentially every run. A
   daily cadence fits the budget, and on a quiet day the script exits BEFORE
   constructing a client — no request, no spend.

   If a window still exceeds --max-chars, files are packed smallest-first.
   That maximizes the number of files reviewed *completely*, at the cost of
   dropping the biggest changes — which are often the interesting ones. The
   remedy is to re-run with a shorter window, and both the report and the
   prompt name every dropped file explicitly. A diff we could not show must
   never read as a diff that wasn't there.

3. NO CONFIDENT FICTION. See build_system_prompt(). A prior audit of this repo
   filed two "P0 release blockers" that were both already implemented, and
   cited endpoint paths that do not exist. The prompt is written against that
   specific failure.

Exit codes: 0 always, unless invoked wrongly (2). A review tool must never fail
a build — its output is advisory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_CHARS = 350_000
DEFAULT_MAX_FILE_CHARS = 60_000
DEFAULT_SINCE_DAYS = 1

# Paths whose diffs carry no review signal but plenty of bytes. Lockfiles and
# generated bundles would eat the entire budget on their own.
EXCLUDE_PATTERNS = [
    r"(^|/)package-lock\.json$",
    r"(^|/)Cargo\.lock$",
    r"(^|/)pnpm-lock\.yaml$",
    r"(^|/)yarn\.lock$",
    r"(^|/)\.next/",
    r"(^|/)node_modules/",
    r"(^|/)out/",
    r"(^|/)dist/",
    r"(^|/)target/",
    r"(^|/)docs/release-notes/",
    r"(^|/)scripts/ci/baselines/",
    r"\.(png|jpg|jpeg|gif|ico|icns|woff2?|mp3|wav|zip|pdf)$",
]
_EXCLUDE = [re.compile(p) for p in EXCLUDE_PATTERNS]


def excluded(path: str) -> bool:
    return any(rx.search(path) for rx in _EXCLUDE)


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def git(*args: str, cwd: str | None = None) -> str:
    # errors="replace", not the default strict: this repo has committed files
    # with non-UTF-8 bytes (cp1252 punctuation in older release notes), and a
    # UnicodeDecodeError here would abort the whole review over one stray
    # byte in one hunk. Mangling a character is the right trade against
    # losing the run.
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or '').strip()}")
    return proc.stdout or ""


def resolve_range(base: str | None, since_days: int) -> tuple[str, str]:
    """Return (base_rev, head_rev) for the review window.

    Three outcomes, kept distinct on purpose:
      - explicit --base given            -> use it
      - a commit exists before the cutoff -> use that commit
      - the history does not reach back that far (shallow clone, young repo)
        -> fall back to the repo's root commit, and let the caller say so
    """
    head = git("rev-parse", "HEAD").strip()
    if base:
        return git("rev-parse", base).strip(), head
    cutoff = f"{since_days} days ago"
    found = git("rev-list", "-1", f"--before={cutoff}", "HEAD").strip()
    if found:
        return found, head
    root = git("rev-list", "--max-parents=0", "HEAD").strip().splitlines()
    return (root[-1] if root else head), head


def changed_files(base: str, head: str) -> list[str]:
    out = git("diff", "--name-only", f"{base}..{head}")
    return [ln for ln in out.splitlines() if ln and not excluded(ln)]


def file_diff(base: str, head: str, path: str) -> str:
    return git("diff", "--unified=3", f"{base}..{head}", "--", path)


def commit_log(base: str, head: str) -> str:
    return git("log", "--no-merges", "--format=- %h %s (%an)", f"{base}..{head}")


# --------------------------------------------------------------------------
# diff assembly under a byte budget
# --------------------------------------------------------------------------

@dataclass
class DiffBundle:
    base: str
    head: str
    commits: str
    files_included: list[str] = field(default_factory=list)
    files_dropped: list[tuple[str, int]] = field(default_factory=list)
    text: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.files_included

    @property
    def truncated(self) -> bool:
        return bool(self.files_dropped)


def build_bundle(
    base: str,
    head: str,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS,
    diff_fn=file_diff,
    log_fn=commit_log,
    files_fn=changed_files,
) -> DiffBundle:
    """Collect per-file diffs smallest-first until the budget runs out.

    Smallest-first is deliberate: one 200KB generated file must not crowd out
    twenty 2KB hand-written changes. Whatever does not fit is recorded in
    files_dropped and surfaced in the report — a diff we could not show is
    never allowed to look like a diff that wasn't there.
    """
    bundle = DiffBundle(base=base, head=head, commits=log_fn(base, head))
    paths = files_fn(base, head)
    sized: list[tuple[int, str, str]] = []
    for p in paths:
        d = diff_fn(base, head, p)
        if len(d) > max_file_chars:
            d = d[:max_file_chars] + f"\n... [file diff truncated at {max_file_chars} chars]\n"
        sized.append((len(d), p, d))
    sized.sort(key=lambda t: t[0])

    used = 0
    chunks: list[str] = []
    for size, path, text in sized:
        if used + size > max_chars:
            bundle.files_dropped.append((path, size))
            continue
        used += size
        chunks.append(text)
        bundle.files_included.append(path)
    bundle.files_dropped.sort(key=lambda t: -t[1])
    bundle.text = "".join(chunks)
    return bundle


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are reviewing a bounded git diff from the "meeting-recorder-v2" repository: \
a Tauri desktop app (Rust shell) wrapping a Next.js frontend and a Python \
FastAPI sidecar that records meetings, transcribes and diarizes audio, and \
syncs calendar data.

Your output is filed as a GitHub issue that a solo maintainer reads. A wrong \
finding costs more than a missed one: it burns the maintainer's time and \
teaches them to stop reading these reports. Optimize for precision, not recall.

## Verify before you report

This repository has been burned by exactly this failure mode. A prior automated \
audit filed two "P0 release blockers" that were BOTH already implemented in the \
code it was reviewing — a `require_backend_token` auth middleware and a \
parent-PID watchdog — and it cited API endpoint paths that do not exist \
anywhere in the repo. Do not repeat that.

Concretely, before you report anything:

- Quote the exact line from the provided material that shows the defect, and \
give its `file:line`. If you cannot quote it, you cannot report it.
- Never name a function, endpoint, module, config key, or CLI flag that does \
not appear in the material you were given. Do not infer that something exists \
because a codebase like this usually has one.
- Ask whether the code already handles it. Guards, early returns, `try`/`except`, \
validation in a caller, and `#[cfg]` branches all count as handling. If the \
handling is plausibly elsewhere in a file you were not shown, say so and mark \
the finding `unverified` — do not assert the bug.
- You are seeing a DIFF, not the whole repository. Absence of evidence is not \
evidence of absence. When a claim depends on code outside this diff, the honest \
answer is `unverified` with a one-line note on what you'd need to check.

Prefer reporting three findings you are sure of over ten you are not. Zero \
findings is a completely acceptable, and common, result. Do not manufacture \
findings to justify the run.

## What to look for

General correctness and security defects in the changed code — but weight these \
three patterns heavily. Each has cost this repo a release, and none of them are \
caught by its linters or its test suite:

1. **A result you could not read must never render as a result that is not \
there.** Look for code with one branch where three are needed: value present, \
value absent, and value not-yet-knowable (fetch failed, permission denied, \
still loading, file unreadable). Collapsing "unknown" into "empty" produces a \
UI that confidently displays nothing, and a caller that confidently proceeds. \
An `except: return []`, an `unwrap_or_default()`, a `?? []`, or a falsy check \
that cannot distinguish "0 items" from "could not load" are all instances.

2. **Test fixtures that supply an input production cannot supply.** This repo \
shipped a parser bug past 779 green tests because every fixture provided a \
`columnDateIso` field that real Outlook Web never sends. When a test changes, \
ask where each fixture field actually comes from in production, and whether the \
real upstream can omit it, send it empty, or send a different type. A green \
test proving behaviour on impossible input is worse than no test.

3. **Error messages that name a cause the code has not established.** A message \
like "microphone not found" on a branch that is reached for any non-zero exit \
code sends the maintainer, and the user, down the wrong path. Check that each \
message's claim is actually implied by the condition guarding it.

Also worth reporting when clearly evidenced in the diff: injection and path \
traversal on user-supplied paths, credentials or tokens in source, unsafe \
subprocess construction, resource leaks on error paths, and concurrency races \
around session or file state.

## Do not

- Do not report style, formatting, naming, or "consider extracting this".
- Do not report a missing test unless the diff changes behaviour that the diff \
itself shows to be untested.
- Do not propose refactors, rewrites, or architecture changes.
- Do not report dependency CVEs — a separate workflow already covers those.
- Do not pad. If nothing meets the bar, return an empty findings list.
"""

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "One line, specific."},
                    "file": {"type": "string", "description": "Repo-relative path from the diff."},
                    "line": {"type": "integer", "description": "Line number in the new file."},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "confidence": {
                        "type": "string",
                        "enum": ["verified", "unverified"],
                        "description": (
                            "'verified' only if you quoted the defect from the material "
                            "given. 'unverified' if the claim depends on code you were "
                            "not shown."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "unreadable-vs-absent",
                            "impossible-fixture",
                            "unestablished-error-cause",
                            "security",
                            "correctness",
                            "other",
                        ],
                    },
                    "evidence": {
                        "type": "string",
                        "description": "The exact quoted line(s) from the diff showing the defect.",
                    },
                    "explanation": {"type": "string"},
                    "why_not_already_handled": {
                        "type": "string",
                        "description": (
                            "What you checked to rule out the code already handling this, "
                            "or what you could not check."
                        ),
                    },
                },
                "required": [
                    "title", "file", "line", "severity", "confidence",
                    "category", "evidence", "explanation", "why_not_already_handled",
                ],
                "additionalProperties": False,
            },
        },
        "summary": {
            "type": "string",
            "description": "Two sentences max. Say plainly if nothing was found.",
        },
    },
    "required": ["findings", "summary"],
    "additionalProperties": False,
}


def build_user_prompt(bundle: DiffBundle) -> str:
    parts = [
        f"Review the changes between `{bundle.base[:12]}` and `{bundle.head[:12]}`.",
        "",
        f"## Commits ({len(bundle.files_included)} file(s) in scope)",
        "",
        bundle.commits.strip() or "(no commit subjects available)",
        "",
    ]
    if bundle.files_dropped:
        parts += [
            "## Not shown to you",
            "",
            "These changed files did NOT fit the size budget. Do not reason about "
            "them, and do not treat their absence as evidence of anything:",
            "",
            *[f"- `{p}` ({n} chars)" for p, n in bundle.files_dropped],
            "",
        ]
    parts += ["## Diff", "", "```diff", bundle.text.rstrip(), "```"]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# model call
# --------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a plain-text response (fallback path)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start : end + 1] if start != -1 and end > start else None
    if candidate is None:
        raise ValueError("no JSON object in model response")
    return json.loads(candidate)


def review(client, bundle: DiffBundle, model: str, effort: str = "high") -> dict:
    """Call the model once and return the parsed {findings, summary} payload.

    Structured output is the primary path. If the API rejects the
    output_config.format (older model, provider that lacks it), fall back to
    asking for JSON in the text and parsing it — a rejected request must not
    silently become "no findings".
    """
    user = build_user_prompt(bundle)
    common = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
    )
    try:
        response = client.messages.create(
            **common,
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": FINDINGS_SCHEMA},
            },
        )
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if type(exc).__name__ not in ("BadRequestError", "UnprocessableEntityError"):
            raise
        print(f"note: structured output rejected ({exc}); retrying in text mode", file=sys.stderr)
        response = client.messages.create(
            **{
                **common,
                "messages": [
                    {
                        "role": "user",
                        "content": user
                        + "\n\nRespond with ONLY a JSON object matching this schema:\n"
                        + json.dumps(FINDINGS_SCHEMA, indent=2),
                    }
                ],
            }
        )

    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError("model declined to review this diff")
    text = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    )
    if not text.strip():
        raise RuntimeError("model returned no text content")
    data = _extract_json(text)
    usage = getattr(response, "usage", None)
    data["_usage"] = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
    return data


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
SEV_ICON = {"high": ":red_circle:", "medium": ":yellow_circle:", "low": ":white_circle:"}


def render_report(bundle: DiffBundle, data: dict, model: str) -> str:
    findings = sorted(
        data.get("findings", []) or [],
        key=lambda f: (SEV_ORDER.get(f.get("severity", "low"), 3),
                       f.get("confidence") != "verified"),
    )
    out = [
        f"Automated review of `{bundle.base[:12]}..{bundle.head[:12]}` "
        f"({len(bundle.files_included)} file(s)), by `{model}`.",
        "",
        f"**Summary.** {data.get('summary', '(none given)')}",
        "",
    ]
    if bundle.truncated:
        out += [
            f"> :warning: The diff exceeded the size budget. "
            f"{len(bundle.files_dropped)} file(s) were **not reviewed**: "
            + ", ".join(f"`{p}`" for p, _ in bundle.files_dropped[:15])
            + ("…" if len(bundle.files_dropped) > 15 else "")
            + ". This review is partial — treat it as such.",
            "",
        ]
    if not findings:
        out += ["No findings met the reporting bar.", ""]
    for i, f in enumerate(findings, 1):
        icon = SEV_ICON.get(f.get("severity", "low"), ":white_circle:")
        conf = f.get("confidence", "unverified")
        badge = "" if conf == "verified" else "  _(unverified — depends on code not in this diff)_"
        out += [
            f"### {i}. {icon} {f.get('title', 'untitled')}{badge}",
            "",
            f"`{f.get('file')}:{f.get('line')}` · severity **{f.get('severity')}** · "
            f"category `{f.get('category')}`",
            "",
            "```",
            str(f.get("evidence", "")).strip(),
            "```",
            "",
            str(f.get("explanation", "")).strip(),
            "",
            f"_Checked against existing handling:_ {str(f.get('why_not_already_handled', '')).strip()}",
            "",
        ]
    out += [
        "---",
        "",
        "<sub>Generated by `.github/workflows/ai-code-review.yml`. Findings are "
        "advisory and may be wrong — verify before acting. Nothing here modifies "
        "code or opens pull requests.</sub>",
    ]
    return "\n".join(out)


def note(message: str, summary_path: str | None) -> None:
    print(message)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=None,
                    help="Base revision. Default: the newest commit older than --since-days.")
    ap.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--max-file-chars", type=int, default=DEFAULT_MAX_FILE_CHARS)
    ap.add_argument("--out", default="ai-review.md", help="Markdown report path.")
    ap.add_argument("--result", default="ai-review-result.json",
                    help="Machine-readable result the workflow branches on.")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = ap.parse_args(argv)

    def finish(status: str, findings: int = 0, message: str = "") -> int:
        Path(args.result).write_text(
            json.dumps({"status": status, "findings": findings, "message": message},
                       indent=2) + "\n",
            encoding="utf-8",
        )
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        note(
            "### AI code review — skipped\n\n"
            "`ANTHROPIC_API_KEY` is not set for this repository, so no review ran. "
            "This is a skip, not a failure. Add the secret under "
            "**Settings → Secrets and variables → Actions** to enable it.",
            args.summary,
        )
        return finish("skipped-no-key", 0, "ANTHROPIC_API_KEY not set")

    try:
        base, head = resolve_range(args.base, args.since_days)
    except RuntimeError as exc:
        note(f"### AI code review — skipped\n\nCould not resolve the review range: {exc}",
             args.summary)
        return finish("skipped-no-range", 0, str(exc))

    bundle = build_bundle(base, head, args.max_chars, args.max_file_chars)
    if bundle.is_empty:
        note(
            f"### AI code review — nothing to review\n\n"
            f"No reviewable changes between `{base[:12]}` and `{head[:12]}` "
            f"(last {args.since_days} day(s)).",
            args.summary,
        )
        return finish("empty", 0, "no reviewable changes")

    try:
        import anthropic
    except ImportError:
        note("### AI code review — skipped\n\nThe `anthropic` package is not installed.",
             args.summary)
        return finish("skipped-no-sdk", 0, "anthropic not installed")

    client = anthropic.Anthropic()
    try:
        data = review(client, bundle, args.model, args.effort)
    except Exception as exc:  # noqa: BLE001
        # An API error is a skip with a loud message, never a failed build — a
        # rate limit at 07:00 UTC on a Monday must not look like a code defect.
        note(f"### AI code review — could not complete\n\n`{type(exc).__name__}`: {exc}",
             args.summary)
        return finish("error", 0, f"{type(exc).__name__}: {exc}")

    report = render_report(bundle, data, args.model)
    Path(args.out).write_text(report + "\n", encoding="utf-8")
    n = len(data.get("findings", []) or [])
    usage = data.get("_usage", {})
    note(
        f"### AI code review\n\n{n} finding(s) over {len(bundle.files_included)} file(s) "
        f"(`{base[:12]}..{head[:12]}`, model `{args.model}`, "
        f"{usage.get('input_tokens')} in / {usage.get('output_tokens')} out tokens).\n\n"
        + report,
        args.summary,
    )
    return finish("ok", n, data.get("summary", ""))


if __name__ == "__main__":
    sys.exit(main())
