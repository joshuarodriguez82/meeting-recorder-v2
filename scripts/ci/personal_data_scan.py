#!/usr/bin/env python3
"""Scan the tracked tree for real personal / customer data.

Why this exists
---------------
This repository is public and meant to be cloneable by anyone. In 2026-08 a
scrub removed real colleague names, real customer names, real meeting subjects,
a personal email address and personal home-directory paths from source, tests,
fixtures, design mockups and — worst of all — 14 published release-notes files,
whose text release.yml copies verbatim into the GitHub Release body.

Nothing structural stopped any of that from being committed, so nothing
structural stops it happening again. This scanner is that structure.

What it is NOT
--------------
Not a general PII detector. A regex that tries to recognise "a person's name"
either misses most of them or fires on every capitalised word; both failure
modes end with the check switched off. This is deliberately a *curated
deny-list* of the specific identities that leaked here, plus three pattern
classes (Windows home path, POSIX home path, non-example email address) that
are precise enough to stay quiet.

Why the deny-list is hashed
---------------------------
A plain-text list of the real names and customers would put them straight back
into the public repo the scrub just cleaned — a grep-able roster of colleagues,
sitting in `scripts/ci/`. So `personal-data-terms.json` stores only
`sha256(salt | normalized term)[:16]`, and matching works by normalising each
source line the same way and hashing its 1-, 2- and 3-word windows.

Stated plainly, because it matters: **this is obfuscation, not secrecy.** The
salt is in the file. Anyone with a surname dictionary can confirm a guess. What
it buys is that the repo no longer *states* who these people are — no scraper,
no casual reader and no `grep` recovers the roster — while CI keeps its exact
matching power. For a public repo that is the right trade; do not describe it
as encryption.

Consequence: a finding cannot name the term it matched (that would re-disclose
it in a public job summary). It reports the file, the line number and the
category. The developer who just typed the string knows which one it was, and
`test-term` will confirm.

Commands
--------
    # CI: write a JSON report for security_baseline.py to diff.
    python scripts/ci/personal_data_scan.py --report personal-data-report.json

    # Add a name/customer to the deny-list (appends the hash, never the text).
    python scripts/ci/personal_data_scan.py add-term "Some Person"

    # Ask whether something is already denied.
    python scripts/ci/personal_data_scan.py test-term "Some Person"

Exit codes:
    0  scan completed (findings, if any, are in the JSON report)
    1  test-term: the term is NOT in the deny-list
    2  bad invocation / unreadable term list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TERMS = Path(__file__).resolve().parent / "personal-data-terms.json"

# A source line longer than this is almost certainly minified or generated;
# a match inside one produces an unreadable finding and a useless fingerprint.
MAX_LINE = 500

# Word characters incl. Latin-1/Latin-Extended letters, so "Døe" and
# "Zoë" tokenize as single words rather than splitting at the diacritic.
_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ɏ]+")


def normalize_term(text: str) -> str:
    """Casefold and reduce to space-separated word tokens.

    "Rodriguez, Joshua", "user" and "NorthwindDigital.com" all reduce
    to stable token sequences, so one stored hash covers the punctuation
    variants of the same identity.
    """
    return " ".join(w.casefold() for w in _WORD_RE.findall(text or ""))


def term_hash(salt: str, text: str) -> str:
    payload = f"{salt}\x00{normalize_term(text)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _load_terms(path: Path, *, require_nonempty: bool = True) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"error: term list not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: term list {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"error: term list {path} is not a JSON object")
    # A truncated or half-merged term list must not read as "clean tree".
    # `add-term` is the one caller that legitimately sees an empty list.
    if require_nonempty and not (data.get("denied") or {}).get("hashes"):
        raise SystemExit(
            f"error: {path} has an empty deny-list — refusing to run a check "
            f"that cannot fail. Restore the file from git."
        )
    return data


def _tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"error: could not list tracked files: {exc}")
    return [line for line in out.splitlines() if line]


def _windows(line: str, max_n: int) -> Iterator[str]:
    """Every 1..max_n-word window of a normalized line, longest first.

    Longest first so "rocket mortgage" is reported once as a 2-gram rather than
    twice as two unrelated 1-grams — only relevant if a single-word term ever
    shares a token with a multi-word one.
    """
    tokens = normalize_term(line).split()
    for n in range(min(max_n, len(tokens)), 0, -1):
        for i in range(len(tokens) - n + 1):
            yield " ".join(tokens[i:i + n])


def scan(root: Path, terms: dict[str, Any]) -> list[dict[str, Any]]:
    salt = terms.get("salt", "")
    denied = set((terms.get("denied") or {}).get("hashes") or [])
    max_n = int((terms.get("denied") or {}).get("max_ngram") or 3)

    allow = terms.get("allow") or {}
    allow_substrings = allow.get("line_substrings") or []
    allow_paths = set(allow.get("paths") or [])

    scan_cfg = terms.get("scan") or {}
    skip_suffixes = tuple(scan_cfg.get("skip_suffixes") or [])
    skip_paths = set(scan_cfg.get("skip_paths") or [])

    rules = (terms.get("patterns") or {}).get("rules") or []
    compiled = [(r["id"], re.compile(r["regex"]), r.get("message", r["id"]),
                 set(r.get("allow_captures") or [])) for r in rules]

    results: list[dict[str, Any]] = []

    for rel in _tracked_files(root):
        if rel in skip_paths or rel in allow_paths:
            continue
        if rel.lower().endswith(skip_suffixes):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            if len(line) > MAX_LINE:
                continue
            if any(sub in line for sub in allow_substrings):
                continue

            seen_here = False
            for window in _windows(line, max_n):
                if term_hash(salt, window) in denied and not seen_here:
                    seen_here = True
                    results.append({
                        "rule": "denied-term",
                        "path": rel,
                        "line": lineno,
                        # Deliberately NOT the matched text — see the module
                        # docstring. Job summaries are public.
                        "match": "<redacted>",
                        "message": (
                            "a deny-listed real name / customer / employer "
                            "appears on this line — replace it with a "
                            "fictional placeholder before committing"
                        ),
                        "text": f"{rel}:{lineno}",
                    })

            for rule_id, rx, message, allowed in compiled:
                for match in rx.finditer(line):
                    captured = match.group(1) if match.groups() else ""
                    if captured in allowed:
                        continue
                    results.append({
                        "rule": rule_id,
                        "path": rel,
                        "line": lineno,
                        "match": match.group(0),
                        "message": message,
                        "text": line.strip(),
                    })
    return results


# ── commands ────────────────────────────────────────────────────────────


def cmd_scan(args: argparse.Namespace) -> int:
    terms = _load_terms(args.terms)
    results = scan(args.root.resolve(), terms)
    payload = {
        "tool": "personal-data",
        "terms_schema": terms.get("schema"),
        "count": len(results),
        "results": results,
    }
    blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.report:
        args.report.write_text(blob, encoding="utf-8")
    else:
        sys.stdout.write(blob)

    for r in results:
        print(f"{r['path']}:{r['line']}: [{r['rule']}] {r['message']}",
              file=sys.stderr)
    print(f"personal-data scan: {len(results)} finding(s)", file=sys.stderr)
    return 0


def cmd_add_term(args: argparse.Namespace) -> int:
    terms = _load_terms(args.terms, require_nonempty=False)
    salt = terms.get("salt", "")
    normalized = normalize_term(args.term)
    if not normalized:
        raise SystemExit("error: term normalizes to nothing")
    words = len(normalized.split())
    max_n = int(terms["denied"].get("max_ngram") or 3)
    if words > max_n:
        raise SystemExit(
            f"error: '{args.term}' is {words} words but max_ngram is {max_n}. "
            f"Raise max_ngram (it costs one extra pass per line) or deny a "
            f"shorter, still-identifying part of the name."
        )
    h = term_hash(salt, args.term)
    hashes = terms["denied"]["hashes"]
    if h in hashes:
        print("already denied — nothing to do")
        return 0
    hashes.append(h)
    hashes.sort()
    terms["denied"]["count"] = len(hashes)
    args.terms.write_text(
        json.dumps(terms, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"added {h} ({words}-word term) — {len(hashes)} denied terms total")
    print("Commit the term list WITHOUT mentioning the term in the commit "
          "message or PR description.")
    return 0


def cmd_test_term(args: argparse.Namespace) -> int:
    terms = _load_terms(args.terms)
    h = term_hash(terms.get("salt", ""), args.term)
    hit = h in set(terms["denied"]["hashes"])
    print(f"{h}  {'DENIED' if hit else 'not in the deny-list'}")
    return 0 if hit else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", type=Path, default=REPO_ROOT,
                   help="repository root to scan (default: this repo)")
    p.add_argument("--terms", type=Path, default=DEFAULT_TERMS,
                   help="deny-list JSON (default: scripts/ci/personal-data-terms.json)")
    p.add_argument("--report", type=Path,
                   help="write the JSON report here (default: stdout)")
    p.set_defaults(func=cmd_scan)

    sub = p.add_subparsers()
    a = sub.add_parser("add-term", help="append a term's hash to the deny-list")
    a.add_argument("term")
    a.set_defaults(func=cmd_add_term)

    t = sub.add_parser("test-term", help="check whether a term is already denied")
    t.add_argument("term")
    t.set_defaults(func=cmd_test_term)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
