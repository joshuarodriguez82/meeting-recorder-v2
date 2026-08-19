"""
Provenance rules in generated summaries.

Two verified fabrications in one 43-minute recording motivated this
file. Both were reproducible from prompt construction alone, so that is
what these tests pin: the literal instruction text the builders emit.
No model is ever called — `_chat` is monkeypatched to capture the
prompt, which is the artifact under test. (Same pattern as
test_prep_brief_documents.py.)

DEFECT 1 — an invented year.
    The transcript said "come October" once, with no year anywhere in
    the call. The summary asserted a year two years in the PAST, twice,
    once as a section heading. Nothing in the prompt carried the
    meeting's own date, so the model had no anchor for the relative
    reference and supplied a year from nowhere. A deadline six weeks
    out was reported as an event long since gone by, in a summary that
    gets forwarded to the customer.

DEFECT 2 — co-pilot speculation presented as meeting content.
    The live co-pilot generates clarifying questions and risk flags
    DURING the call: those are the model's own speculation, not things
    anyone said. The summarizer prompt told the model to "prefer the
    co-pilot's phrasing if it's clearer than the transcript evidence" —
    which is how invented specificity reaches a summary. The result
    listed four "open routing questions raised by co-pilot"; exactly
    one matched something a participant actually asked.

The principle both halves serve: every factual claim in the body of a
summary must be traceable to the transcript. Co-pilot output may direct
attention; it must not become content.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

# summarizer.py imports the anthropic SDK at module load and
# config.settings imports python-dotenv; neither is in the lightweight
# test env. Same stub pattern as test_coach_tick.py.
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core.summarizer import Summarizer  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _capturing() -> tuple[Summarizer, list[str]]:
    s = Summarizer(api_key="x", model="claude-haiku-4-5", provider="anthropic")
    prompts: list[str] = []

    async def _fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return "## Summary\n- body"

    s._chat = _fake_chat  # type: ignore[assignment]
    return s, prompts


# The field case, anonymised: one bare month, no year stated anywhere,
# in a meeting that happened in August 2026. "Come October" therefore
# means October 2026 — and any other year is a fabrication.
MEETING_DATE = "2026-08-19T14:03:11.412000"
BARE_MONTH_TRANSCRIPT = (
    "[00:00 → 00:07] Jane Doe: We're at forty-two agents on the Acme "
    "queue today.\n"
    "[00:07 → 00:14] Richard Roe: Come October that number will move — "
    "we're onboarding the Globex desk.\n"
    "[00:14 → 00:20] Jane Doe: Understood. I'll take the sizing away.\n"
)


# ── Defect 1: the date anchor ───────────────────────────────────────


def test_bare_month_transcript_carries_the_meeting_date():
    """The transcript says 'come October' and states no year. The
    prompt must carry the meeting's own date so the model has something
    to resolve that against."""
    s, prompts = _capturing()
    _run(s.summarize(BARE_MONTH_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert "=== DATE ANCHOR" in prompt
    # Full date AND weekday: "next Tuesday" is unresolvable without the
    # weekday, and the ISO form removes any ambiguity about the rest.
    assert "This meeting took place on Wednesday, 19 August 2026 " \
           "(2026-08-19)." in prompt


def test_no_year_absent_from_the_transcript_is_offered_to_the_model():
    """The specific regression. 2024 appeared twice in the real summary
    — once as a section heading — and appears nowhere in the source.
    The only year the prompt puts in front of the model is the
    meeting's own."""
    s, prompts = _capturing()
    _run(s.summarize(BARE_MONTH_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert "2024" not in prompt
    assert "October" in prompt          # the source reference survives
    assert "October 2024" not in prompt


def test_anchor_forbids_dates_neither_stated_nor_derivable():
    """Resolution alone would invite the model to pin down dates it is
    guessing at. The prohibition is what stops the invention, and it has
    to name headings explicitly — the real fabrication was a heading."""
    s, prompts = _capturing()
    _run(s.summarize(BARE_MONTH_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert ("NEVER state a year, month or day that is not either stated "
            "explicitly in the source material or derived from the anchor "
            "date above.") in prompt
    assert ('- A bare month with no year stays bare, or is resolved '
            'forward from the anchor. "Come October" becomes "October" or '
            "the October on or after the anchor date — never a different "
            "year.") in prompt
    assert ("- Do not invent a date for an undated commitment.") in prompt
    # The real fabrication was a section heading, so the rule has to say
    # headings out loud.
    assert ("- This applies to section headings and table cells exactly "
            "as it applies to prose.") in prompt
    assert "An unqualified date is correct." in prompt


def test_relative_references_resolve_forward_never_backward():
    """October 2024 is 'October' resolved BACKWARD from an August 2026
    meeting. The direction has to be stated."""
    s, prompts = _capturing()
    _run(s.summarize(BARE_MONTH_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    assert "on or after the anchor date above — never an earlier one." \
        in prompts[0]


@pytest.mark.parametrize("method", [
    "extract_action_items", "extract_decisions", "extract_requirements",
])
def test_markdown_extractors_are_anchored_too(method):
    """A due date is exactly where an invented year lands, so every
    transcript-to-prompt path gets the same anchor — not just the
    summary."""
    s, prompts = _capturing()
    _run(getattr(s, method)(BARE_MONTH_TRANSCRIPT,
                            meeting_date=MEETING_DATE))
    assert "=== DATE ANCHOR" in prompts[0]
    assert "19 August 2026" in prompts[0]
    assert "2024" not in prompts[0]


def test_structured_extractor_is_anchored_and_stays_json_only():
    """extract_structured has no prose to say 'timing unspecified' in —
    its unknown-deadline answer is "". The anchor must not talk it out
    of emitting bare JSON."""
    s, prompts = _capturing()

    async def _json_chat(prompt, **kwargs):
        prompts.append(prompt)
        return '{"requirements":[],"decisions":[],"action_items":[],' \
               '"open_questions":[]}'

    s._chat = _json_chat  # type: ignore[assignment]
    _run(s.extract_structured(BARE_MONTH_TRANSCRIPT,
                              meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert "=== DATE ANCHOR" in prompt
    assert '"due", an unstated or unclear deadline is ""' in prompt
    assert "never a guessed date" in prompt
    assert "Still output nothing but the JSON object." in prompt


def test_prep_briefs_anchor_on_today():
    """A brief is written NOW about a meeting in the FUTURE, so today is
    what a relative reference in the prior notes resolves against."""
    s, prompts = _capturing()
    _run(s.meeting_prep_brief("### Kickoff (2026-01-01)\nnotes",
                              "Cutover planning",
                              today_iso="2026-08-19"))
    assert "Today's date is Wednesday, 19 August 2026 (2026-08-19)." \
        in prompts[0]

    s2, prompts2 = _capturing()
    _run(s2.meeting_prep_brief_from_calendar(
        upcoming_subject="Cutover planning",
        upcoming_attendees=["dana.doe@globex.example"],
        upcoming_when="Monday Sep 07 at 10:00 AM",
        identified_client="Globex",
        identified_project="Phase 2",
        prior_notes="### [S1] Kickoff  (2026-01-01)\nnotes",
        today_iso="2026-08-19",
    ))
    assert "Today's date is Wednesday, 19 August 2026 (2026-08-19)." \
        in prompts2[0]


def test_daily_briefing_already_anchored_and_resolves_forward():
    """parse_daily_briefing already carried today's date. It now also
    carries the never-invent rule, in the same direction as everywhere
    else."""
    s, prompts = _capturing()
    _run(s.parse_daily_briefing("9:30 standup with Jane Doe",
                                today_iso="2026-08-19"))
    prompt = prompts[0]
    assert "TODAY'S DATE IS 2026-08-19" in prompt
    assert ("Never state a year, month or day that is not either in the "
            "briefing text or derived from today's date above.") in prompt
    assert "resolves FORWARD from today's date, never to an earlier one." \
        in prompt


# ── The anchor is optional and never fires on a bad date ────────────


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_missing_date_emits_the_unanchored_prompt(bad):
    """No date is a real state (legacy sessions, imports). Anchoring on
    a date we don't have would be worse than not anchoring: the caller
    passes through whatever it has and gets the pre-anchor prompt."""
    s, prompts = _capturing()
    _run(s.summarize("[00:00 → 00:03] Jane Doe: hello",
                     prompt="Summarize.", meeting_date=bad or ""))
    assert "DATE ANCHOR" not in prompts[0]

    baseline, base_prompts = _capturing()
    _run(baseline.summarize("[00:00 → 00:03] Jane Doe: hello",
                            prompt="Summarize."))
    assert prompts[0] == base_prompts[0]


def test_unparseable_date_is_passed_through_not_dropped():
    """A fuzzy anchor still beats none — but it must not crash or
    silently vanish."""
    s, prompts = _capturing()
    _run(s.summarize("[00:00 → 00:03] Jane Doe: hello",
                     prompt="Summarize.", meeting_date="last Thursday"))
    assert "This meeting took place on last Thursday." in prompts[0]


# ── Defect 2: co-pilot output is a pointer, not a source ────────────


COPILOT_BLOB = (
    "Questions the AI co-pilot suggested asking "
    "(generated, not asked by anyone):\n"
    "  • Confirm whether the Globex desk routes through the same queue\n"
    "Risks the AI co-pilot generated (not raised by anyone):\n"
    "  • Agent headcount may not absorb the Globex volume\n"
)


def _copilot_prompt() -> str:
    s, prompts = _capturing()
    _run(s.summarize(BARE_MONTH_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE,
                     copilot_observations=COPILOT_BLOB))
    return prompts[0]


def test_prefer_the_copilots_phrasing_instruction_is_gone():
    """The exact instruction that laundered speculation into record.
    'Do not invent details' was inert against it — the invention
    happened upstream, in the co-pilot, and this told the summarizer to
    copy it out."""
    prompt = _copilot_prompt()
    assert "prefer the co-pilot's phrasing" not in prompt
    assert "prefer the co-pilot" not in prompt.lower()
    assert "co-pilot's phrasing" not in prompt
    assert "corroborating second pass" not in prompt
    # The whole preference direction is reversed now, not just deleted.
    assert "never the co-pilot's" in prompt


def test_copilot_lines_are_labelled_as_generated_not_as_said():
    """The framing the reader inherits. The old blob header said
    'Clarifying questions raised', which reads as though a participant
    raised them."""
    prompt = _copilot_prompt()
    assert "THAT MODEL'S OWN SUGGESTIONS" in prompt
    assert "They are NOT a record of what anyone said." in prompt
    assert "No participant necessarily raised any of them" in prompt
    assert ("=== AI CO-PILOT SUGGESTIONS (machine-generated during the "
            "call — NOT meeting content, NOT said by anyone) ===") in prompt
    assert "CO-PILOT IN-MEETING OBSERVATIONS" not in prompt


def test_unsupported_copilot_item_is_barred_from_meeting_content():
    """Three of the four 'open routing questions raised by co-pilot' in
    the real summary appear nowhere in the transcript. Nothing that
    survives on co-pilot authority alone may sit in a section that
    reads as a record of what happened."""
    prompt = _copilot_prompt()
    assert ("A co-pilot line the transcript does NOT support must not "
            "appear anywhere that reads as a record of the meeting") in prompt
    # Named section by section, because "the summary" is too vague to
    # bind: the real fabrications landed in an open-questions list.
    for section in ("not in the narrative", "not among decisions",
                    "not among questions raised", "not among action items"):
        assert section in prompt
    assert ("Never write that the co-pilot's suggestions were 'raised', "
            "'asked' or 'discussed' in the meeting.") in prompt


def test_unsupported_items_are_quarantined_under_a_labelled_heading():
    """Chosen over silent omission: the co-pilot exists because it
    catches things, and a suppressed-but-real risk helps nobody. The
    label has to survive a forward, so it names both the author (AI) and
    the negative (not raised) in the heading itself — not in a footnote
    a forwarding reader will crop."""
    prompt = _copilot_prompt()
    assert ("## Suggested follow-ups (AI-generated — not raised in the "
            "meeting)") in prompt
    assert "at the very END of the summary" in prompt
    assert "Never attribute one to a participant." in prompt
    # And it must not be allowed to pad itself into existence.
    assert "Omit the whole section if nothing qualifies; never pad it." \
        in prompt


def test_supported_copilot_item_is_written_in_transcript_wording():
    """When the transcript does back a co-pilot line, the line may point
    at it — but the words that reach the reader are the participants'.
    That is what stops the co-pilot's invented specificity (a system
    name, a number, a date) riding in on a genuine item."""
    prompt = _copilot_prompt()
    assert ("A co-pilot line may inform the body of the summary ONLY where "
            "the transcript independently supports it.") in prompt
    assert "WRITE IT IN THE TRANSCRIPT'S OWN WORDS" in prompt
    assert "Follow the participants' phrasing, never the co-pilot's." in prompt
    assert ("Never carry over a name, number, system, date or specific "
            "that appears only in a co-pilot line.") in prompt


def test_copilot_block_is_absent_when_there_are_no_ticks():
    """A meeting with the co-pilot off must emit the prompt it always
    did — no empty header, no dangling rules."""
    s, prompts = _capturing()
    _run(s.summarize(BARE_MONTH_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE, copilot_observations="   \n "))
    assert "CO-PILOT" not in prompts[0]

    baseline, base_prompts = _capturing()
    _run(baseline.summarize(BARE_MONTH_TRANSCRIPT, prompt="Summarize.",
                            meeting_date=MEETING_DATE))
    assert prompts[0] == base_prompts[0]


# ── The blob the server hands the summarizer ────────────────────────


def test_server_blob_headers_name_the_copilot_as_author():
    """Provenance starts before the summarizer sees anything. The
    headers used to be passive ('Clarifying questions raised', 'Risks
    flagged') and read as though a participant did the raising."""
    import os
    os.environ.setdefault("MEETING_RECORDER_SKIP_DEP_REPAIR", "1")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from _app_import import import_app
    import_app()
    import server

    class _FakeSession:
        copilot_ticks = [
            {"clarifying_questions": ["Confirm the Globex routing path"],
             "risks": ["Headcount may not absorb the volume"],
             "follow_ups": ["Send the sizing model to Jane Doe"]},
        ]

    blob = server._copilot_observations_blob(_FakeSession())
    assert "Questions the AI co-pilot suggested asking" in blob
    assert "generated, not asked by anyone" in blob
    assert "Risks the AI co-pilot generated (not raised by anyone)" in blob
    assert "Follow-ups the AI co-pilot proposed (not agreed by anyone)" in blob
    # The passive phrasings that made a generated question read as a
    # participant's must not come back.
    assert "Clarifying questions raised" not in blob
    assert "Risks flagged" not in blob
    assert blob.count("Confirm the Globex routing path") == 1


def test_server_blob_is_empty_without_ticks():
    import os
    os.environ.setdefault("MEETING_RECORDER_SKIP_DEP_REPAIR", "1")
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from _app_import import import_app
    import_app()
    import server

    class _NoTicks:
        copilot_ticks: list = []

    assert server._copilot_observations_blob(_NoTicks()) == ""
