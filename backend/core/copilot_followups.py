"""
After the call: which Co-Pilot questions were answered, and what's left.

During a meeting the Co-Pilot raises questions to ask and follow-ups to
chase. Afterwards the useful thing is not the list — it is which of them
the conversation actually closed, what the answer was, and which are
still open and worth sending to the customer.

This module holds the parts that are decisions rather than model calls:
which board items are worth checking, how a checked answer is applied
back to the board, and the follow-ups document exported with the other
meeting files. The check itself is one call per meeting
(Summarizer.resolve_copilot_items), on the full transcript.

Pure — no I/O.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from core.copilot_board import CopilotBoard

#: What a checked item can be.
ANSWERED = "answered"
PARTLY = "partly"
OPEN = "open"
RESOLUTIONS = (ANSWERED, PARTLY, OPEN)

#: Kinds worth checking. A risk isn't "answered"; a question or a
#: follow-up is closed or it isn't.
CHECKED_KINDS = ("clarifying_questions", "follow_ups")

#: Cap on one check: a long call's board can grow, and the call has to
#: stay a single, bounded request.
MAX_ITEMS = 60


def board_for(session) -> List[dict]:
    """The session's board — or, for a meeting recorded before the board
    existed, one rebuilt from its saved Co-Pilot updates, so older calls
    get follow-ups too."""
    board = list(getattr(session, "copilot_board", None) or [])
    if board:
        return board
    rebuilt = CopilotBoard()
    for tick in getattr(session, "copilot_ticks", None) or []:
        rebuilt.merge(tick, now=str(tick.get("generated_at") or "")[:19])
    for item in rebuilt.items:
        item.fresh = False
    return rebuilt.to_list()


def items_to_check(board: Iterable[dict]) -> List[dict]:
    """Questions and follow-ups the user didn't dismiss, not yet checked."""
    out = []
    for item in board:
        if item.get("kind") not in CHECKED_KINDS:
            continue
        if item.get("status") == "dismissed":
            continue
        if item.get("resolution") in RESOLUTIONS:
            continue
        out.append(item)
    return out[:MAX_ITEMS]


def apply_resolutions(board: List[dict],
                      resolutions: Dict[str, dict]) -> int:
    """Write checked results onto the board. Unknown ids and unknown
    statuses are ignored — a model reply is never trusted to invent an
    item or a state. Returns how many items were updated."""
    updated = 0
    by_id = {str(i.get("id")): i for i in board}
    for item_id, res in (resolutions or {}).items():
        item = by_id.get(str(item_id))
        status = str((res or {}).get("status") or "").strip().lower()
        if item is None or status not in RESOLUTIONS:
            continue
        item["resolution"] = status
        answer = str((res or {}).get("answer") or "").strip()
        item["answer"] = answer[:600] if status != OPEN else ""
        updated += 1
    return updated


def _bullet(text: str) -> str:
    return "- " + " ".join(str(text or "").split())


def render_markdown(session, board: Optional[List[dict]] = None) -> str:
    """The follow-ups document: open items first (what to send), then
    what the call answered. Empty string when there is nothing to say."""
    board = board if board is not None else board_for(session)
    checked = [i for i in board if i.get("kind") in CHECKED_KINDS
               and i.get("status") != "dismissed"]
    if not checked:
        return ""

    open_items = [i for i in checked if i.get("resolution") in (OPEN, None)
                  and i.get("status") not in ("done",)]
    partly = [i for i in checked if i.get("resolution") == PARTLY]
    answered = [i for i in checked if i.get("resolution") == ANSWERED]
    risks = [i for i in board if i.get("kind") == "risks"
             and i.get("status") != "dismissed"]

    title = (getattr(session, "display_name", "") or "").strip()
    lines = [f"# Follow-ups — {title}" if title else "# Follow-ups", ""]
    lines.append(
        "_Questions and follow-ups the AI Co-Pilot raised during the call, "
        "checked afterwards against the transcript. These are suggestions, "
        "not things anyone agreed to — review before sending._")
    lines.append("")

    unchecked = all(i.get("resolution") is None for i in checked)
    if open_items:
        lines.append("## Still open" + (" (not yet checked)" if unchecked
                                         else " — candidates for follow-up"))
        lines += [_bullet(i["text"]) for i in open_items]
        lines.append("")
    if partly:
        lines.append("## Partly answered")
        for i in partly:
            lines.append(_bullet(i["text"]))
            if i.get("answer"):
                lines.append(f"  - So far: {' '.join(i['answer'].split())}")
        lines.append("")
    if answered:
        lines.append("## Answered during the call")
        for i in answered:
            lines.append(_bullet(i["text"]))
            if i.get("answer"):
                lines.append(f"  - {' '.join(i['answer'].split())}")
        lines.append("")
    if risks:
        lines.append("## Risks the Co-Pilot flagged")
        lines += [_bullet(i["text"]) for i in risks]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
