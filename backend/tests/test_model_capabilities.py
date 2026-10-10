"""
Any Claude model picked in Settings works without code changes.

The model payloads are the Models API's documented response shape
(``GET /v1/models/{id}``: ``capabilities`` as a tree with ``supported``
at each leaf), and the replies are the Messages API's: a model that
thinks by default returns a ``thinking`` block — empty text plus a
signature — before its ``text`` block. The bug these guard: the reply
was read as ``content[0].text``, which on such a model is the thinking
block, so picking Haiku 5.5, Sonnet 5.5 or Opus 5.5 broke every summary.

The last test drives the real Anthropic SDK against a mock transport
(skipped where the SDK isn't installed, as in CI's slim test env).
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import model_capabilities as mc

#: Models API entry for a 5.x model (thinking adaptive, every effort level).
NEW_MODEL = {
    "id": "claude-haiku-5-5", "type": "model", "display_name": "Claude Haiku 5.5",
    "created_at": "2026-10-01T00:00:00Z",
    "max_input_tokens": 1000000, "max_tokens": 128000,
    "capabilities": {
        "image_input": {"supported": True},
        "structured_outputs": {"supported": True},
        "thinking": {"supported": True, "types": {
            "enabled": {"supported": False}, "adaptive": {"supported": True}}},
        "effort": {"supported": True, "low": {"supported": True},
                   "medium": {"supported": True}, "high": {"supported": True},
                   "xhigh": {"supported": True}, "max": {"supported": True}},
    },
}
#: …and for a model without effort (Haiku 4.5's shape: budget thinking only).
OLD_MODEL = {
    "id": "claude-haiku-4-5", "type": "model", "display_name": "Claude Haiku 4.5",
    "created_at": "2025-10-01T00:00:00Z",
    "max_input_tokens": 200000, "max_tokens": 64000,
    "capabilities": {
        "thinking": {"supported": True, "types": {
            "enabled": {"supported": True}, "adaptive": {"supported": False}}},
        "effort": {"supported": False, "low": {"supported": False},
                   "medium": {"supported": False}, "high": {"supported": False},
                   "xhigh": {"supported": False}, "max": {"supported": False}},
    },
}


def _block(btype, **kw):
    return SimpleNamespace(type=btype, **kw)


def _reply(*blocks, stop="end_turn", details=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           stop_details=details, usage=None)


THINKING = _block("thinking", thinking="", signature="EqQBCkYI…")


# ── the pure decisions ───────────────────────────────────────────────


def test_capabilities_are_read_from_the_models_api_entry():
    new = mc.traits_from_model_info(NEW_MODEL, "claude-haiku-5-5")
    assert new.effort_levels == ("low", "medium", "high", "xhigh", "max")
    assert new.adaptive_thinking and new.max_output_tokens == 128000
    old = mc.traits_from_model_info(OLD_MODEL, "claude-haiku-4-5")
    assert old.effort_levels == () and not old.adaptive_thinking


def test_effort_is_sent_only_to_a_model_that_supports_it():
    new = mc.traits_from_model_info(NEW_MODEL, "m")
    old = mc.traits_from_model_info(OLD_MODEL, "m")
    assert mc.request_extras(new, "low") == {"output_config": {"effort": "low"}}
    assert mc.request_extras(old, "low") == {}
    assert mc.request_extras(mc.unknown_traits("m"), "low") == {}
    assert mc.request_extras(new, None) == {}


def test_an_effort_level_the_model_lacks_steps_down():
    partial = mc.ModelTraits(model="m", effort_levels=("low", "medium", "high"))
    assert mc.request_extras(partial, "max") == {"output_config": {"effort": "high"}}


def test_the_answer_is_the_text_blocks_wherever_they_are():
    assert mc.response_text([THINKING, _block("text", text="Summary.")]) == "Summary."
    assert mc.response_text([_block("text", text="a"), THINKING,
                             _block("text", text="b")]) == "ab"
    assert mc.response_text([{"type": "text", "text": "dict form"}]) == "dict form"
    assert mc.response_text([THINKING]) == ""


@pytest.mark.parametrize("value,expected", [
    (None, "low"), ("medium", "medium"), ("default", None), ("bogus", "low"),
])
def test_effort_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("AI_EFFORT", raising=False)
    else:
        monkeypatch.setenv("AI_EFFORT", value)
    assert mc.configured_effort() == expected


# ── the summarizer's request path ────────────────────────────────────


def _summarizer(monkeypatch, replies, model_info=NEW_MODEL, lookups=None):
    for name in ("anthropic", "dotenv"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    from core.summarizer import Summarizer
    s = Summarizer.__new__(Summarizer)
    s._provider = "anthropic"
    s._model = model_info["id"] if model_info else "claude-haiku-5-5"
    s.cache_stats = {"read": 0, "write": 0, "uncached": 0, "output": 0, "calls": 0}
    sent = []
    lookups = lookups if lookups is not None else []

    class _Messages:
        async def create(self, **kw):
            sent.append(kw)
            return replies.pop(0)

    class _Models:
        async def retrieve(self, model):
            lookups.append(model)
            if model_info is None:
                raise RuntimeError("no models access")
            return model_info

    s._anthropic_client = SimpleNamespace(messages=_Messages(), models=_Models())
    return s, sent


def test_a_reply_that_starts_with_thinking_returns_its_text(monkeypatch):
    s, sent = _summarizer(monkeypatch, [_reply(THINKING, _block("text", text="The summary."))])
    assert asyncio.run(s._chat("Summarize")) == "The summary."
    assert sent[0]["output_config"] == {"effort": "low"}
    assert "temperature" not in sent[0] and "thinking" not in sent[0]


def test_an_older_model_gets_the_plain_request(monkeypatch):
    s, sent = _summarizer(monkeypatch, [_reply(_block("text", text="ok"))],
                          model_info=OLD_MODEL)
    assert asyncio.run(s._chat("x")) == "ok"
    assert "output_config" not in sent[0]


def test_when_the_lookup_fails_the_plain_request_still_works(monkeypatch):
    s, sent = _summarizer(monkeypatch, [_reply(THINKING, _block("text", text="ok"))],
                          model_info=None)
    assert asyncio.run(s._chat("x")) == "ok"
    assert "output_config" not in sent[0]


def test_capabilities_are_looked_up_once(monkeypatch):
    lookups = []
    s, _ = _summarizer(monkeypatch, [_reply(_block("text", text="a")),
                                     _reply(_block("text", text="b"))],
                       lookups=lookups)
    asyncio.run(s._chat("1"))
    asyncio.run(s._chat("2"))
    assert lookups == ["claude-haiku-5-5"]


def test_thinking_that_uses_the_whole_budget_is_retried_with_more_room(monkeypatch):
    s, sent = _summarizer(monkeypatch, [
        _reply(THINKING, stop="max_tokens"),
        _reply(THINKING, _block("text", text="Done."), stop="end_turn"),
    ])
    assert asyncio.run(s._chat("x", max_tokens=1000)) == "Done."
    first = 1000 + mc.THINKING_HEADROOM_TOKENS
    assert [k["max_tokens"] for k in sent] == [first, first + mc.RETRY_EXTRA_TOKENS]


def test_a_model_that_thinks_gets_room_for_it_from_the_first_request(monkeypatch):
    """Field log 2026-10-09 (Haiku 5.5, effort low): a 512-token Co-Pilot
    tick came back cut off mid-JSON and a 4,096-token daily briefing
    stopped ~800 tokens into its answer — thinking had used the rest."""
    s, sent = _summarizer(monkeypatch, [_reply(THINKING, _block("text", text="{}"))])
    asyncio.run(s._chat("x", max_tokens=512))
    assert sent[0]["max_tokens"] == 512 + mc.THINKING_HEADROOM_TOKENS


def test_a_model_that_does_not_think_gets_the_budget_it_asked_for(monkeypatch):
    s, sent = _summarizer(monkeypatch, [_reply(_block("text", text="ok"))],
                          model_info=OLD_MODEL)
    asyncio.run(s._chat("x", max_tokens=512))
    assert sent[0]["max_tokens"] == 512


def test_the_headroom_never_exceeds_what_the_model_can_return():
    traits = mc.ModelTraits(model="m", adaptive_thinking=True,
                            max_output_tokens=6000)
    assert mc.first_budget(traits, 4096) == 6000
    assert mc.first_budget(traits, 8000) == 8000      # never below the ask
    assert mc.first_budget(mc.unknown_traits("m"), 4096) == 4096


def test_a_refusal_is_reported_as_such(monkeypatch):
    details = SimpleNamespace(category="cyber", explanation=None)
    s, _ = _summarizer(monkeypatch, [_reply(stop="refusal", details=details)])
    with pytest.raises(mc.ModelDeclined, match="declined this request \\(cyber\\)"):
        asyncio.run(s._chat("x"))


def test_streamed_answers_carry_the_same_request_shape(monkeypatch):
    s, _ = _summarizer(monkeypatch, [])
    seen = {}

    class _Stream:
        def __init__(self, kw):
            seen.update(kw)

        async def __aenter__(self):
            async def gen():
                yield "Hello"
            return SimpleNamespace(text_stream=gen())

        async def __aexit__(self, *a):
            return False

    s._anthropic_client.messages.stream = lambda **kw: _Stream(kw)

    async def run():
        return [t async for t in s.stream_chat("q")]
    assert asyncio.run(run()) == ["Hello"]
    assert seen["output_config"] == {"effort": "low"}
    assert seen["max_tokens"] > mc.THINKING_HEADROOM_TOKENS


# ── the real SDK, against a mock transport ───────────────────────────


def test_the_real_sdk_end_to_end():
    """The SDK parses the Models API entry into its own ModelInfo and the
    reply into its own block types; the app reads both correctly."""
    anthropic = pytest.importorskip("anthropic")
    if isinstance(anthropic, MagicMock):
        pytest.skip("anthropic is stubbed in this test session")
    import httpx2

    sent = []

    def handler(request):
        if request.url.path.startswith("/v1/models/"):
            return httpx2.Response(200, json=NEW_MODEL)
        import json
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_01", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "EqQBCkYI"},
                {"type": "text", "text": "Real SDK summary."},
            ],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    from core.summarizer import Summarizer
    s = Summarizer(api_key="sk-test", model="claude-haiku-5-5")
    s._anthropic_client = anthropic.AsyncAnthropic(
        api_key="sk-test",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(handler)))
    assert asyncio.run(s._chat("Summarize")) == "Real SDK summary."
    assert sent[0]["output_config"] == {"effort": "low"}
    assert s._traits.effort_levels[-1] == "max"
