"""
Re-exporting unchanged content must not touch the file.

FIELD REPORT 2026-08-21: every app install re-exported every session
into the user's Google Drive folder, so all 79 files jumped to the top
of a Date-modified sort with the current timestamp. The exports were
byte-identical; only the mtimes changed.

That is not cosmetic. "Date modified" is how someone finds the thing
they were working on, and a sync client re-uploads every file it sees
touched — so an install cost a full re-sync of the folder and destroyed
the ordering that makes it navigable.

An export writes what the session says. If the file on disk already
says exactly that, the export is already done.
"""

from __future__ import annotations

import os
import time

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from models.session import Session  # noqa: E402
from services.export_service import ExportService  # noqa: E402


def _session():
    s = Session(session_id="20260821_090000")
    s.display_name = "Acme Discovery Call"
    s.summary = "Discussed the migration timeline."
    s.action_items = "- Send the SOW"
    s.decisions = "- Go with the phased rollout"
    s.requirements = "- SSO via Okta"
    # Segments are model objects, not dicts; an empty list keeps this
    # fixture about the WRITE behaviour rather than transcript
    # rendering, which has its own tests.
    s.segments = []
    return s


@pytest.mark.parametrize("method", [
    "export_transcript", "export_summary", "export_action_items",
    "export_decisions", "export_requirements",
])
def test_re_exporting_identical_content_leaves_the_file_alone(tmp_path, method):
    svc = ExportService(str(tmp_path))
    session = _session()

    path = getattr(svc, method)(session)
    before_mtime = os.stat(path).st_mtime_ns
    before_bytes = open(path, "rb").read()

    # A different filesystem timestamp is available for the second
    # write, so an unconditional rewrite WOULD be visible.
    time.sleep(0.01)

    again = getattr(svc, method)(session)

    assert again == path
    assert open(path, "rb").read() == before_bytes
    assert os.stat(path).st_mtime_ns == before_mtime, (
        f"{method} rewrote a byte-identical file and bumped its mtime — "
        f"every install re-sorts and re-syncs the user's export folder")


def test_a_real_change_still_writes(tmp_path):
    """The skip must be content-based, not a blanket 'never rewrite'."""
    svc = ExportService(str(tmp_path))
    session = _session()

    path = svc.export_summary(session)
    time.sleep(0.01)

    session.summary = "Discussed the migration timeline and the budget."
    svc.export_summary(session)

    assert "budget" in open(path, encoding="utf-8").read()
