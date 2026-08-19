#!/usr/bin/env python3
"""Unit tests for the CI helper scripts. No network, no API key, no git repo.

Run:  python -m pytest scripts/ci/test_ai_code_review.py -q
      python scripts/ci/test_ai_code_review.py          (unittest fallback)

These are deliberately NOT under backend/tests/ — pr-checks.yml runs
`pytest backend/tests` with a minimal dependency set, and CI tooling tests
have no business in the app's data-loss suite.
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_code_review as air  # noqa: E402
import security_baseline as sb  # noqa: E402


# ---------------------------------------------------------------------------
# stub Anthropic client
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 1234
    output_tokens = 567


class _Response:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.usage = _Usage()


class StubMessages:
    """Records the kwargs it was called with so the plumbing can be asserted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StubClient:
    def __init__(self, *responses):
        self.messages = StubMessages(responses)


class BadRequestError(Exception):
    """Mimics anthropic.BadRequestError by class name only."""


CANNED_PAYLOAD = {
    "summary": "One real issue in the calendar merge path.",
    "findings": [
        {
            "title": "Unreadable calendar renders as an empty calendar",
            "file": "backend/services/calendar_service.py",
            "line": 88,
            "severity": "high",
            "confidence": "verified",
            "category": "unreadable-vs-absent",
            "evidence": "except Exception:\n    return []",
            "explanation": "A fetch failure is indistinguishable from 'no meetings today'.",
            "why_not_already_handled": "No caller in the diff checks a separate error channel.",
        },
        {
            "title": "Fixture supplies columnDateIso that Outlook Web never sends",
            "file": "backend/tests/test_extension_calendar_merge.py",
            "line": 12,
            "severity": "medium",
            "confidence": "unverified",
            "category": "impossible-fixture",
            "evidence": '"columnDateIso": "2026-08-19"',
            "explanation": "Real payloads omit this field.",
            "why_not_already_handled": "Could not see the production scraper in this diff.",
        },
    ],
}


CANNED_DIFF = {
    "backend/services/calendar_service.py": (
        "--- a/backend/services/calendar_service.py\n"
        "+++ b/backend/services/calendar_service.py\n"
        "@@ -85,3 +85,5 @@\n"
        "+    except Exception:\n"
        "+        return []\n"
    ),
    "backend/tests/test_extension_calendar_merge.py": (
        "--- a/t\n+++ b/t\n@@ -1,1 +1,2 @@\n" + "+" * 200 + "\n"
    ),
    "package-lock.json": "should never appear\n",
}


def _fake_files(base, head):
    return [p for p in CANNED_DIFF if not air.excluded(p)]


def _fake_diff(base, head, path):
    return CANNED_DIFF[path]


def _fake_log(base, head):
    return "- abc1234 Fix calendar merge (someone)"


def make_bundle(**kw):
    return air.build_bundle(
        "base0000", "head0000",
        diff_fn=_fake_diff, log_fn=_fake_log, files_fn=_fake_files,
        **kw,
    )


# ---------------------------------------------------------------------------
# diff bounding
# ---------------------------------------------------------------------------

class TestDiffBounding(unittest.TestCase):
    def test_lockfiles_and_binaries_are_excluded(self):
        self.assertTrue(air.excluded("package-lock.json"))
        self.assertTrue(air.excluded("src-tauri/Cargo.lock"))
        self.assertTrue(air.excluded("docs/release-notes/release-notes-v2.32.0.md"))
        self.assertTrue(air.excluded("scripts/ci/baselines/bandit.json"))
        self.assertTrue(air.excluded("meeting_recorder.ico"))
        self.assertFalse(air.excluded("backend/server.py"))
        self.assertFalse(air.excluded("src/app/page.tsx"))

    def test_full_bundle_includes_everything_not_excluded(self):
        b = make_bundle()
        self.assertEqual(len(b.files_included), 2)
        self.assertNotIn("package-lock.json", b.files_included)
        self.assertFalse(b.truncated)
        self.assertFalse(b.is_empty)

    def test_budget_drops_largest_files_first_and_records_them(self):
        b = make_bundle(max_chars=150)
        # The small hand-written diff survives; the 200-byte one is dropped.
        self.assertEqual(b.files_included, ["backend/services/calendar_service.py"])
        self.assertTrue(b.truncated)
        self.assertEqual(
            [p for p, _ in b.files_dropped],
            ["backend/tests/test_extension_calendar_merge.py"],
        )

    def test_dropped_files_are_declared_to_the_model(self):
        prompt = air.build_user_prompt(make_bundle(max_chars=150))
        self.assertIn("Not shown to you", prompt)
        self.assertIn("test_extension_calendar_merge.py", prompt)
        self.assertIn("do not treat their absence as evidence", prompt)

    def test_per_file_cap_truncates_with_a_visible_marker(self):
        b = make_bundle(max_file_chars=50)
        self.assertIn("file diff truncated at 50 chars", b.text)

    def test_empty_change_set_is_detected(self):
        b = air.build_bundle(
            "a", "b",
            diff_fn=_fake_diff, log_fn=_fake_log, files_fn=lambda *_: [],
        )
        self.assertTrue(b.is_empty)


# ---------------------------------------------------------------------------
# model plumbing
# ---------------------------------------------------------------------------

class TestReviewCall(unittest.TestCase):
    def test_happy_path_parses_structured_output(self):
        client = StubClient(_Response(json.dumps(CANNED_PAYLOAD)))
        data = air.review(client, make_bundle(), "claude-sonnet-5")
        self.assertEqual(len(data["findings"]), 2)
        self.assertEqual(data["_usage"]["input_tokens"], 1234)

    def test_request_carries_schema_thinking_and_effort(self):
        client = StubClient(_Response(json.dumps(CANNED_PAYLOAD)))
        air.review(client, make_bundle(), "claude-haiku-4-5", effort="low")
        kw = client.messages.calls[0]
        self.assertEqual(kw["model"], "claude-haiku-4-5")
        self.assertEqual(kw["thinking"], {"type": "adaptive"})
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertIn("require_backend_token", kw["system"])
        self.assertIn("columnDateIso", kw["system"])
        self.assertIn("not-yet-knowable", kw["system"])

    def test_falls_back_to_text_mode_when_schema_is_rejected(self):
        client = StubClient(
            BadRequestError("output_config.format unsupported"),
            _Response("Here you go:\n```json\n" + json.dumps(CANNED_PAYLOAD) + "\n```"),
        )
        data = air.review(client, make_bundle(), "claude-sonnet-5")
        self.assertEqual(len(data["findings"]), 2)
        self.assertEqual(len(client.messages.calls), 2)
        self.assertNotIn("output_config", client.messages.calls[1])

    def test_unexpected_error_is_not_swallowed_by_the_fallback(self):
        client = StubClient(RuntimeError("connection reset"))
        with self.assertRaises(RuntimeError):
            air.review(client, make_bundle(), "claude-sonnet-5")

    def test_refusal_is_an_error_not_an_empty_result(self):
        client = StubClient(_Response("{}", stop_reason="refusal"))
        with self.assertRaises(RuntimeError):
            air.review(client, make_bundle(), "claude-sonnet-5")

    def test_empty_response_is_an_error_not_zero_findings(self):
        client = StubClient(_Response("   "))
        with self.assertRaises(RuntimeError):
            air.review(client, make_bundle(), "claude-sonnet-5")

    def test_bare_json_without_fences_still_parses(self):
        payload = json.dumps({"findings": [], "summary": "clean"})
        self.assertEqual(air._extract_json("noise " + payload + " trailer")["summary"], "clean")


# ---------------------------------------------------------------------------
# report rendering
# ---------------------------------------------------------------------------

class TestReport(unittest.TestCase):
    def test_report_orders_by_severity_and_flags_unverified(self):
        md = air.render_report(make_bundle(), dict(CANNED_PAYLOAD), "claude-sonnet-5")
        self.assertLess(md.index("Unreadable calendar"), md.index("Fixture supplies"))
        self.assertIn("_(unverified", md)
        self.assertIn("backend/services/calendar_service.py:88", md)
        self.assertIn("Nothing here modifies", md)

    def test_partial_review_says_so(self):
        md = air.render_report(make_bundle(max_chars=150), dict(CANNED_PAYLOAD), "m")
        self.assertIn("This review is partial", md)
        self.assertIn("not reviewed", md)

    def test_no_findings_renders_cleanly(self):
        md = air.render_report(make_bundle(), {"findings": [], "summary": "clean"}, "m")
        self.assertIn("No findings met the reporting bar", md)


# ---------------------------------------------------------------------------
# graceful degradation
# ---------------------------------------------------------------------------

class TestMainDegradation(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.result = str(Path(self.tmp.name) / "result.json")
        self.out = str(Path(self.tmp.name) / "review.md")
        self.summary = str(Path(self.tmp.name) / "summary.md")

    def _run(self, env, argv_extra=()):
        import os
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update({k: v for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
        try:
            return air.main([
                "--result", self.result, "--out", self.out,
                "--summary", self.summary, *argv_extra,
            ])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_missing_api_key_skips_without_failing(self):
        rc = self._run({"ANTHROPIC_API_KEY": None})
        self.assertEqual(rc, 0)
        result = json.loads(Path(self.result).read_text())
        self.assertEqual(result["status"], "skipped-no-key")
        self.assertEqual(result["findings"], 0)
        self.assertIn("ANTHROPIC_API_KEY", Path(self.summary).read_text())
        self.assertFalse(Path(self.out).exists(), "no report should be written")


# ---------------------------------------------------------------------------
# security baseline
# ---------------------------------------------------------------------------

BANDIT_REPORT = {
    "results": [
        {"test_id": "B324", "filename": "backend/a.py", "line_number": 10,
         "issue_severity": "HIGH", "issue_text": "weak sha1", "code": "10 h = hashlib.sha1()\n"},
        {"test_id": "B110", "filename": "backend/b.py", "line_number": 4,
         "issue_severity": "LOW", "issue_text": "try/except/pass", "code": "4 pass\n"},
    ]
}


class TestSecurityBaseline(unittest.TestCase):
    def _write(self, name, data, ndjson=False):
        import tempfile
        d = getattr(self, "_dir", None)
        if d is None:
            d = self._dir = tempfile.TemporaryDirectory()
            self.addCleanup(d.cleanup)
        p = Path(d.name) / name
        if ndjson:
            p.write_text("\n".join(json.dumps(x) for x in data))
        else:
            p.write_text(json.dumps(data))
        return p

    def test_fingerprint_ignores_line_numbers(self):
        shifted = json.loads(json.dumps(BANDIT_REPORT))
        shifted["results"][0]["line_number"] = 210
        shifted["results"][0]["code"] = "210 h = hashlib.sha1()\n"
        a = sb.load_report(self._write("a.json", BANDIT_REPORT), "bandit")
        b = sb.load_report(self._write("b.json", shifted), "bandit")
        self.assertEqual(a[0].fingerprint, b[0].fingerprint)

    def test_fingerprint_changes_with_rule_file_or_code(self):
        base = sb.load_report(self._write("c.json", BANDIT_REPORT), "bandit")[0].fingerprint
        moved = json.loads(json.dumps(BANDIT_REPORT))
        moved["results"][0]["filename"] = "backend/other.py"
        other = sb.load_report(self._write("d.json", moved), "bandit")[0].fingerprint
        self.assertNotEqual(base, other)

    def test_duplicates_get_distinct_occurrence_fingerprints(self):
        dup = {"results": [BANDIT_REPORT["results"][0],
                           dict(BANDIT_REPORT["results"][0], line_number=99)]}
        found = sb.load_report(self._write("e.json", dup), "bandit")
        self.assertEqual(len({f.fingerprint for f in found}), 2,
                         "a second identical finding must not be silently absorbed")

    def test_check_passes_on_baselined_tree_and_fails_on_new(self):
        report = self._write("f.json", BANDIT_REPORT)
        baseline = Path(self._dir.name) / "baseline.json"
        sb.main(["update", "--tool", "bandit", "--report", str(report),
                 "--baseline", str(baseline)])
        self.assertEqual(
            sb.main(["check", "--tool", "bandit", "--report", str(report),
                     "--baseline", str(baseline)]), 0)

        grown = json.loads(json.dumps(BANDIT_REPORT))
        grown["results"].append(
            {"test_id": "B602", "filename": "backend/c.py", "line_number": 1,
             "issue_severity": "HIGH", "issue_text": "shell=True",
             "code": "1 subprocess.call(x, shell=True)\n"})
        self.assertEqual(
            sb.main(["check", "--tool", "bandit",
                     "--report", str(self._write("g.json", grown)),
                     "--baseline", str(baseline)]), 1)

    def test_missing_baseline_treats_everything_as_new_and_says_so(self):
        report = self._write("h.json", BANDIT_REPORT)
        missing = Path(self._dir.name) / "nope.json"
        rc = sb.main(["check", "--tool", "bandit", "--report", str(report),
                      "--baseline", str(missing)])
        self.assertEqual(rc, 1)

    def test_clippy_ndjson_parses_and_skips_dependency_warnings(self):
        entries = [
            {"reason": "compiler-message", "message": {
                "level": "warning", "code": {"code": "clippy::manual_find"},
                "message": "manual implementation of `Iterator::find`",
                "spans": [{"is_primary": True, "file_name": "src/lib.rs",
                           "line_start": 354, "text": [{"text": "for x in y {"}]}]}},
            {"reason": "compiler-message", "message": {
                "level": "warning", "code": {"code": "dead_code"},
                "message": "in a dependency",
                "spans": [{"is_primary": True,
                           "file_name": "/root/.cargo/registry/src/foo/lib.rs",
                           "line_start": 9, "text": [{"text": "fn f() {}"}]}]}},
            {"reason": "compiler-artifact", "message": None},
        ]
        found = sb.load_report(self._write("i.json", entries, ndjson=True), "clippy")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["file"], "src-tauri/src/lib.rs")
        self.assertEqual(found[0]["rule"], "clippy::manual_find")

    def test_semgrep_report_parses(self):
        report = {"results": [{
            "check_id": "python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1",
            "path": "backend/services/document_service.py",
            "start": {"line": 114},
            "extra": {"severity": "WARNING", "message": "SHA1 is insecure",
                      "lines": "    h = hashlib.sha1()"},
        }]}
        found = sb.load_report(self._write("j.json", report), "semgrep")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["line"], 114)
        self.assertEqual(found[0]["severity"], "WARNING")

    def test_unreadable_report_is_an_error_not_an_empty_scan(self):
        bad = Path(self._dir.name if hasattr(self, "_dir") else ".") / "missing.json"
        with self.assertRaises(SystemExit):
            sb.load_report(Path("/nonexistent/report.json"), "bandit")
        del bad


if __name__ == "__main__":
    unittest.main(verbosity=2)
