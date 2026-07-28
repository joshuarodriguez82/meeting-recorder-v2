"""Summaries must not be silently cut off.

Field repro (2026-07-31): meeting summaries stopped mid-sentence
("...based on KB match quality,"; "...unavailable day one (") and were
saved, exported, and emailed as if complete. Two causes, both pinned
here:

  1. `summarize` asked for only 1024 output tokens (~750 words). A real
     meeting summary is routinely longer, so the model hit the ceiling
     mid-word.
  2. NOTHING in the module inspected `stop_reason` / `finish_reason`, so
     hitting that ceiling produced no error, no log, and no marker — the
     truncation was invisible until a human read to the end.
"""

import sys
from unittest.mock import MagicMock

# summarizer.py imports the anthropic SDK at module load, and building a
# Summarizer touches config.settings (python-dotenv). Neither is in the
# lightweight test env.
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core.summarizer import Summarizer, _flag_truncation  # noqa: E402


def _summarizer(provider: str = "anthropic") -> Summarizer:
    return Summarizer(api_key="x", model="claude-haiku-4-5", provider=provider)


# ── The marker: truncation is never silent ──────────────────────────

def test_untruncated_text_is_returned_untouched():
    text = "A complete summary that ended on its own."
    assert _flag_truncation(text, False, 8192, "claude-haiku-4-5") == text


def test_truncated_text_is_marked_visibly():
    # The exact shape of the bug: output ends mid-clause.
    text = "...agent evaluates confidence low/medium/high based on KB match quality,"
    out = _flag_truncation(text, True, 8192, "claude-haiku-4-5")

    assert out.startswith(text.rstrip()), "original content must be preserved"
    assert "cut off" in out.lower(), "user must be told it was truncated"
    assert "8,192" in out, "the limit that was hit should be named"


def test_marker_survives_trailing_whitespace():
    out = _flag_truncation("cut here   \n\n", True, 4096, "m")
    assert "cut here" in out and "cut off" in out.lower()


# ── The budgets: enough room for a real summary ─────────────────────

def test_summary_budget_is_large_enough_for_a_real_meeting():
    # 1024 tokens (~750 words) truncated real summaries. Anything at or
    # below that ceiling reintroduces the bug.
    budget = _summarizer()._budget(8192)
    assert budget >= 8192, f"summary budget regressed to {budget}"


def test_openai_compatible_providers_get_headroom_for_hidden_reasoning():
    # Gemini 2.5 et al burn hidden reasoning tokens against max_tokens —
    # the same effect that made the live co-pilot return nothing at all.
    anthropic_budget = _summarizer("anthropic")._budget(8192)
    openai_budget = _summarizer("openai")._budget(8192)
    assert openai_budget > anthropic_budget, (
        "OpenAI-compatible providers need a larger budget than Anthropic "
        "for the same visible output")


def test_budget_scales_with_the_requested_base():
    s = _summarizer()
    assert s._budget(4096) < s._budget(8192)
