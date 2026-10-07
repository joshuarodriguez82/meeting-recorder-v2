"""
The Co-Pilot board merges a question asked again in other words.

Field board 2026-10-06: a call on one topic left 22 open questions and
none merged. The true rephrasings shared 44-55 % of their content words
(under the 60 % word-overlap bar) and scored 0.72-0.77 on the app's
local sentence model; distinct questions on the same topic scored
0.33-0.65.

The pairs below keep the shape of those questions with every name
swapped for a placeholder (AGENTS.md). Swapping names moves the scores:
two field pairs that scored 0.73-0.74 fell to 0.59-0.66 once anonymised,
so only pairs that keep their property are used here.

The fake-encoder tests run everywhere; the last one uses the real model
and is skipped where sentence-transformers or its cached weights are
absent (as in CI's slim env).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from core import copilot_board
from core.copilot_board import CopilotBoard

REPHRASED = [
    ("Sam: when you say 'numbers ported to Amazon Connect'—does that "
     "assume native AWS termination, or are you modeling a call-out / "
     "BYOC hybrid for failover or compliance zones?",
     "When you say 'port those numbers to Amazon Connect'—does that mean "
     "native AWS termination via Amazon's carrier partners, or are you "
     "modeling SIP trunk failover to Initech as Plan B?"),
    ("Sam: when you say 'forwarding Initech numbers to Amazon Connect "
     "DIDs'—does Globex own those Amazon-assigned DIDs, or does Amazon "
     "assign them dynamically per call? Affects failover & number porting.",
     "Sam: when you say 'Amazon Connect number' / 'DIDs Amazon World'—are "
     "those Amazon-assigned DIDs or customer-owned portable numbers? "
     "Affects failover and number portability if strategy shifts."),
]
DISTINCT = [
    ("Native AWS Connect + DID routing: does Globex own existing DIDs, or "
     "migrate from the current carrier? Portability timeline?",
     "Sam: does Globex's new 4.5-year Initech contract include "
     "early-termination fees or number-portability penalties if you "
     "migrate before year 5?"),
    ("Sam's deck: does it quantify the call-out trade-offs (latency, "
     "failover, feature parity vs. native) so Amazon can validate "
     "feasibility?",
     "Sam: does Globex's new 4.5-year Initech contract include "
     "early-termination fees or number-portability penalties if you "
     "migrate before year 5?"),
]


def _tick(*questions):
    return {"clarifying_questions": list(questions), "risks": [],
            "follow_ups": []}


def _fake_encoder(groups):
    """Texts in the same group get the same vector; groups are
    orthogonal."""
    def encode(texts):
        out = []
        for t in texts:
            v = np.zeros(8, dtype=np.float32)
            v[next(i for i, g in enumerate(groups) if t in g)] = 1.0
            out.append(v)
        return np.stack(out)
    return encode


def _run(board, *ticks):
    for t in ticks:
        board.prepare(t)
        board.merge(t)
    return [it.text for it in board.open_items()]


@pytest.mark.parametrize("a,b", REPHRASED)
def test_a_rephrased_question_merges(monkeypatch, a, b):
    monkeypatch.setattr(copilot_board, "encoder_factory",
                        lambda: _fake_encoder([{a, b}]))
    board = CopilotBoard()
    assert _run(board, _tick(a), _tick(b)) == [a]
    assert board.open_items()[0].times_suggested == 2


@pytest.mark.parametrize("a,b", DISTINCT)
def test_a_different_question_stays(monkeypatch, a, b):
    monkeypatch.setattr(copilot_board, "encoder_factory",
                        lambda: _fake_encoder([{a}, {b}]))
    assert _run(CopilotBoard(), _tick(a), _tick(b)) == [a, b]


def test_without_the_model_the_word_rule_still_applies(monkeypatch):
    a, b = REPHRASED[0]
    monkeypatch.setattr(copilot_board, "encoder_factory", lambda: None)
    assert _run(CopilotBoard(), _tick(a), _tick(b)) == [a, b]
    assert _run(CopilotBoard(), _tick(a), _tick(a + " ")) == [a]


def test_an_encoder_failure_never_costs_the_tick(monkeypatch):
    def broken():
        def encode(texts):
            raise RuntimeError("model file truncated")
        return encode
    monkeypatch.setattr(copilot_board, "encoder_factory", broken)
    a, b = REPHRASED[0]
    assert _run(CopilotBoard(), _tick(a), _tick(b)) == [a, b]


def test_a_dismissed_question_stays_dismissed_when_reworded(monkeypatch):
    a, b = REPHRASED[1]
    monkeypatch.setattr(copilot_board, "encoder_factory",
                        lambda: _fake_encoder([{a, b}]))
    board = CopilotBoard()
    _run(board, _tick(a))
    board.set_status(board.items[0].id, "dismissed")
    assert _run(board, _tick(b)) == []


def test_a_restored_board_is_embedded_before_comparing(monkeypatch):
    """Entries loaded from the saved session have no vectors yet;
    prepare() embeds them along with the new bullets."""
    a, b = REPHRASED[0]
    saved = CopilotBoard()
    monkeypatch.setattr(copilot_board, "encoder_factory", lambda: None)
    _run(saved, _tick(a))
    monkeypatch.setattr(copilot_board, "encoder_factory",
                        lambda: _fake_encoder([{a, b}]))
    board = CopilotBoard(saved.to_list())
    assert _run(board, _tick(b)) == [a]


def _real_model_ready() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        from config.settings import USER_DATA_DIR
    except Exception:
        return False
    cache = Path(USER_DATA_DIR) / "models" / "minilm-l6"
    return cache.is_dir() and any(cache.iterdir())


@pytest.mark.skipif(not _real_model_ready(),
                    reason="local sentence model not installed here")
def test_the_real_model_tells_rephrasings_from_new_questions():
    for a, b in REPHRASED:
        assert _run(CopilotBoard(), _tick(a), _tick(b)) == [a], (a, b)
    for a, b in DISTINCT:
        assert _run(CopilotBoard(), _tick(a), _tick(b)) == [a, b], (a, b)
