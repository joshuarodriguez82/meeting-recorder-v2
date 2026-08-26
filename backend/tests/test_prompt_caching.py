"""
The transcript is sent once per session, not once per extractor.

FIELD DATA, August 2026 (the org's own token export): 5,545,790 input
tokens billed, `cache_read_input_tokens` **0** for the entire month —
no prompt caching at all, while every session sent its transcript five
times (summary, structured, action items, decisions, requirements).

The cause was ordering, not a missing flag. Prompt caching is a PREFIX
match, and `_with_user_notes` built each prompt as
`instruction + transcript`. Since the instruction differs per
extractor, the shared prefix ended at character zero and nothing was
ever cacheable.

Transcript and user notes are per-SESSION and identical across all
five calls, so they became the cached prefix; the per-EXTRACTOR
instruction moved after the breakpoint.

    without caching   5 x T
    with caching      1.25 x T (write) + 4 x 0.1 x T (read) = 1.65 x T

A cache that silently is NOT hitting looks exactly like no cache, so
these assert the wire shape rather than trusting the setting.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

for _m in ("anthropic", "dotenv"):
    sys.modules.setdefault(_m, MagicMock())

from core.summarizer import Summarizer, _split_for_cache  # noqa: E402


TRANSCRIPT = "SPEAKER_00: We agreed to ship on Friday.\n" * 40


def test_the_transcript_is_the_prefix_and_the_instruction_is_the_tail():
    prefix, tail = _split_for_cache("EXTRACT DECISIONS", TRANSCRIPT, "")
    assert TRANSCRIPT in prefix
    assert "EXTRACT DECISIONS" not in prefix, (
        "the instruction is inside the cached prefix — every extractor "
        "would get a different prefix and nothing would cache")
    assert tail == "EXTRACT DECISIONS"


def test_all_extractors_share_one_identical_prefix():
    """The whole saving depends on this being byte-identical."""
    notes = "Kickoff was moved up a week."
    prefixes = {
        _split_for_cache(instr, TRANSCRIPT, notes)[0]
        for instr in ("SUMMARY", "ACTION ITEMS", "DECISIONS",
                      "REQUIREMENTS", "STRUCTURED")
    }
    assert len(prefixes) == 1, (
        "extractors produced different prefixes; the cache would miss "
        "on every call after the first")


def test_user_notes_ride_in_the_prefix_not_the_tail():
    """Notes are per-session, so they belong in the cached span."""
    prefix, tail = _split_for_cache("X", TRANSCRIPT, "Budget approved.")
    assert "Budget approved." in prefix
    assert "Budget approved." not in tail


class _FakeUsage:
    cache_read_input_tokens = 1200
    cache_creation_input_tokens = 0
    input_tokens = 40
    output_tokens = 90


class _FakeMessage:
    content = [MagicMock(text="ok")]
    stop_reason = "end_turn"
    usage = _FakeUsage()


def _summarizer():
    s = Summarizer.__new__(Summarizer)
    s._provider = "anthropic"
    s._model = "claude-haiku-4-5"
    s.cache_stats = {"read": 0, "write": 0, "uncached": 0,
                     "output": 0, "calls": 0}
    return s


def test_the_request_carries_a_cache_breakpoint_on_the_transcript():
    """The wire shape is the thing that actually bills differently."""
    import asyncio

    sent = {}

    class _Messages:
        async def create(self, **kwargs):
            sent.update(kwargs)
            return _FakeMessage()

    s = _summarizer()
    s._anthropic_client = MagicMock(messages=_Messages())

    asyncio.run(s._chat("INSTRUCTION", cache_prefix="THE TRANSCRIPT"))

    blocks = sent["messages"][0]["content"]
    assert isinstance(blocks, list), "content collapsed to a bare string"
    assert blocks[0]["text"] == "THE TRANSCRIPT"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}, (
        "no breakpoint on the transcript block — nothing caches")
    # The instruction must sit AFTER the breakpoint, or it becomes part
    # of the prefix and each extractor invalidates the others.
    assert blocks[-1]["text"] == "INSTRUCTION"
    assert "cache_control" not in blocks[-1]


def test_cache_hits_are_recorded_so_a_silent_miss_is_visible():
    import asyncio

    class _Messages:
        async def create(self, **kwargs):
            return _FakeMessage()

    s = _summarizer()
    s._anthropic_client = MagicMock(messages=_Messages())
    asyncio.run(s._chat("I", cache_prefix="T"))

    assert s.cache_stats["read"] == 1200
    assert s.cache_stats["calls"] == 1


def test_a_call_without_a_prefix_still_sends_a_plain_string():
    """Coach ticks and repair passes have no transcript to cache."""
    import asyncio

    sent = {}

    class _Messages:
        async def create(self, **kwargs):
            sent.update(kwargs)
            return _FakeMessage()

    s = _summarizer()
    s._anthropic_client = MagicMock(messages=_Messages())
    asyncio.run(s._chat("JUST THIS"))
    assert sent["messages"][0]["content"] == "JUST THIS"


# ── not paying twice for identical inputs ────────────────────────────


def test_the_fingerprint_moves_only_when_the_output_would():
    from core.prompt_version import extraction_fingerprint as fp

    base = fp("transcript text", "notes", "General")
    assert fp("transcript text", "notes", "General") == base
    # Anything that changes the produced text changes the fingerprint.
    assert fp("different transcript", "notes", "General") != base
    assert fp("transcript text", "other notes", "General") != base
    assert fp("transcript text", "notes", "Discovery") != base


def test_a_prompt_edit_invalidates_every_session():
    """The reprocessing runs existed because prompts changed. That must
    keep working — the skip is not allowed to outlive a prompt edit."""
    import core.prompt_version as pv

    before = pv.extraction_fingerprint("t", "n", "General")
    original = pv.EXTRACTOR_PROMPT_VERSION
    try:
        pv.EXTRACTOR_PROMPT_VERSION = "9999-01-01.9"
        after = pv.extraction_fingerprint("t", "n", "General")
    finally:
        pv.EXTRACTOR_PROMPT_VERSION = original
    assert after != before


def test_the_cache_warming_call_is_not_inside_the_gather():
    """Five concurrent calls all race the cache write and every one
    misses — the cache would report 0 reads and look identical to the
    bug it fixes. Source-pinned because it is invisible at runtime."""
    from pathlib import Path

    # Read the source rather than importing server: this assertion is
    # about the shape of the call, and importing pulls the whole audio
    # stack in for no benefit.
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8")
    start = src.index("async def process_full(")
    body = src[start:start + 14000]
    # The warming call is wrapped in try/except to keep the best-effort
    # contract (one extractor failing must not cancel the rest), so the
    # assertion is on the await itself, not the assignment.
    assert "summary_r = await _do_summary()" in body, (
        "the cache-warming call was folded back into the gather")
    gather_args = body.split("asyncio.gather(", 1)[1].split(")", 1)[0]
    assert "_do_summary()" not in gather_args, (
        "summary is back inside the gather — all five calls would race "
        "the cache write and every one would miss")
