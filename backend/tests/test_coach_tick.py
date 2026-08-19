"""coach_tick behavior — dedup, filler removal, empty-output error, and
the total-output cap. The Summarizer imports the anthropic SDK at module
load, which isn't in the test env, so we stub it before import. The LLM
call itself is monkeypatched, so no network / no real model runs.
"""

import asyncio
import sys
from unittest.mock import MagicMock

# Stub optional deps that summarizer.py's import graph touches but the
# test env doesn't install: the anthropic SDK (summarizer top-level) and
# python-dotenv (config.settings, imported when a Summarizer is built).
for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core.summarizer import Summarizer  # noqa: E402


def _summarizer(reply: str) -> Summarizer:
    s = Summarizer(api_key="x", model="claude-haiku-4-5", provider="anthropic")

    async def _fake_chat(prompt, **kwargs):
        return reply

    s._chat = _fake_chat  # type: ignore[assignment]
    return s


def _run(coro):
    return asyncio.run(coro)


def test_dedup_against_prior_and_filler_removed():
    reply = (
        '{"clarifying_questions":["Confirm SCV telephony: BYOT vs Amazon?"],'
        '"risks":["Vendor lock-in risk"],'
        '"follow_ups":["Request an update"]}'
    )
    s = _summarizer(reply)
    prior = [{"clarifying_questions": [], "risks": ["vendor lock in risk"],
              "follow_ups": []}]
    out = _run(s.coach_tick(
        segments=[{"speaker": "A", "text": "we are moving to Salesforce SCV"}],
        prior_ticks=prior,
    ))
    assert out["clarifying_questions"] == ["Confirm SCV telephony: BYOT vs Amazon?"]
    assert out["risks"] == []        # near-duplicate of a prior tick → dropped
    assert out["follow_ups"] == []   # generic filler → dropped
    assert "error" not in out


def test_empty_model_output_surfaces_error():
    # Gemini-2.5 "reasoning ate the budget → empty content" case.
    s = _summarizer("")
    out = _run(s.coach_tick(
        segments=[{"speaker": "A", "text": "discussing the ZTX endpoint"}]))
    assert out["error"] == "no_output"
    assert out["clarifying_questions"] == []


def test_unparseable_output_surfaces_error():
    s = _summarizer("here are some thoughts, no json at all")
    out = _run(s.coach_tick(
        segments=[{"speaker": "A", "text": "talking about India carriers"}]))
    assert out["error"] == "no_output"


def test_total_output_capped_at_three():
    reply = (
        '{"clarifying_questions":["ZTX rate limits and failover?",'
        '"India in-country carrier via which SBC?","Philippines Connect region?"],'
        '"risks":["Bedrock agent-core scope not pinned to a phase"],'
        '"follow_ups":["Name the VDL endpoint owner"]}'
    )
    s = _summarizer(reply)
    out = _run(s.coach_tick(
        segments=[{"speaker": "A", "text": "ZTX India Philippines Bedrock VDL"}]))
    total = (len(out["clarifying_questions"]) + len(out["risks"])
             + len(out["follow_ups"]))
    assert total <= 3


def test_no_segments_is_quiet_no_error():
    s = _summarizer("{}")
    out = _run(s.coach_tick(segments=[]))
    assert out == {"clarifying_questions": [], "risks": [], "follow_ups": []}
