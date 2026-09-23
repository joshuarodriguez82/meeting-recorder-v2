"""
A summary of half a conversation must know it is half.

A meeting whose system audio never arrived is a transcript of the user
alone (field report 2026-09-15). The Sessions list now marks it — but
the summarizer, which writes the summary, action items and decisions,
was never told, and wrote them as if the user's lines were the whole
meeting: every commitment lands on the one person recorded.

The caveat rides in on the notes every extractor already receives, so
these tests check the notes that actually reach the summarizer through
the real process_full, not only the helper that builds them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from _app_import import import_app

import_app()
import server  # noqa: E402
from core.capture_health import (  # noqa: E402
    missing_system_audio_warning,
    one_sided_transcript_note,
)
from models.session import Session  # noqa: E402

WARNING = missing_system_audio_warning(
    system_configured=True, system_samples=0,
    system_open_error="'AUDCLNT_E_UNSUPPORTED_FORMAT'")


def test_no_warning_no_caveat():
    assert one_sided_transcript_note(None) == ""
    assert one_sided_transcript_note("   ") == ""


def test_the_caveat_says_what_is_missing_and_what_not_to_do():
    note = one_sided_transcript_note(WARNING)
    assert "only the user's side" in note
    assert "Do not present it as the whole discussion" in note
    assert "not the user" in note  # provenance: the app, not a user note


def test_llm_notes_on_a_real_session():
    """Uses the real Session, so a renamed field fails here rather than
    silently producing no caveat."""
    s = Session(session_id="S")
    s.notes = "Follow up on pricing."
    assert server._llm_notes(s) == "Follow up on pricing."

    s.capture_warning = WARNING
    notes = server._llm_notes(s)
    assert notes.startswith("RECORDING LIMITATION")
    assert notes.endswith("Follow up on pricing.")
    assert s.notes == "Follow up on pricing."  # stored notes untouched


def _process_full_seeing_notes(monkeypatch, capture_warning):
    settings = SimpleNamespace(is_configured=True)

    def fake_load_settings():
        server.svc.settings = settings
        return settings
    monkeypatch.setattr(server.svc, "load_settings", fake_load_settings)
    monkeypatch.setattr(server.svc, "ensure_models_loaded", lambda: None)

    session = SimpleNamespace(
        session_id="S1",
        segments=[{"speaker_id": "A", "start": 0, "end": 1, "text": "hi"}],
        notes="my note", screenshots=[], started_at=None,
        capture_warning=capture_warning, summary="",
        full_transcript=lambda: "hi",
    )
    monkeypatch.setattr(server.svc, "session_svc", SimpleNamespace(
        load_full=lambda sid: session, save=lambda s: None))
    monkeypatch.setattr(server.svc, "search_svc", None)
    monkeypatch.setattr(server.svc, "commitments_svc", None)
    monkeypatch.setattr(server.svc, "engagement_svc", None)

    seen = {}

    async def fake_summarize(transcript, prompt="", notes="", **_k):
        seen["summarize"] = notes
        return "summary"

    def markdown(name):
        async def _m(transcript, notes="", **_k):
            seen[name] = notes
            return "- item"
        return _m

    async def fake_structured(transcript, notes="", **_k):
        seen["extract_structured"] = notes
        return {}

    monkeypatch.setattr(server.svc, "summarizer", SimpleNamespace(
        summarize=fake_summarize,
        extract_action_items=markdown("extract_action_items"),
        extract_decisions=markdown("extract_decisions"),
        extract_requirements=markdown("extract_requirements"),
        extract_structured=fake_structured,
    ))
    monkeypatch.setattr(server.svc, "template_svc",
                        SimpleNamespace(get_prompt=lambda name: "prompt"))
    monkeypatch.setattr(server, "_export_after_processing", lambda sid: None)
    asyncio.run(server.process_full("S1", server.ProcessFullRequest()))
    return seen


def test_every_extractor_is_told(monkeypatch):
    seen = _process_full_seeing_notes(monkeypatch, WARNING)
    assert {"summarize", "extract_action_items", "extract_decisions",
            "extract_requirements", "extract_structured"} <= set(seen)
    for name, notes in seen.items():
        assert notes.startswith("RECORDING LIMITATION"), name
        assert "my note" in notes, name


def test_a_complete_recording_is_summarized_as_before(monkeypatch):
    seen = _process_full_seeing_notes(monkeypatch, None)
    assert seen and all(n == "my note" for n in seen.values()), seen


def test_the_caveat_changes_the_reprocessing_fingerprint():
    """Reprocessing skips the LLM when its inputs are unchanged. A
    session re-run after this ships must not be skipped as 'unchanged'
    and keep a summary written without the caveat."""
    from core.prompt_version import extraction_fingerprint
    s = Session(session_id="S")
    s.notes = "n"
    before = extraction_fingerprint("t", server._llm_notes(s), "General")
    s.capture_warning = WARNING
    after = extraction_fingerprint("t", server._llm_notes(s), "General")
    assert before != after
