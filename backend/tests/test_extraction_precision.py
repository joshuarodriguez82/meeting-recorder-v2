"""
The extractors must define what does NOT belong in their category.

FIELD ANALYSIS 2026-08-21, one real discovery call (735-line
transcript). The app produced:

  * 14 "decisions" in decisions_<call>.txt
  * 11 "decisions" in action_items_<call>.txt — a DIFFERENT list,
    overlapping but neither a subset of the other
  * 39 requirement rows

Reading them against the transcript, roughly four were decisions
actually made in the meeting. The rest were:

  - tasks/commitments ("Provide refreshed volume data to AWS",
    "Develop dual-track cost modeling") — action items, already
    extracted as action items, duplicated into decisions;
  - pre-existing roadmap a participant reported ("Replace Genesys in
    2027") — context, decided long before this call;
  - facts ("Establish clear timeline awareness for Genesys contract
    expiration") — nobody decided the contract's end date;
  - restatements of an entry already in the list ("Prioritize
    Genesys-to-Connect funding for 2027" alongside "Replace Genesys
    with Amazon Connect in 2027").

Two causes, both fixed:

  1. Decisions were extracted TWICE — extract_action_items asked for
     a "## Decisions Made" section while extract_decisions
     independently asked for every decision. Two independent
     extractions of one meeting do not agree, and the user cannot tell
     which list is real. Decisions now have one extractor.

  2. Neither prompt said what its category EXCLUDES. "Extract every
     DECISION made" with a five-slot template is a recall instruction
     with slots to fill; nothing pushed back toward precision.

These are source-level assertions: the prompts are what changed, and
there is no LLM in the test suite to measure output against. They
pin the rules so a future edit cannot quietly drop them — the same
guard style used for the extension's parsing invariants.
"""

from __future__ import annotations

import inspect
import sys
from unittest.mock import MagicMock

# Same stubs test_coach_tick.py uses: summarizer.py imports the
# anthropic SDK at module load and config.settings pulls python-dotenv,
# neither of which the test env installs. No LLM is called here — these
# assertions read the prompt source.
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core import summarizer  # noqa: E402


def _src(fn) -> str:
    """Source with comment-only lines removed: the assertions below are
    about the PROMPT the model receives, and the comments explaining
    why a rule exists quote the very strings being checked."""
    out = []
    for line in inspect.getsource(fn).splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_decisions_are_extracted_in_exactly_one_place():
    """extract_action_items must not also produce a decisions list."""
    action_src = _src(summarizer.Summarizer.extract_action_items)
    assert "## Decisions Made" not in action_src, (
        "the action-items prompt is asking for decisions again — two "
        "independent extractions of one meeting produce two "
        "disagreeing lists (field: 11 vs 14)")
    # The dedicated extractor still owns them.
    assert "## Decision:" in _src(summarizer.Summarizer.extract_decisions)


def test_the_decisions_prompt_excludes_the_categories_that_inflated_it():
    src = _src(summarizer.Summarizer.extract_decisions)
    assert "NOT decisions" in src
    # Each exclusion corresponds to a class of false positive found in
    # the field output.
    assert "action item" in src.lower()            # tasks/commitments
    # (Prompt strings wrap across source lines, so match the
    # contiguous fragment rather than the rendered sentence.)
    assert "BEFORE this" in src                    # reported roadmap
    assert "fact, date or contract term" in src    # facts
    assert "restatement" in src.lower()            # duplicates
    # And evidence, which is what makes precision checkable.
    assert "Evidence:" in src
    assert "quote" in src.lower()


def test_the_requirements_prompt_excludes_tasks_and_decisions():
    src = _src(summarizer.Summarizer.extract_requirements)
    assert "NOT requirements" in src
    assert "action item" in src.lower()
    assert "decision" in src.lower()
    assert "One row per requirement" in src
