"""
Lock src/lib/owner-grouping.ts to services/owner_service.py.

The frontend can't import Python, so owner-grouping.ts is a hand-kept
mirror of the split/normalise rules (see that file's module docstring)
— the same mirroring convention this codebase already uses for
item-status hashing and markdown parsing. That convention always ships
with a LOCK so the two sides can't quietly drift apart; the precedent
is test_export_reconcile.py::test_base_name_matches_export_service,
which calls both implementations on the same inputs and asserts equal
output. That trick needs both sides in the same language — Python here
— so this file does the cross-language equivalent two ways:

1. REQUIRED, dependency-free: parse the regex *patterns* for the
   delimiter set / org-suffix / trailing-punctuation rules out of the
   TypeScript source text (same spirit as the crash-recency test
   scanning source text, and the boot-smoke test walking an AST) and
   assert they're character-for-character identical to the Python
   patterns. This is the test that actually catches "someone added a
   delimiter — a comma especially — to one side only", which is the
   realistic failure mode: a one-line pattern edit on just one side.

2. BONUS, best-effort: if `node` is on PATH, actually import the real
   .ts file (Node 22's --experimental-strip-types needs no build step
   for this file — it has no JSX/decorators) and run it against a
   battery of the same fixtures backend/tests/test_owner_service.py
   uses, diffing real outputs against the real Python functions. This
   is strictly stronger evidence than the regex-pattern check, but
   depends on a node binary and an experimental flag that may not be
   installed in every CI image, so any environment/tooling hiccup
   SKIPS rather than fails — only an actual output mismatch fails.
   Never make this required per module docstring guidance ("don't add
   a flaky dependency").
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from services.owner_service import (
    _ORG_SUFFIX_RE,
    _SPLIT_RE,
    _TRAILING_PUNCT_RE,
)

TS_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "lib" / "owner-grouping.ts"
)

# Matches `const NAME = /pattern/flags;` in the TS source, tolerant of
# escaped characters (incl. an escaped "/") inside the pattern body.
_TS_REGEX_LITERAL_RE = re.compile(
    r"const\s+(?P<name>\w+)\s*=\s*/(?P<pattern>(?:\\.|[^/\\])*)/(?P<flags>[a-z]*)\s*;"
)


def _extract_ts_regexes(source: str) -> dict:
    """{name: (pattern, flags)} for every `const X = /.../flags;` in
    the TS source, with the JS-only `\\/` escape undone so the pattern
    text is directly comparable to a Python regex's raw string."""
    out = {}
    for m in _TS_REGEX_LITERAL_RE.finditer(source):
        pattern = m.group("pattern").replace("\\/", "/")
        out[m.group("name")] = (pattern, m.group("flags"))
    return out


@pytest.fixture(scope="module")
def ts_regexes() -> dict:
    assert TS_PATH.exists(), f"owner-grouping.ts not found at {TS_PATH}"
    source = TS_PATH.read_text(encoding="utf-8")
    regexes = _extract_ts_regexes(source)
    # Fail loudly (not silently pass with an empty dict) if the source
    # was refactored enough that the extractor no longer finds them —
    # a lock that can't find what it's locking is worse than no lock.
    for name in ("SPLIT_RE", "ORG_SUFFIX_RE", "TRAILING_PUNCT_RE"):
        assert name in regexes, (
            f"Could not find `const {name} = /.../;` in {TS_PATH} — "
            f"either the regex was renamed/restructured (update this "
            f"test's extractor) or it was removed (update owner_service.py "
            f"too, they must stay in lock-step)."
        )
    return regexes


class TestRegexSourceLock:
    """Tier 1/2 rules encoded as regex patterns, compared character
    for character. This is the test that fires the day someone adds a
    delimiter to only one side."""

    def test_split_pattern_matches(self, ts_regexes):
        ts_pattern, ts_flags = ts_regexes["SPLIT_RE"]
        assert ts_pattern == _SPLIT_RE.pattern, (
            "owner-grouping.ts's SPLIT_RE and owner_service.py's "
            "_SPLIT_RE have drifted — Follow Ups (TS) and Commitments "
            "(Python) would split multi-owner strings differently."
        )
        # Case-insensitivity must match too ("AND" vs "and").
        assert "i" in ts_flags

    def test_org_suffix_pattern_matches(self, ts_regexes):
        ts_pattern, _ = ts_regexes["ORG_SUFFIX_RE"]
        assert ts_pattern == _ORG_SUFFIX_RE.pattern

    def test_trailing_punct_pattern_matches(self, ts_regexes):
        ts_pattern, _ = ts_regexes["TRAILING_PUNCT_RE"]
        assert ts_pattern == _TRAILING_PUNCT_RE.pattern

    def test_neither_side_splits_on_comma(self, ts_regexes):
        """The specific regression this whole module exists to
        prevent — asserted directly (not just implied by the pattern
        equality checks above) because it's the realistic failure
        mode the coordinator flagged: a comma added to ONE side only."""
        ts_pattern, _ = ts_regexes["SPLIT_RE"]
        assert "," not in ts_pattern
        assert "," not in _SPLIT_RE.pattern

    def test_split_pattern_covers_slash_ampersand_and_word_and(self, ts_regexes):
        ts_pattern, _ = ts_regexes["SPLIT_RE"]
        for token in ("/", "&", r"\band\b"):
            assert token in ts_pattern, f"TS SPLIT_RE missing {token!r}"
            assert token in _SPLIT_RE.pattern, f"Python _SPLIT_RE missing {token!r}"


# ── Bonus: real behavioral diff via node, best-effort ──────────────────

# Shared fixtures — a subset of test_owner_service.py's cases, run
# through BOTH implementations for real (not just pattern-compared).
_SPLIT_FIXTURES = [
    "Mark/Josh",
    "Osmo/Craig/Josh",
    "Melissa & Kendra",
    "Melissa and Kendra",
    "melissa AND kendra",
    "Andrew and Sanders",
    "Roe, Bob Jr. [US-US]",
    "Smith, John",
    "",
    "   ",
    "Josh / josh",
    "Josh",
]
_NORMALIZE_FIXTURES = [
    "  Josh  ", "JOSH", "Josh.", "Josh,", "Josh (AWS)", "Josh (Umbrella)",
    "Jake (AWS)", "Josh   Rodriguez", "Roe, Bob Jr.",
]

_NODE_HARNESS = """
import { splitOwners, normalizeOwner } from "__TS_PATH__";
const splitFixtures = __SPLIT_FIXTURES__;
const normFixtures = __NORM_FIXTURES__;
const out = {
  split: splitFixtures.map((s) => splitOwners(s)),
  normalize: normFixtures.map((s) => {
    const r = normalizeOwner(s);
    return [r.key, r.display];
  }),
};
console.log(JSON.stringify(out));
"""


def _run_node_harness():
    """Returns parsed {{split: [...], normalize: [...]}} from the real
    TS file via node, or None if node isn't usable here (missing
    binary, unsupported flag, import error, etc.) — anything short of
    "it ran and printed JSON" degrades to None rather than raising, so
    this stays a bonus check, never a source of CI flakiness."""
    node = shutil.which("node")
    if not node:
        return None
    script = (
        _NODE_HARNESS
        .replace("__TS_PATH__", TS_PATH.as_posix())
        .replace("__SPLIT_FIXTURES__", json.dumps(_SPLIT_FIXTURES))
        .replace("__NORM_FIXTURES__", json.dumps(_NORMALIZE_FIXTURES))
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".mjs", delete=False, encoding="utf-8"
    ) as f:
        f.write(script)
        harness_path = f.name
    try:
        proc = subprocess.run(
            [node, "--experimental-strip-types", harness_path],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    finally:
        Path(harness_path).unlink(missing_ok=True)


def test_node_behavioral_equivalence():
    """Best-effort real-output diff — see module docstring. Skips
    (does not fail) when node/the harness isn't usable here."""
    from services.owner_service import normalize_owner, split_owners

    result = _run_node_harness()
    if result is None:
        pytest.skip(
            "node --experimental-strip-types unavailable or failed to "
            "run owner-grouping.ts here; the required regex-source lock "
            "above still covers this branch."
        )

    for raw, ts_out in zip(_SPLIT_FIXTURES, result["split"]):
        assert ts_out == split_owners(raw), (
            f"split_owners({raw!r}) drifted: TS={ts_out!r} "
            f"Python={split_owners(raw)!r}"
        )
    for raw, (ts_key, ts_display) in zip(_NORMALIZE_FIXTURES, result["normalize"]):
        py_key, py_display = normalize_owner(raw)
        assert (ts_key, ts_display) == (py_key, py_display), (
            f"normalize_owner({raw!r}) drifted: "
            f"TS=({ts_key!r}, {ts_display!r}) "
            f"Python=({py_key!r}, {py_display!r})"
        )
