"""The no-invented-precision rule, as it reaches every prompt builder.

v2.35.1's DATE ANCHOR stopped the model inventing dates. It worked, and
it was too narrow: the underlying defect is ADDING PRECISION THE SOURCE
DID NOT CARRY, and the very next summary off the same recording
produced three more instances of it that the anchor says nothing about.

  1. A section stated "she identified SEVEN candidate intents" and then
     listed six. The transcript contained exactly six. A count
     manufactured out of nothing, contradicted by the list directly
     underneath it.
  2. An action item was given the target "By end of meeting". The
     source gave no timing at all for that item.
  3. A demo was scheduled for a named calendar week. The source said
     only "rolling out this week and next week" and "a week or two
     out" — a vague window sharpened into a specific one.

Same shape as the invented year. So the rule is stated ONCE, in
core/_precision.py, and every builder references that text. These tests
pin two things:

  * COVERAGE — the clauses actually reach each builder's prompt. A
    builder that quietly stopped emitting them would fail here rather
    than in a customer's inbox.
  * NON-DUPLICATION — the clause text exists in exactly one source
    file. Fifteen paraphrases is how this class of bug survives: they
    drift, and the weakest one becomes the one that ships.

No model is ever called. `_chat` / `stream_chat` are monkeypatched to
capture the prompt, which is the artifact under test. (Same pattern as
test_summary_provenance.py and test_prep_brief_documents.py.)
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# summarizer.py imports the anthropic SDK at module load and
# config.settings imports python-dotenv; neither is in the lightweight
# test env. Same stub pattern as test_coach_tick.py.
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core._precision import (  # noqa: E402
    PRECISION_ATTRIBUTION,
    PRECISION_CLOSE,
    PRECISION_COMPACT,
    PRECISION_COUNTS,
    PRECISION_HEADER,
    PRECISION_IDENTIFIERS,
    PRECISION_LEAD,
    PRECISION_TIMING,
)
from core.summarizer import Summarizer  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    return asyncio.run(coro)


def _capturing(reply: str = "## Summary\n- body"):
    s = Summarizer(api_key="x", model="claude-haiku-4-5", provider="anthropic")
    prompts: list[str] = []

    async def _fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return reply

    s._chat = _fake_chat  # type: ignore[assignment]
    return s, prompts


# The field case, anonymised. Six items enumerated by one speaker and a
# commitment with no timing whatsoever — the two shapes that produced
# the fabricated "seven" and the fabricated "By end of meeting".
MEETING_DATE = "2026-08-19T14:03:11.412000"
SIX_INTENTS_TRANSCRIPT = (
    "[00:00 → 00:22] Jane Doe: I went through the Globex call logs and "
    "pulled out the intents — billing, outage, upgrade, cancellation, "
    "appointment and password reset.\n"
    "[00:22 → 00:31] Richard Roe: Good. Can you get the routing map "
    "over to me?\n"
    "[00:31 → 00:35] Jane Doe: Yeah, I'll put that together.\n"
    "[00:35 → 00:44] Richard Roe: And the demo — we're rolling out this "
    "week and next week, so it's a week or two out.\n"
)

ALL_CLAUSES = (
    PRECISION_COUNTS, PRECISION_TIMING,
    PRECISION_IDENTIFIERS, PRECISION_ATTRIBUTION,
)


def assert_full_block(prompt: str, where: str = "") -> None:
    """Every clause, the lead and the close, each exactly once."""
    for piece in (PRECISION_HEADER, PRECISION_LEAD, *ALL_CLAUSES,
                  PRECISION_CLOSE):
        assert prompt.count(piece) == 1, (
            f"{where}: expected exactly one copy of {piece[:48]!r}, "
            f"found {prompt.count(piece)}"
        )


# ── The three field defects, one per clause ─────────────────────────


def test_a_count_may_not_exceed_the_list_it_sits_above():
    """Defect 1. The summary said seven and listed six; the transcript
    had six. Nothing in the old prompt made a stated count answerable to
    the list underneath it, so the rule has to tie the two together
    rather than just say 'be accurate'."""
    s, prompts = _capturing()
    _run(s.summarize(SIX_INTENTS_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert ("Never state a number of items, people, branches, options or "
            "occurrences that is not stated in the source or literally "
            "countable from it.") in prompt
    assert ("If you enumerate a list, any count you give must equal the "
            "number of items you actually wrote.") in prompt
    # The escape hatch matters as much as the prohibition: without it the
    # model picks a number rather than dropping the phrase.
    assert "Prefer omitting a count to guessing one." in prompt


def test_an_untimed_commitment_is_reported_as_untimed():
    """Defect 2. 'By end of meeting' was attached to an item the source
    never timed. The correct output is a statement that the timing was
    not specified — not a blank the model feels obliged to fill."""
    s, prompts = _capturing()
    _run(s.extract_action_items(SIX_INTENTS_TRANSCRIPT,
                                meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert ("If the source gave no timing, say the timing was not "
            "specified.") in prompt
    assert "Never manufacture a deadline" in prompt


def test_a_vague_window_may_not_be_sharpened_into_a_named_one():
    """Defect 3. 'Rolling out this week and next week' / 'a week or two
    out' became a specific named week. The date anchor does not catch
    this: the sharpened week IS derivable from the anchor, which is
    exactly what makes it look defensible. The prohibition on SHARPENING
    is the separate rule that catches it."""
    s, prompts = _capturing()
    _run(s.summarize(SIX_INTENTS_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert ('never sharpen a vague one: "next week" must not become a '
            'specific date, "a week or two" must not become a named '
            "week.") in prompt


def test_the_positive_principle_is_stated_not_just_the_banned_cases():
    """The generalising sentence. A list of banned mistakes only bans
    the mistakes already made; this is what makes the rule cover the
    next invention nobody has seen yet."""
    s, prompts = _capturing()
    _run(s.summarize(SIX_INTENTS_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert ("Write only what the source material in front of you "
            "actually carries. Never add precision the source did not "
            "have.") in prompt
    assert ("An unqualified or absent detail is correct; an invented one "
            "is a factual error the reader cannot catch.") in prompt
    # The real fabrications landed in a heading and a table cell.
    assert ("This applies to section headings, table cells and JSON "
            "field values exactly as it applies to prose.") in prompt


# ── Coverage: the summarizer's ten prompt builders ──────────────────


@pytest.mark.parametrize("method", [
    "extract_action_items", "extract_decisions", "extract_requirements",
])
def test_markdown_extractors_get_the_full_block(method):
    s, prompts = _capturing()
    _run(getattr(s, method)(SIX_INTENTS_TRANSCRIPT,
                            meeting_date=MEETING_DATE))
    assert_full_block(prompts[0], method)


def test_summarize_gets_the_full_block():
    s, prompts = _capturing()
    _run(s.summarize(SIX_INTENTS_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    assert_full_block(prompts[0], "summarize")


def test_summarize_carries_the_block_through_any_template():
    """The rule rides after whatever prompt the template service
    resolved, so a user-written template — which will never contain
    these rules — is covered too. That is why this lives in summarize()
    and not in template_service.DEFAULT_TEMPLATES."""
    s, prompts = _capturing()
    _run(s.summarize(SIX_INTENTS_TRANSCRIPT,
                     prompt="Write it as a haiku.",
                     template_name="user-made", meeting_date=MEETING_DATE))
    assert "Write it as a haiku." in prompts[0]
    assert_full_block(prompts[0], "summarize/custom-template")


def test_both_prep_briefs_get_the_full_block():
    """A brief is the least verifiable artifact in the app — nobody
    re-listens to five prior calls to check an invented commitment."""
    s, prompts = _capturing()
    _run(s.meeting_prep_brief("### [S1] Kickoff (2026-01-01)\nnotes",
                              "Cutover planning", today_iso="2026-08-19"))
    assert_full_block(prompts[0], "meeting_prep_brief")

    s2, prompts2 = _capturing()
    _run(s2.meeting_prep_brief_from_calendar(
        upcoming_subject="Cutover planning",
        upcoming_attendees=["dana.doe@globex.example"],
        upcoming_when="Monday Sep 07 at 10:00 AM",
        identified_client="Globex",
        identified_project="Phase 2",
        prior_notes="### [S1] Kickoff (2026-01-01)\nnotes",
        today_iso="2026-08-19",
    ))
    assert_full_block(prompts2[0], "meeting_prep_brief_from_calendar")


def test_structured_extractor_gets_the_block_without_breaking_its_json():
    """extract_structured has no prose to say 'timing not specified' in.
    The reconciliation has to say how THIS schema spells it, or the
    timing clause invites the model to editorialise inside a JSON string
    — or outside the JSON altogether."""
    s, prompts = _capturing(
        '{"requirements":[],"decisions":[],"action_items":[],'
        '"open_questions":[]}')
    _run(s.extract_structured(SIX_INTENTS_TRANSCRIPT,
                              meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert_full_block(prompt, "extract_structured")
    assert '"due", an unstated or unclear deadline is ""' in prompt
    assert "never a guessed date" in prompt
    assert "Still output nothing but the JSON object." in prompt


def test_structured_json_reconciliation_ships_without_a_meeting_date():
    """It used to be gated on having a date, because the DATE ANCHOR was.
    The timing clause is not gated, so its reconciliation cannot be
    either — an undated session is the one most likely to get a deadline
    invented for it."""
    s, prompts = _capturing(
        '{"requirements":[],"decisions":[],"action_items":[],'
        '"open_questions":[]}')
    _run(s.extract_structured(SIX_INTENTS_TRANSCRIPT, meeting_date=""))
    prompt = prompts[0]
    assert "DATE ANCHOR" not in prompt          # no date, no anchor
    assert PRECISION_TIMING in prompt           # rule ships anyway
    assert '"due", an unstated or unclear deadline is ""' in prompt


def test_daily_briefing_parse_gets_the_block_and_the_json_note():
    """This parse is the LLM step that puts events on the Record tab's
    timeline (extension_calendar_service consumes what it returns), so
    an invented time is a wrong meeting on the user's calendar, not
    merely a wrong sentence."""
    s, prompts = _capturing('{"agenda":[]}')
    _run(s.parse_daily_briefing("9:30 standup with Jane Doe",
                                today_iso="2026-08-19"))
    prompt = prompts[0]
    assert_full_block(prompt, "parse_daily_briefing")
    assert '"due", an unstated or unclear deadline is ""' in prompt
    # The pre-existing per-field rules must survive alongside it.
    assert "do NOT guess a time" in prompt
    assert "Never construct one." in prompt
    assert "=== BRIEFING TEXT ===" in prompt


def test_identify_speakers_takes_only_the_clauses_that_apply():
    """DELIBERATE PARTIAL. The output is a JSON map of speaker labels to
    names: no list to enumerate, no count to state, no date or deadline
    anywhere in it. COUNTS and TIMINGS would be instructions about
    surfaces this builder cannot produce. IDENTIFIERS and ATTRIBUTION
    are the entire failure mode — a plausible name for a speaker nobody
    named, or a real name pinned to the wrong turn."""
    s, prompts = _capturing('{"SPEAKER_00": "Jane Doe"}')
    _run(s.identify_speakers(SIX_INTENTS_TRANSCRIPT))
    prompt = prompts[0]
    assert PRECISION_IDENTIFIERS in prompt
    assert PRECISION_ATTRIBUTION in prompt
    assert PRECISION_COUNTS not in prompt
    assert PRECISION_TIMING not in prompt
    # The omission is of clauses, not of the rule: lead and close stay,
    # so the generalising principle still reaches this builder.
    assert PRECISION_LEAD in prompt
    assert PRECISION_CLOSE in prompt
    # And the prompt it always emitted is intact around it.
    assert "SELF-INTRODUCTION" in prompt
    assert prompt.rstrip().endswith(SIX_INTENTS_TRANSCRIPT.rstrip())


def test_coach_tick_takes_the_compact_form_not_the_full_block():
    """DELIBERATE COMPRESSION. This prompt is re-sent in full on every
    tick — hot ticks fire ~every 15s against a 256-token budget and a
    10s timeout — so the full block would be paid dozens of times per
    meeting and would dilute the 'usually output nothing' bias the rest
    of the coach's rules exist to create. Live output is ≤2 bullets of
    ≤120 chars: no enumerated list to miscount, no prose to sharpen a
    date in."""
    s, prompts = _capturing('{"clarifying_questions":[],"risks":[],'
                            '"follow_ups":[]}')
    _run(s.coach_tick([{"speaker": "Jane Doe", "text": "six intents"}]))
    prompt = prompts[0]
    assert PRECISION_COMPACT in prompt
    assert PRECISION_HEADER not in prompt
    assert PRECISION_COUNTS not in prompt
    # Compressed, but not to nothing — all four classes survive.
    for phrase in ("Never invent a count, a deadline, a name, a number "
                   "or a system that is not in it",
                   "never sharpen a vague timing into a specific one",
                   "never attribute anything to someone not shown "
                   "saying it"):
        assert phrase in prompt
    assert ("An absent detail is correct; an invented one is a factual "
            "error the reader cannot catch.") in prompt


def test_hot_coach_tick_inherits_the_same_compact_rule():
    """The hot variant prepends its own urgency framing to the shared
    output rules; it must not be a second, drifting copy."""
    s, prompts = _capturing('{"clarifying_questions":[],"risks":[],'
                            '"follow_ups":[]}')
    _run(s.coach_tick([{"speaker": "Jane Doe", "text": "six intents"}],
                      hot=True))
    prompt = prompts[0]
    assert "HOT TICK MODE" in prompt
    assert prompt.count(PRECISION_COMPACT) == 1


# ── The rule is not conditional on knowing the date ─────────────────


def test_the_rule_ships_even_when_the_anchor_cannot():
    """The anchor needs a date; the precision rule does not. A session
    with no recorded start (legacy imports) is precisely the one most
    likely to get a deadline invented for it, so dropping the anchor
    must not drop the rule with it."""
    s, prompts = _capturing()
    _run(s.summarize(SIX_INTENTS_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=""))
    prompt = prompts[0]
    assert "DATE ANCHOR" not in prompt
    assert_full_block(prompt, "summarize/no-date")


def test_anchor_and_rule_are_both_present_and_do_not_collide():
    """They overlap on undated commitments by design — the anchor's
    date-shaped instance and the general clause reinforce each other —
    but each must appear once, in its own block."""
    s, prompts = _capturing()
    _run(s.summarize(SIX_INTENTS_TRANSCRIPT, prompt="Summarize.",
                     meeting_date=MEETING_DATE))
    prompt = prompts[0]
    assert prompt.count("=== DATE ANCHOR") == 1
    assert prompt.count(PRECISION_HEADER) == 1
    assert prompt.index("=== DATE ANCHOR") < prompt.index(PRECISION_HEADER)


# ── Coverage: the service-layer prompt builders ─────────────────────


class _FakeStreamer:
    """Minimal Summarizer stand-in for QAService — it only calls
    stream_chat."""

    def __init__(self):
        self.prompts: list[str] = []

    async def stream_chat(self, prompt, max_tokens=1024):
        self.prompts.append(prompt)
        yield "answer"


def _drain(agen):
    async def _go():
        return [x async for x in agen]
    return _run(_go())


def test_knowledge_base_answers_get_the_full_block():
    """The Knowledge Base tab. Its existing rules already forbid invented
    session IDs, invented document names and answering beyond the
    excerpts — and "she identified seven candidate intents" over a list
    of six violates none of them: every citation is real and every claim
    is in the excerpts. The count is the invention."""
    from services.qa_service import QAService

    fake = _FakeStreamer()
    qa = QAService(search_service=None, summarizer=fake)
    _drain(qa.stream_answer("what intents did she find?", sources=[
        {"session_id": "ABC123", "display_name": "Globex discovery",
         "start_s": 42.0, "text": "billing, outage, upgrade"},
    ]))
    prompt = fake.prompts[0]
    assert_full_block(prompt, "qa_service.stream_answer")
    # The citation contract has to survive alongside it.
    assert "[session_id @ mm:ss]" in prompt
    assert "USER QUESTION: what intents did she find?" in prompt


def test_in_call_search_answers_get_the_full_block():
    """The panel the user opens mid-call to ask 'what was that number
    she just said?'. A fabricated figure here is repeated out loud to
    the room seconds later. One-shot and user-initiated, not a poll
    loop, so it takes the full block rather than the compact form."""
    from services.qa_service import QAService

    fake = _FakeStreamer()
    qa = QAService(search_service=None, summarizer=fake)
    _drain(qa.stream_inline_answer("what was that number?",
                                   context="You: forty-two agents"))
    prompt = fake.prompts[0]
    assert_full_block(prompt, "qa_service.stream_inline_answer")
    assert "LIVE TRANSCRIPT:" in prompt


def test_commitment_extraction_gets_the_block_and_the_json_note():
    """A commitment record is chased across meetings and read months
    later by someone who will not re-listen to the call. Every clause
    has a slot to be violated in: owner (attribution), quote
    (identifiers — it is supposed to be verbatim), description (counts),
    due_date_iso (timings)."""
    from services import commitments_service as cs

    prompt = cs.COMMITMENTS_EXTRACTION_PROMPT.format(
        today_iso="2026-08-19", customer_hint="Globex",
        transcript=SIX_INTENTS_TRANSCRIPT,
    )
    assert_full_block(prompt, "COMMITMENTS_EXTRACTION_PROMPT")
    # JSON array, not object — the reconciliation must say so or it
    # contradicts the "Output ONLY a JSON array" contract above it.
    assert '"due_date_iso", an unstated or unclear deadline is ""' in prompt
    assert "Still output nothing but the JSON array." in prompt
    # Anchored resolution is NOT what the timing clause forbids: this
    # prompt legitimately turns "by Friday" into a date against a stated
    # reference date. Both survive together.
    assert '"by Friday" → next Friday' in prompt
    assert "Reference date for resolving relative deadlines: 2026-08-19" \
        in prompt
    # The transcript is still last, after the rules.
    assert prompt.index(PRECISION_HEADER) < prompt.index("=== TRANSCRIPT ===")


def test_follow_up_email_draft_gets_the_full_block():
    """The only artifact in the app that leaves the machine addressed to
    someone else. A fabricated deadline here is a commitment the
    recipient now believes they made."""
    from services import _follow_up_email_outlook as fu

    prompts: list[str] = []

    # Goes through the provider-agnostic `_chat`, not a raw Anthropic
    # client. The previous shape called `summarizer._client`, which
    # `Summarizer` does not define — so this path raised AttributeError
    # for every user until it was routed here.
    async def _chat(prompt, **kwargs):
        prompts.append(prompt)
        return "SUBJECT: x\nBODY:\nbody"

    fake = SimpleNamespace(_chat=_chat, _model="claude-haiku-4-5")
    subject, body = _run(fu._compose_body(
        fake, meeting_title="Globex discovery", owner="Jane Doe",
        tasks=["Send the routing map"], decisions_md="", summary_md="",
        tone="friendly",
    ))
    assert subject == "x"
    assert_full_block(prompts[0], "_compose_body")
    # The output contract it always had is intact.
    assert "SUBJECT: <subject line, short, actionable>" in prompts[0]


def test_client_tagging_suggestions_get_the_block():
    """Suggestions are a JSON array of ids drawn from a list we supplied.
    IDENTIFIERS is the load-bearing clause: an id the model composes
    rather than copies silently drops the suggestion, and a `reason`
    naming a system nobody mentioned is what makes a wrong suggestion
    look researched."""
    import os
    os.environ.setdefault("MEETING_RECORDER_SKIP_DEP_REPAIR", "1")
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_import import import_app
    import_app()
    import server

    prompts: list[str] = []

    async def _fake_chat(prompt, **kwargs):
        prompts.append(prompt)
        return "[]"

    fake_svc = SimpleNamespace(
        load_settings=lambda: None,
        summarizer=SimpleNamespace(_chat=_fake_chat),
        session_svc=SimpleNamespace(list_sessions=lambda: [
            {"session_id": "ABC123", "display_name": "Globex discovery",
             "client": "", "summary": "intent analysis"},
        ]),
    )
    original = server.svc
    server.svc = fake_svc
    try:
        _run(server.suggest_tagging(
            server.SuggestTaggingRequest(client="Globex", project="")))
    finally:
        server.svc = original

    assert_full_block(prompts[0], "suggest_tagging")
    assert "ID:ABC123 | Globex discovery" in prompts[0]


# ── Anti-drift: the rule exists in exactly one place ────────────────


def _python_sources() -> list[Path]:
    skip = {"_precision.py"}
    return [
        p for p in BACKEND_ROOT.rglob("*.py")
        if p.name not in skip
        and "tests" not in p.parts
        and "__pycache__" not in p.parts
        and ".venv" not in p.parts
        and "site-packages" not in p.parts
    ]


@pytest.mark.parametrize("clause", [
    PRECISION_LEAD, PRECISION_COUNTS, PRECISION_TIMING,
    PRECISION_IDENTIFIERS, PRECISION_ATTRIBUTION, PRECISION_CLOSE,
])
def test_no_call_site_carries_its_own_copy_of_a_clause(clause):
    """The whole point of a shared constant. A builder that pastes the
    text instead of referencing it will drift the moment the rule is
    revised — and the stale copy becomes the one that ships. Whitespace
    is normalised so a reflowed paste is caught too."""
    needle = re.sub(r"\s+", " ", clause).strip()
    offenders = []
    for path in _python_sources():
        body = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        # Source literals are wrapped and quote-concatenated, so compare
        # against the file with its string-joining punctuation removed.
        flat = re.sub(r'"\s*\+?\s*"', "", body)
        if needle in flat or needle in body:
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert offenders == [], (
        f"clause duplicated outside core/_precision.py: {offenders}"
    )


def test_the_compact_form_is_a_derivative_not_a_rewrite():
    """It is allowed to be shorter; it is not allowed to lose a class.
    Each of the four gets a recognisable stem in the compact text, so a
    future edit that drops one fails here rather than silently leaving
    live coaching un-covered."""
    compact = PRECISION_COMPACT.lower()
    for stem in ("count", "deadline", "name", "sharpen", "attribute"):
        assert stem in compact, f"compact form dropped {stem!r}"
