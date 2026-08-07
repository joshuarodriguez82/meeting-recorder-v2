"""Tests for Designated Folder reconciliation.

Guards the 2026-08-07 field report: meetings tagged to a client never
reached the client's Google Drive folder because nothing enqueued an
export for them, and nothing ever noticed the gap.
"""

from types import SimpleNamespace

import pytest

from services.export_reconcile import (
    base_name,
    expected_artifacts,
    missing_artifacts,
    needs_export,
    normalize_client,
    sessions_for_client,
)


def _session(**kw):
    base = dict(
        session_id="ABC123",
        display_name="",
        client="",
        segments=None,
        summary="",
        action_items="",
        decisions="",
        requirements="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _summary(**kw):
    base = dict(
        session_id="ABC123",
        display_name="Session ABC123",
        client="",
        has_transcript=False,
        has_summary=False,
        has_action_items=False,
        has_decisions=False,
        has_requirements=False,
    )
    base.update(kw)
    return base


# ── filename prediction ───────────────────────────────────────────────

def test_base_name_matches_export_service():
    """Reconciliation predicts filenames; ExportService writes them. If
    these two ever drift, every file looks permanently missing and the
    worker re-exports forever. Lock them together."""
    from services.export_service import ExportService

    svc = ExportService.__new__(ExportService)  # no __init__: avoid mkdir
    for display, sid in (
        ("Quarterly Review", "ABC123"),
        ("AWS ZORG Italy Call 08032026", "DEF456"),
        ("weird/name:with*chars", "GHI789"),
        ("", "JKL012"),
        ("!!!", "MNO345"),  # sanitizes to empty -> falls back to id
    ):
        sess = _session(session_id=sid, display_name=display)
        assert base_name(display, sid) == svc._base_name(sess), display


def test_untitled_session_uses_session_prefix():
    assert base_name("", "ABC123") == "session_ABC123"


def test_summary_placeholder_display_name_is_undone():
    """list_sessions() substitutes "Session <id>" for an empty
    display_name, but the exporter stemmed the file "session_<id>".
    Reconciliation must predict the latter or untitled meetings look
    permanently unmirrored."""
    row = _summary(has_transcript=True)  # display_name = "Session ABC123"
    assert expected_artifacts(row) == ["transcript_session_ABC123.txt"]


# ── what a session owes ───────────────────────────────────────────────

def test_unprocessed_session_owes_nothing():
    assert expected_artifacts(_session()) == []
    assert missing_artifacts(_session(), "/some/folder") == []


def test_only_present_artifacts_are_expected():
    sess = _session(display_name="Review", segments=[{"text": "hi"}],
                    summary="a summary")
    assert expected_artifacts(sess) == [
        "transcript_Review.txt", "summary_Review.txt",
    ]


def test_all_five_artifacts():
    sess = _session(
        display_name="Big", segments=[1], summary="s",
        action_items="a", decisions="d", requirements="r")
    assert expected_artifacts(sess) == [
        "transcript_Big.txt", "summary_Big.txt", "action_items_Big.txt",
        "decisions_Big.txt", "requirements_Big.txt",
    ]


def test_object_and_dict_shapes_agree():
    """Reconciling a client walks cheap summary dicts; single-session
    callers hold real Session objects. Both must predict identically."""
    obj = _session(display_name="Review", segments=[1], summary="s")
    row = _summary(display_name="Review", has_transcript=True,
                   has_summary=True)
    assert expected_artifacts(obj) == expected_artifacts(row)


# ── disk comparison ───────────────────────────────────────────────────

def test_missing_detected_and_cleared(tmp_path):
    sess = _session(display_name="Review", segments=[1], summary="s")
    assert needs_export(sess, str(tmp_path))
    assert missing_artifacts(sess, str(tmp_path)) == [
        "transcript_Review.txt", "summary_Review.txt",
    ]

    (tmp_path / "transcript_Review.txt").write_text("t")
    assert missing_artifacts(sess, str(tmp_path)) == ["summary_Review.txt"]

    (tmp_path / "summary_Review.txt").write_text("s")
    assert missing_artifacts(sess, str(tmp_path)) == []
    assert not needs_export(sess, str(tmp_path))


def test_absent_folder_counts_everything_missing(tmp_path):
    """Drive offline / path typo / disk unplugged must read as 'all
    outstanding', never 'all present' — otherwise a disconnected mount
    reports a client fully mirrored when nothing was written."""
    sess = _session(display_name="Review", segments=[1])
    gone = str(tmp_path / "not-there")
    assert missing_artifacts(sess, gone) == ["transcript_Review.txt"]


def test_no_folder_configured_owes_nothing():
    sess = _session(display_name="Review", segments=[1])
    assert missing_artifacts(sess, None) == []
    assert missing_artifacts(sess, "") == []


# ── client matching (the case-sensitivity split) ──────────────────────

@pytest.mark.parametrize("a,b", [
    ("Zorg", "ZORG"), ("Zorg", "[scrubbed]"), ("Zorg", " Zorg "), ("TYR", "ups"),
])
def test_client_matching_is_case_and_space_insensitive(a, b):
    assert normalize_client(a) == normalize_client(b)


def test_sessions_for_client_matches_regardless_of_casing():
    """The exact bug behind the field report: export resolved the folder
    case-insensitively (so files landed in G:\\My Drive\\Zorg) while the
    UI filtered `s.client === "Zorg"` case-sensitively (so those meetings
    were invisible under Zorg)."""
    rows = [
        _summary(session_id="1", client="Zorg"),
        _summary(session_id="2", client="ZORG"),
        _summary(session_id="3", client="[scrubbed] "),
        _summary(session_id="4", client="Acme"),
        _summary(session_id="5", client=""),
    ]
    got = {r["session_id"] for r in sessions_for_client(rows, "Zorg")}
    assert got == {"1", "2", "3"}


def test_sessions_for_client_empty_name_matches_nothing():
    rows = [_summary(session_id="1", client="Zorg"),
            _summary(session_id="2", client="")]
    assert sessions_for_client(rows, "") == []
    assert sessions_for_client(rows, "   ") == []
