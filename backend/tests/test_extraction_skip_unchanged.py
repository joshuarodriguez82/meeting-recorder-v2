"""Reprocessing a session whose inputs never moved must not pay for it.

The August 2026 token export is the reason this exists: 5.5M input
tokens in a month, with the three reprocessing days running 4-8x a
normal day. Nearly all of that was five extractors regenerating text
that was byte-identical to what was already on disk, because
``/process_full`` had no notion of "nothing changed" — it re-ran
unconditionally.

``core.prompt_version.extraction_fingerprint`` answers the only
question that matters — *would re-running produce different text* — from
the transcript, the notes, the template and the prompt version. The
guard in ``process_full`` skips the five calls when that fingerprint
matches what the last successful run stamped.

The properties pinned here are the ones where a wrong answer is
expensive or silent:

  - a skip must actually skip (no LLM call), not just report one;
  - anything that moves the output — transcript, notes, template, the
    prompt version itself — must defeat the skip;
  - ``force=True`` must always defeat it, because that is the only
    escape hatch when the cached output is wrong for a reason the
    fingerprint cannot see;
  - a session that has never produced a summary must never skip, even
    with a matching stamp — the stamp without the output is the "a
    result you couldn't read rendering as a result that isn't there"
    failure this repo keeps re-learning;
  - a PARTIAL run must not stamp. If one extractor is rate-limited and
    the other four succeed, stamping would make the next reprocess skip
    the one session that still needs finishing, and the skip would be
    indistinguishable from success.

All LLM deps are stubbed — this exercises the guard, not the pipeline.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from _app_import import import_app

import_app()  # sets MEETING_RECORDER_SKIP_DEP_REPAIR + stubs BEFORE server
import server  # noqa: E402

from core.prompt_version import extraction_fingerprint  # noqa: E402


def _install(monkeypatch, *, transcript="hello there", notes="",
             fingerprint="", summary="", fail=None):
    """Wire server.svc so process_full reaches the extraction stage with
    no real deps. Returns (session, calls) — `calls` counts how many
    times each extractor actually ran, which is the only honest way to
    tell a skip from a re-run that happened to produce the same text."""
    settings = SimpleNamespace(
        is_configured=True,
        anthropic_api_key="sk-EXAMPLE",  # nosec B106 - synthetic fixture
        hf_token="hf-EXAMPLE")  # nosec B106 - synthetic fixture

    def fake_load_settings():
        server.svc.settings = settings
        return settings
    monkeypatch.setattr(server.svc, "load_settings", fake_load_settings)
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)

    session = SimpleNamespace(
        session_id="S1",
        segments=[{"speaker_id": "A", "start": 0, "end": 1,
                   "text": transcript}],
        notes=notes, screenshots=[], started_at=None,
        summary=summary,
        extraction_fingerprint=fingerprint,
        full_transcript=lambda: transcript,
    )
    saved = []
    monkeypatch.setattr(server.svc, "session_svc", SimpleNamespace(
        load_full=lambda sid: session, save=lambda s: saved.append(s)))
    monkeypatch.setattr(server.svc, "search_svc", None)
    monkeypatch.setattr(server.svc, "commitments_svc", None)
    monkeypatch.setattr(server.svc, "engagement_svc", None)

    calls = {"summarize": 0, "action_items": 0, "decisions": 0,
             "requirements": 0, "structured": 0}

    def _counter(key, result, boom=False):
        async def _fn(*_a, **_k):
            calls[key] += 1
            if boom:
                raise RuntimeError("Claude 500")
            return result
        return _fn

    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace(
        summarize=_counter("summarize", "summary text", fail == "summarize"),
        extract_action_items=_counter(
            "action_items", "- item", fail == "action_items"),
        extract_decisions=_counter(
            "decisions", "- item", fail == "decisions"),
        extract_requirements=_counter(
            "requirements", "- item", fail == "requirements"),
        extract_structured=_counter("structured", {}, fail == "structured"),
    ))
    monkeypatch.setattr(server.svc, "template_svc",
                        SimpleNamespace(get_prompt=lambda name: "prompt"))
    session._saved = saved
    return session, calls


def _run(**kw):
    return asyncio.run(server.process_full("S1", server.ProcessFullRequest(**kw)))


# ── The skip itself ──────────────────────────────────────────────────

def test_unchanged_inputs_skip_every_extractor(monkeypatch):
    """The whole point: zero LLM calls, and the response says so."""
    fp = extraction_fingerprint("hello there", "", "default")
    _s, calls = _install(monkeypatch, fingerprint=fp, summary="old summary")
    result = _run(template="default")
    assert result["skipped"] is True
    assert result["stages"]["extract"].startswith("skipped")
    assert sum(calls.values()) == 0, calls


def test_first_run_never_skips(monkeypatch):
    """No stamp yet — there is nothing to compare against, so run."""
    _s, calls = _install(monkeypatch, fingerprint="", summary="")
    result = _run(template="default")
    assert result.get("skipped") is not True
    assert calls["summarize"] == 1


def test_a_stamp_without_a_summary_never_skips(monkeypatch):
    """A stamp claiming "done" over an empty summary is the stamp being
    wrong, not the work being done. Re-run rather than serve nothing."""
    fp = extraction_fingerprint("hello there", "", "default")
    _s, calls = _install(monkeypatch, fingerprint=fp, summary="")
    result = _run(template="default")
    assert result.get("skipped") is not True
    assert calls["summarize"] == 1


# ── What must defeat the skip ────────────────────────────────────────

def test_a_changed_transcript_defeats_the_skip(monkeypatch):
    fp = extraction_fingerprint("the OLD transcript", "", "default")
    _s, calls = _install(monkeypatch, transcript="a re-diarized transcript",
                         fingerprint=fp, summary="old summary")
    result = _run(template="default")
    assert result.get("skipped") is not True
    assert calls["summarize"] == 1


def test_changed_notes_defeat_the_skip(monkeypatch):
    """Editing the notes and reprocessing is a real user action — it is
    how you correct an extraction — so it cannot silently no-op."""
    fp = extraction_fingerprint("hello there", "", "default")
    _s, calls = _install(monkeypatch, notes="actually it was Webex",
                         fingerprint=fp, summary="old summary")
    result = _run(template="default")
    assert result.get("skipped") is not True
    assert calls["summarize"] == 1


def test_a_different_template_defeats_the_skip(monkeypatch):
    fp = extraction_fingerprint("hello there", "", "default")
    _s, calls = _install(monkeypatch, fingerprint=fp, summary="old summary")
    result = _run(template="discovery")
    assert result.get("skipped") is not True
    assert calls["summarize"] == 1


def test_a_prompt_version_bump_defeats_the_skip(monkeypatch):
    """The fingerprint carries EXTRACTOR_PROMPT_VERSION, so tightening a
    prompt (the decisions over-extraction fix) still forces the re-run
    it needs — the guard makes reprocessing cheap, not impossible."""
    import core.prompt_version as pv
    stale = extraction_fingerprint("hello there", "", "default")
    monkeypatch.setattr(pv, "EXTRACTOR_PROMPT_VERSION", "2099-01-01.9")
    _s, calls = _install(monkeypatch, fingerprint=stale, summary="old summary")
    result = _run(template="default")
    assert result.get("skipped") is not True
    assert calls["summarize"] == 1


def test_force_defeats_the_skip(monkeypatch):
    """The escape hatch for everything the fingerprint cannot see."""
    fp = extraction_fingerprint("hello there", "", "default")
    _s, calls = _install(monkeypatch, fingerprint=fp, summary="old summary")
    result = _run(template="default", force=True)
    assert result.get("skipped") is not True
    assert calls["summarize"] == 1


# ── Stamping ─────────────────────────────────────────────────────────

def test_a_successful_run_stamps_so_the_next_one_skips(monkeypatch):
    session, calls = _install(monkeypatch)
    _run(template="default")
    assert session.extraction_fingerprint == extraction_fingerprint(
        "hello there", "", "default")
    assert calls["summarize"] == 1

    # Second identical run over the same (now-stamped) session.
    for k in calls:
        calls[k] = 0
    result = _run(template="default")
    assert result["skipped"] is True
    assert sum(calls.values()) == 0, calls


def test_a_partial_failure_does_not_stamp(monkeypatch):
    """One extractor rate-limited, four fine. Stamping here would make
    the next reprocess skip the one session that still needs work — and
    the skip would look exactly like success."""
    session, _calls = _install(monkeypatch, fingerprint="stale",
                               fail="decisions")
    result = _run(template="default")
    assert result["stages"]["decisions"].startswith("failed")
    assert session.extraction_fingerprint == ""


def test_a_failed_summary_does_not_stamp(monkeypatch):
    """The summary call is the one moved out of the gather to warm the
    cache; its failure has to stay best-effort AND stay unstamped."""
    session, calls = _install(monkeypatch, fail="summarize")
    result = _run(template="default")
    assert result["ok"] is True
    assert result["stages"]["summary"].startswith("failed")
    # Best-effort preserved: the other four still ran.
    assert calls["action_items"] == 1 and calls["structured"] == 1
    assert session.extraction_fingerprint == ""
