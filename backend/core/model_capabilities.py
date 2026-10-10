"""
Talking to whichever Claude model is configured — including ones
released after this build — without code changes.

WHY
---
The model is a setting. The request was written for the model of the
day: it read the reply as ``msg.content[0].text``. Every Claude model
from the 5.x generation (Haiku 5.5, Sonnet 5.5, Opus 5.5) thinks by
default, so a reply can begin with a ``thinking`` block that has no
``.text`` — and every summary, extraction and Co-Pilot tick would have
failed the moment someone picked one in Settings. The same models count
thinking against ``max_tokens``, so a budget sized for the answer alone
can end with no answer at all.

So the request is shaped by what the model says about itself, not by
its name: Anthropic's Models API reports each model's capabilities
(``GET /v1/models/{id}``), and those decide whether effort is sent and
at which level. Text is read from the blocks whose type is ``text``,
wherever they are. Thinking shares ``max_tokens`` with the answer, so a
model that thinks gets headroom from the first request; a reply that
still runs out before any text is retried once with room to spare. A
model that declines is reported as such. None of it names a model, so
the next one works the same way.

Nothing here sends sampling parameters or assistant prefill: newer
models reject both, and no request in the app needs them.

Pure — the caller fetches capabilities and makes the requests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: How hard the model thinks before answering, when it supports the
#: choice. The app's work — summaries, extractions, Co-Pilot ticks — ran
#: without thinking on the models it was built for, and "low" keeps it
#: close to that in cost and speed (a model may still think a little, or
#: not at all, on simple requests). Override with AI_EFFORT
#: (low | medium | high | xhigh | max, or "default" to send nothing).
DEFAULT_EFFORT = "low"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

#: A retry after an empty, truncated reply: the first budget plus this,
#: capped by what the model can return.
RETRY_EXTRA_TOKENS = 4096

#: Added to every request to a model that thinks, before the first try.
#: Thinking shares max_tokens with the answer, and every budget in the
#: app was sized for the answer alone. Field log 2026-10-09, Haiku 5.5 at
#: effort "low": a daily briefing cut off at 4,096 with ~800 tokens of
#: answer written, a Co-Pilot tick cut off at 512 mid-JSON, and six
#: calls that spent their whole budget thinking and had to be re-sent.
#: max_tokens is a ceiling, not a charge — tokens are billed as
#: produced — so the room costs nothing unless the model uses it.
THINKING_HEADROOM_TOKENS = 4096


@dataclass
class ModelTraits:
    """What a model supports, from its Models API entry."""
    model: str
    effort_levels: tuple = ()
    adaptive_thinking: bool = False
    max_output_tokens: Optional[int] = None
    context_tokens: Optional[int] = None
    display_name: str = ""
    known: bool = True              # False when the lookup failed


def _leaf(tree: Any, *path: str) -> bool:
    """``tree[path...]["supported"]``, False for anything missing."""
    node = tree
    for key in path:
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    return isinstance(node, dict) and bool(node.get("supported"))


def traits_from_model_info(info: Any, model: str) -> ModelTraits:
    """ModelTraits from a Models API object (SDK object or plain dict)."""
    data = info
    if not isinstance(info, dict):
        to_dict = getattr(info, "to_dict", None) or getattr(info, "model_dump", None)
        data = to_dict() if callable(to_dict) else {}
    caps = data.get("capabilities") or {}
    levels = tuple(lv for lv in EFFORT_LEVELS
                   if _leaf(caps, "effort") and _leaf(caps, "effort", lv))
    return ModelTraits(
        model=model,
        effort_levels=levels,
        adaptive_thinking=_leaf(caps, "thinking", "types", "adaptive"),
        max_output_tokens=data.get("max_tokens"),
        context_tokens=data.get("max_input_tokens"),
        display_name=data.get("display_name") or "",
    )


def unknown_traits(model: str) -> ModelTraits:
    """When the Models API can't be asked: send nothing optional — the
    plain request every Claude model accepts."""
    return ModelTraits(model=model, known=False)


def configured_effort() -> Optional[str]:
    """The effort to ask for, or None to leave it to the model."""
    raw = (os.getenv("AI_EFFORT") or DEFAULT_EFFORT).strip().lower()
    if raw in ("", "default", "none", "model"):
        return None
    return raw if raw in EFFORT_LEVELS else DEFAULT_EFFORT


def request_extras(traits: ModelTraits,
                   effort: Optional[str] = None) -> dict:
    """Optional request fields this model supports. Only what it says it
    supports is sent; an effort level it lacks steps down to the nearest
    one it has rather than failing the request."""
    if effort is None or not traits.effort_levels:
        return {}
    if effort in traits.effort_levels:
        return {"output_config": {"effort": effort}}
    order = list(EFFORT_LEVELS)
    want = order.index(effort) if effort in order else 0
    lower = [lv for lv in traits.effort_levels if order.index(lv) <= want]
    pick = lower[-1] if lower else traits.effort_levels[0]
    return {"output_config": {"effort": pick}}


def response_text(content: Iterable[Any]) -> str:
    """The answer in a reply: every ``text`` block, in order. Thinking
    blocks (empty by default on models that think) and any other block
    type are skipped, wherever they sit."""
    parts = []
    for block in content or ():
        btype = getattr(block, "type", None)
        if btype is None and isinstance(block, dict):
            btype = block.get("type")
        if btype != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        parts.append(text or "")
    return "".join(parts)


def first_budget(traits: ModelTraits, max_tokens: int) -> int:
    """``max_tokens`` for the first request: the caller's budget for the
    answer, plus THINKING_HEADROOM_TOKENS when the model thinks, capped
    by what the model can return. Unchanged for a model that doesn't."""
    if not traits.adaptive_thinking:
        return max_tokens
    bigger = max_tokens + THINKING_HEADROOM_TOKENS
    if traits.max_output_tokens:
        bigger = min(bigger, traits.max_output_tokens)
    return max(max_tokens, bigger)


def retry_budget(traits: ModelTraits, max_tokens: int) -> Optional[int]:
    """A bigger ``max_tokens`` for one retry after a reply that ran out
    before writing any text, or None when there's no more room."""
    bigger = max_tokens + RETRY_EXTRA_TOKENS
    cap = traits.max_output_tokens
    if cap:
        bigger = min(bigger, cap)
    return bigger if bigger > max_tokens else None


class ModelDeclined(RuntimeError):
    """The model declined the request (``stop_reason: "refusal"``)."""

    def __init__(self, model: str, category: Optional[str] = None,
                 explanation: Optional[str] = None):
        self.category = category
        what = f" ({category})" if category else ""
        super().__init__(
            f"{model} declined this request{what}. Try again, or pick a "
            f"different model in Settings → AI Models."
            + (f" {explanation}" if explanation else ""))


def declined(msg: Any, model: str) -> Optional[ModelDeclined]:
    """A ModelDeclined for a refused reply, else None."""
    if getattr(msg, "stop_reason", None) != "refusal":
        return None
    details = getattr(msg, "stop_details", None)
    return ModelDeclined(model, getattr(details, "category", None),
                         getattr(details, "explanation", None))
