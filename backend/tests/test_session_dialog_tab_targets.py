"""Every deep-link into the session dialog must name a tab that exists.

Field report 2026-08-27: clicking a row under Insights → Open Loops →
Stale Commitments opened the session dialog with a fully populated tab
strip — "Transcript (733)", "Speakers (9)" — and a COMPLETELY BLANK
body. The session had loaded fine. The caller had asked for a tab named
"commitments", the dialog has no such tab, and Radix Tabs renders no
panel when the active value matches no `TabsContent`. Two of the three
Open Loops sections did this ("commitments", "follow_ups"); the third
happened to pass a real name and worked.

This is the house defect in its purest form: a lookup that found
nothing rendering as a result that isn't there, with no error anywhere.

The frontend has no test runner yet, so this is a source-level cross
check rather than a component test — and for this particular bug that
is arguably the better shape, because the failure lives in the SEAM
between two files that no single component test would span. It reads
the tab names the dialog actually defines, reads every tab name any
caller passes, and fails when a caller names one that does not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
DIALOG = SRC / "components" / "session-detail-dialog.tsx"

# `onOpenSession(someId, "tab")` — only calls that pass an explicit tab.
_CALL_RE = re.compile(
    r"onOpenSession\(\s*[^,()]+(?:\([^()]*\))?[^,()]*,\s*[\"']([a-z_]+)[\"']")
_CONTENT_RE = re.compile(r"<TabsContent\s+value=[\"']([a-z_]+)[\"']")
_TRIGGER_RE = re.compile(r"<TabsTrigger\s+value=[\"']([a-z_]+)[\"']")


def _dialog_tabs() -> set:
    text = DIALOG.read_text(encoding="utf-8")
    return set(_CONTENT_RE.findall(text))


def _callers():
    """[(file, tab), ...] for every deep-link that names a tab."""
    out = []
    for path in sorted(SRC.rglob("*.tsx")):
        if path == DIALOG:
            continue
        for tab in _CALL_RE.findall(path.read_text(encoding="utf-8")):
            out.append((path.relative_to(SRC).as_posix(), tab))
    return out


def test_the_dialog_defines_tabs_at_all():
    """Guard the guard: if the regexes stop matching, every assertion
    below passes vacuously and this file silently stops testing."""
    tabs = _dialog_tabs()
    assert "overview" in tabs
    assert len(tabs) >= 5, tabs


def test_callers_exist_to_check():
    calls = _callers()
    assert calls, "no onOpenSession(id, tab) call sites found — regex rotted"


def test_every_deep_link_names_a_tab_that_exists():
    tabs = _dialog_tabs()
    bad = [(f, t) for f, t in _callers() if t not in tabs]
    assert not bad, (
        "these call sites open the session dialog on a tab it does not "
        f"define, which renders a blank dialog: {bad}. Valid tabs: "
        f"{sorted(tabs)}")


def test_every_tab_panel_has_a_trigger():
    """A panel with no trigger is unreachable by clicking; a trigger
    with no panel is a blank body. Both are the same bug from opposite
    ends, so pin the pairing."""
    text = DIALOG.read_text(encoding="utf-8")
    panels = set(_CONTENT_RE.findall(text))
    triggers = set(_TRIGGER_RE.findall(text))
    assert not (panels - triggers), (
        f"tab panels with no trigger: {sorted(panels - triggers)}")
    assert not (triggers - panels), (
        f"tab triggers with no panel: {sorted(triggers - panels)}")


def test_dialog_falls_back_to_overview_for_an_unknown_tab():
    """Belt to the braces above: even with every current caller correct,
    a future one passing a bad name must land on Overview rather than on
    nothing. Source-pinned because the dialog is React and there is no
    frontend runner yet."""
    text = DIALOG.read_text(encoding="utf-8")
    assert "VALID_TABS" in text, (
        "session-detail-dialog no longer normalises its initial tab; an "
        "unknown tab name will render a blank dialog again")
