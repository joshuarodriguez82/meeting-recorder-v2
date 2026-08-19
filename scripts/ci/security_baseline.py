#!/usr/bin/env python3
"""Baseline-diff first-party static-security findings so CI fails only on NEW ones.

Why this exists
---------------
Pointing a fresh scanner at a mature codebase produces a wall of pre-existing
findings. A check that is red on the day it lands is a check everyone learns to
ignore within a week — strictly worse than no check at all. So instead of
gating on "zero findings", we gate on "no findings the baseline hasn't already
seen". Pre-existing debt stays visible (it is listed in the job summary and in
the committed baseline file) but does not block a PR that didn't cause it.

A finding is identified by a *fingerprint* that deliberately excludes the line
number: rule id + file path + the normalized source text of the offending line.
Line numbers churn every time someone adds an import; the offending code does
not. That means inserting 20 lines above a known finding does NOT resurrect it
as "new", while genuinely new code that trips the same rule DOES show up.

Usage
-----
    # Fail (exit 1) if the report contains findings absent from the baseline.
    security_baseline.py check  --tool bandit  --report bandit.json \\
        --baseline .github/security-baselines/bandit.json \\
        --summary "$GITHUB_STEP_SUMMARY"

    # Deliberately re-record the current tree as the accepted baseline.
    security_baseline.py update --tool bandit  --report bandit.json \\
        --baseline .github/security-baselines/bandit.json

`update` is never run automatically by CI — refreshing the baseline is a
reviewable commit, on purpose. See docs/ci-security-and-ai-review.md.

Exit codes (check):
    0  no new findings
    1  new findings present
    2  bad invocation / unreadable report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 2

# Bandit's JSON `code` field is a few source lines each prefixed with its line
# number and a space ("114 \t  h = hashlib.sha1()"). Strip that prefix so the
# fingerprint survives line renumbering.
_LINENO_PREFIX = re.compile(r"^\s*\d+\s")


def _normalize_snippet(text: str) -> str:
    """Collapse a code snippet to a whitespace-insensitive, line-number-free form."""
    out = []
    for raw_line in (text or "").splitlines():
        line = _LINENO_PREFIX.sub("", raw_line)
        line = " ".join(line.split())
        if line:
            out.append(line)
    return "\n".join(out)


def _fingerprint(rule: str, path: str, snippet: str, occurrence: int = 1) -> str:
    payload = "\x00".join((rule, path, _normalize_snippet(snippet), str(occurrence)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _number_occurrences(findings: list["Finding"]) -> list["Finding"]:
    """Re-fingerprint N identical findings in one file as occurrences 1..N.

    Without this, three byte-identical `urllib.request.urlopen(url)` calls in
    server.py collapse to a single fingerprint — and a fourth one added later
    would be silently swallowed by the baseline. Numbering by ascending line
    means the Nth occurrence is only "new" once the file genuinely grows to N
    of them. Deleting an occurrence renumbers the survivors downward, which
    surfaces the tail entry as "no longer present" rather than as a false new
    finding: it can under-report after a deletion, never over-report.
    """
    by_key: dict[tuple[str, str, str], list[Finding]] = {}
    for f in findings:
        by_key.setdefault((f["rule"], f["file"], f["_snippet"]), []).append(f)
    for (rule, path, snippet), group in by_key.items():
        group.sort(key=lambda f: (f["line"] is None, f["line"]))
        for i, f in enumerate(group, 1):
            f["fingerprint"] = _fingerprint(rule, path, snippet, i)
            f["occurrence"] = i
    for f in findings:
        f.pop("_snippet", None)
    return findings


class Finding(dict):
    """A normalized finding. dict-backed so it serializes straight to JSON."""

    @property
    def fingerprint(self) -> str:
        return self["fingerprint"]


def _make(rule: str, path: str, line: Any, severity: str, message: str, snippet: str) -> Finding:
    path = str(path or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    norm = _normalize_snippet(snippet)
    return Finding(
        # Placeholder — _number_occurrences() assigns the real value once the
        # whole report is known and duplicates can be counted.
        fingerprint=_fingerprint(rule, path, norm, 1),
        rule=rule,
        file=path,
        line=line,
        severity=(severity or "UNKNOWN").upper(),
        message=" ".join((message or "").split())[:300],
        _snippet=norm,
    )


def parse_bandit(report: dict) -> list[Finding]:
    findings = []
    for r in report.get("results", []) or []:
        findings.append(
            _make(
                rule=r.get("test_id", "?"),
                path=r.get("filename", "?"),
                line=r.get("line_number"),
                severity=r.get("issue_severity", "UNKNOWN"),
                message=r.get("issue_text", ""),
                snippet=r.get("code", ""),
            )
        )
    return findings


def parse_semgrep(report: dict) -> list[Finding]:
    findings = []
    for r in report.get("results", []) or []:
        extra = r.get("extra", {}) or {}
        findings.append(
            _make(
                rule=r.get("check_id", "?"),
                path=r.get("path", "?"),
                line=(r.get("start", {}) or {}).get("line"),
                severity=extra.get("severity", "UNKNOWN"),
                message=(extra.get("message") or ""),
                snippet=extra.get("lines", ""),
            )
        )
    return findings


def parse_clippy(report: dict) -> list[Finding]:
    """Parse `cargo clippy --message-format=json` (newline-delimited JSON).

    load_report() pre-wraps the NDJSON stream as {"results": [...]} so this
    parser sees the same shape as the others.
    """
    findings = []
    for entry in report.get("results", []) or []:
        if entry.get("reason") != "compiler-message":
            continue
        msg = entry.get("message") or {}
        level = msg.get("level")
        if level not in ("warning", "error"):
            continue
        primary = next((s for s in msg.get("spans", []) or [] if s.get("is_primary")), None)
        if primary is None:
            continue  # crate-level notes ("N warnings emitted") carry no location
        # Only lint our own crate. Dependency warnings are not ours to fix and
        # would make the baseline churn on every `cargo update`.
        path = primary.get("file_name", "?")
        if path.startswith("/") or "/.cargo/" in path or path.startswith(".."):
            continue
        snippet = "".join(t.get("text", "") for t in (primary.get("text") or []))
        findings.append(
            _make(
                rule=(msg.get("code") or {}).get("code") or level,
                path=f"src-tauri/{path}",
                line=primary.get("line_start"),
                severity="HIGH" if level == "error" else "MEDIUM",
                message=msg.get("message", ""),
                snippet=snippet,
            )
        )
    return findings


PARSERS: dict[str, Callable[[dict], list[Finding]]] = {
    "bandit": parse_bandit,
    "semgrep": parse_semgrep,
    "clippy": parse_clippy,
}


def load_report(path: Path, tool: str) -> list[Finding]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"error: report not found: {path}")
    if tool == "clippy":
        # cargo emits newline-delimited JSON, not one document.
        results = []
        for lineno, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"error: {path}:{lineno} is not valid JSON: {exc}")
        return _number_occurrences(parse_clippy({"results": results}))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: report {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"error: report {path} is not a JSON object")
    return _number_occurrences(PARSERS[tool](data))


def load_baseline(path: Path) -> dict[str, dict]:
    """Return {fingerprint: record}. A missing baseline is an EMPTY baseline.

    Deliberately three-state at the call site rather than here: callers that
    need to distinguish "no baseline file" from "baseline with zero findings"
    check `path.exists()` themselves — the two mean very different things when
    deciding whether a scan is trustworthy.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: baseline {path} is corrupt: {exc}")
    findings = data.get("findings", [])
    return {f["fingerprint"]: f for f in findings if "fingerprint" in f}


def write_baseline(path: Path, tool: str, findings: Iterable[Finding]) -> int:
    ordered = sorted(findings, key=lambda f: (f["file"], f["rule"], f.get("occurrence", 1)))
    seen: dict[str, Finding] = {}
    for f in ordered:
        seen.setdefault(f["fingerprint"], f)
    payload = {
        "schema": SCHEMA_VERSION,
        "tool": tool,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "Accepted pre-existing findings. CI fails only on findings absent "
            "from this list. Refresh deliberately with "
            "`scripts/ci/security_baseline.py update` and review the diff."
        ),
        "count": len(seen),
        "findings": list(seen.values()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return len(seen)


def _table(findings: list[Finding], limit: int = 40) -> list[str]:
    lines = ["| Severity | Rule | Location | Message |", "| --- | --- | --- | --- |"]
    for f in findings[:limit]:
        msg = f["message"].replace("|", "\\|")
        lines.append(f"| {f['severity']} | `{f['rule']}` | `{f['file']}:{f['line']}` | {msg} |")
    if len(findings) > limit:
        lines.append(f"| … | | | _{len(findings) - limit} more — see the uploaded report artifact._ |")
    return lines


def render_summary(
    tool: str, baseline_path: Path, baseline_existed: bool,
    total: int, new: list[Finding], fixed: list[dict],
) -> str:
    out = [f"### {tool} — first-party static security scan", ""]
    if not baseline_existed:
        out.append(
            f"> **No baseline at `{baseline_path}`.** Every finding below is being "
            f"treated as new. If this is the first run, create the baseline with "
            f"`python scripts/ci/security_baseline.py update --tool {tool} …`."
        )
        out.append("")
    out.append(f"{total} finding(s) in this tree; {len(new)} not in the baseline.")
    out.append("")
    if new:
        out.append(f"#### :rotating_light: {len(new)} NEW finding(s)")
        out.append("")
        out.extend(_table(new))
        out.append("")
        out.append(
            "Fix them, or — if they are accepted risk — refresh the baseline in a "
            "reviewable commit (see `docs/ci-security-and-ai-review.md`)."
        )
    else:
        out.append(":white_check_mark: No new findings.")
    out.append("")
    if fixed:
        out.append(
            f"<details><summary>{len(fixed)} baselined finding(s) no longer present "
            f"— baseline can be tightened</summary>"
        )
        out.append("")
        out.extend(
            f"- `{f.get('rule')}` in `{f.get('file')}` — {f.get('message', '')}" for f in fixed
        )
        out.append("")
        out.append("</details>")
        out.append("")
    return "\n".join(out)


def cmd_check(args: argparse.Namespace) -> int:
    report_path = Path(args.report)
    baseline_path = Path(args.baseline)
    baseline_existed = baseline_path.exists()

    findings = load_report(report_path, args.tool)
    baseline = load_baseline(baseline_path)

    seen: set[str] = set()
    new_fps: set[str] = set()
    new: list[Finding] = []
    for f in findings:
        if f.fingerprint in baseline:
            seen.add(f.fingerprint)
        elif f.fingerprint not in new_fps:
            new_fps.add(f.fingerprint)
            new.append(f)
    fixed = [rec for fp, rec in baseline.items() if fp not in seen]

    new.sort(key=lambda f: ({"HIGH": 0, "ERROR": 0, "MEDIUM": 1, "WARNING": 1}.get(f["severity"], 2),
                            f["file"], str(f["line"])))

    summary = render_summary(args.tool, baseline_path, baseline_existed, len(findings), new, fixed)
    print(summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"tool": args.tool, "total": len(findings),
                        "new": new, "fixed": fixed}, indent=2) + "\n",
            encoding="utf-8",
        )
    return 1 if new else 0


def cmd_update(args: argparse.Namespace) -> int:
    findings = load_report(Path(args.report), args.tool)
    n = write_baseline(Path(args.baseline), args.tool, findings)
    print(f"Wrote {n} baselined {args.tool} finding(s) to {args.baseline}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn in (("check", cmd_check), ("update", cmd_update)):
        sp = sub.add_parser(name)
        sp.add_argument("--tool", required=True, choices=sorted(PARSERS))
        sp.add_argument("--report", required=True, help="scanner JSON output")
        sp.add_argument("--baseline", required=True, help="baseline JSON path")
        if name == "check":
            sp.add_argument("--summary", default=None, help="append markdown here ($GITHUB_STEP_SUMMARY)")
            sp.add_argument("--json-out", default=None, help="write the machine-readable diff here")
        sp.set_defaults(func=fn)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
