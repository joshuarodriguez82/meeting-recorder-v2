"""
Who a follow-up email should be addressed to — the owner-attribution
step shared by both follow-up-email backends.

This module answers one question: *for this session, which named people
have outstanding work, and where did that attribution come from?* Both
`_follow_up_email_outlook.py` (Windows/COM) and `_follow_up_email_macos.py`
(AppleScript) import `build_draft_plan()` — the parsing lives here once
rather than once per platform.

Two sources, in preference order
--------------------------------

1. **Commitments** (`services/commitments_service.py`). A `Commitment`
   carries a real `owner` field produced by a dedicated extraction pass,
   and we run it through `services/owner_service.py`'s `resolve_owners()`
   so "Mark/Sam" splits into two people and "Samantha" collapses onto
   "Sam" when the user has confirmed that alias. That is structured data
   with real provenance, and it is strictly better than a regex over
   generated prose.

   Commitments are a per-session JSON sidecar, written by the extraction
   pass (`/sessions/{id}/commitments/extract`, and automatically at the
   end of `process_full`). **A session may legitimately have none** —
   it predates the feature, the user never ran the extraction, the
   summarizer was unavailable at the time, or the meeting genuinely
   contained no personal commitments. So this is a preference, not a
   requirement, and we fall through when the sidecar is missing, empty,
   or contains only delivered/dismissed items.

2. **The `action_items` markdown**, re-parsed. This is the legacy path
   and the reason this module exists.

Why the markdown parser got rewritten
-------------------------------------

The old parser was a single regex that required *exactly*
`- [ ] **[Owner]**: description` — the shape `extract_action_items` asks
the model for. One deviation (a table, an em dash, missing bold, a
nested bullet) made every line unreadable, and the feature reported
"Claude didn't attribute any items to a specific person". That is the
recurring failure this codebase keeps hitting: **something we could not
parse rendering as something that is not there.** So the parser below is
tolerant of what models actually emit, and — just as important — the
result distinguishes *nothing to read*, *could not read it* and *read it,
nobody individual owns anything*. See `NO_ACTION_ITEMS`,
`UNREADABLE_FORMAT` and `GENERIC_OWNERS_ONLY`.

Where the ambiguity line is drawn
---------------------------------

The danger in loosening the parser is reading a description that happens
to contain a colon ("- [ ] Send the report: include Q3 numbers") as an
owner named "Send the report". The rule is:

* An owner that is **explicitly marked** — bolded `**Sam**`, bracketed
  `[Sam]`, or both `**[Sam]**` — is trusted as written. The author (the
  model) told us which span is the owner; we do not second-guess it.
* An owner that is **not marked** (`- [ ] Sam: send the deck`) has to
  survive `looks_like_person()`: at most four whitespace tokens, at most
  40 characters, every token capitalised (or a name particle like "van",
  a suffix like "Jr.", or a bracketed region tag), not starting with a
  task verb, and — for the single-token case, where there is the least
  evidence — not carrying topic-label morphology ("Pricing:",
  "Onboarding:", "Migration:").
* The **ASCII hyphen** separator (`Sam - send the deck`) is accepted only
  with a marked owner. Descriptions contain " - " constantly; em dash and
  en dash do not, so those are accepted unmarked.

That combination rejects every unmarked candidate that reads like a
sentence fragment while keeping real names, including the awkward
calendar form "Roe, Pat Jr. [US-EMEA]".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from services.owner_service import (
    AliasIndex, load_alias_index, normalize_owner, resolve_owners,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ── Draft-plan states ────────────────────────────────────────────────
#
# These three are the point of the module. Conflating the middle one
# into the last one is the bug that produced the field report: action
# items existed, the parser could not read a single line, and the UI
# blamed the model for not attributing anything.

READY = "ready"
NO_ACTION_ITEMS = "no_action_items"
UNREADABLE_FORMAT = "unreadable_format"
GENERIC_OWNERS_ONLY = "generic_owners_only"

STATE_MESSAGES = {
    NO_ACTION_ITEMS: (
        "No action items on this session yet — extract action items "
        "first, then draft follow-ups."
    ),
    UNREADABLE_FORMAT: (
        "This session has action items, but none of them are in a format "
        "we can read an owner out of — so nobody could be addressed. "
        "This is a formatting problem, not a missing-owner one. "
        "Re-running extraction usually fixes it."
    ),
    GENERIC_OWNERS_ONLY: (
        "Every action item is owned by the whole group (\"team\", \"all\", "
        "\"everyone\") — there is no individual to address an email to."
    ),
}


# ── Generic owners ───────────────────────────────────────────────────
#
# Owners we can never resolve to one mailbox. "team" / "all" / "everyone"
# must stay here — a follow-up email addressed to "Team" is worse than
# no email. The rest are the same idea in other words, plus the
# placeholder tokens models reach for when they have no owner.

GENERIC_OWNERS = {
    "team", "all", "everyone", "everybody", "group", "tbd", "tbc",
    "unknown", "unassigned", "n/a", "na", "none", "nobody", "no one",
    "we", "us", "our", "attendees", "all attendees", "both", "both teams",
    "owner", "owners", "someone", "anyone", "staff", "everyone else",
    "whole team", "the group", "participants", "all parties",
}

# Stripped before the GENERIC_OWNERS lookup so "The Team" == "team".
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|our|entire|whole|full)\s+", re.IGNORECASE)


def is_generic_owner(owner: str) -> bool:
    """True when `owner` names a group rather than a person."""
    key, _display = normalize_owner(owner or "")
    if not key:
        return True
    key = _LEADING_ARTICLE_RE.sub("", key).strip()
    return key in GENERIC_OWNERS


# ── "Does this look like a person's name?" ───────────────────────────

# Words an action item starts with when the leading span is the task,
# not the owner. Only consulted for UNMARKED candidates.
_TASK_VERBS = {
    "add", "align", "approve", "arrange", "assess", "assign", "book",
    "bring", "build", "call", "check", "circulate", "clarify", "close",
    "collect", "compile", "complete", "confirm", "connect", "coordinate",
    "create", "decide", "define", "deliver", "deploy", "determine",
    "discuss", "document", "download", "draft", "draw", "email",
    "engage", "ensure", "escalate", "evaluate", "expand", "explore",
    "export", "finalise", "finalize", "find", "finish", "fix", "follow",
    "forward", "gather", "get", "give", "hand", "identify", "implement",
    "import",
    "investigate", "kick", "look", "loop", "make", "merge", "migrate",
    "monitor", "move", "onboard", "open", "order", "organise",
    "organize", "outline", "pick", "ping", "plan", "prepare", "present",
    "produce", "provide", "publish", "pull", "push", "reach", "rebuild",
    "recheck", "remove", "replace", "request", "research", "resolve",
    "review", "revisit", "run", "schedule", "send", "set", "share",
    "sign", "start", "submit", "summarise", "summarize", "sync", "take",
    "test", "track", "train", "update", "upload", "validate", "verify",
    "walk", "work", "write",
}

# Lowercase tokens that legitimately appear inside a person's name.
_NAME_PARTICLES = {
    "van", "von", "de", "der", "den", "del", "della", "di", "da", "du",
    "la", "le", "bin", "ibn", "al", "mc", "mac", "ter", "ten",
}
_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "phd", "md"}

# Endings that mark a topic label rather than a name. Only applied to
# single-token unmarked candidates, where "Pricing: confirm with finance"
# would otherwise pass every other test.
_TOPIC_LABEL_ENDINGS = (
    "ing", "tion", "sion", "ment", "ness", "ity", "ance", "ence",
    "ology", "ics", "ship", "work", "line", "date", "notes", "steps",
    "items", "risks", "issues", "actions", "updates",
)

_NAME_PART_TRIM = " ,.()[]"


def looks_like_person(candidate: str) -> bool:
    """Conservative "could this span be a person's name?" test, applied
    only to owner candidates that were NOT explicitly marked up.

    Accepts: "Sam", "Jane Doe", "Roe, Pat Jr.", "Sam (Acme)",
             "Poe, Alex [US-EMEA]", "Ana van der Noh".
    Rejects: "Send the report", "Next steps", "Follow Up",
             "Pricing", "Decide on the pricing model".
    """
    s = (candidate or "").strip()
    if not s or len(s) > 40:
        return False
    if any(ch in s for ch in ";!?/\\|"):
        return False
    if s.count(",") > 1:
        return False
    tokens = [t for t in s.split() if t.strip(_NAME_PART_TRIM)]
    if not tokens or len(tokens) > 4:
        return False

    bare = [t.strip(_NAME_PART_TRIM) for t in tokens]
    if bare[0].lower() in _TASK_VERBS:
        return False
    if not any(ch.isalpha() for ch in "".join(bare)):
        return False

    for i, tok in enumerate(bare):
        low = tok.lower()
        if low in _NAME_PARTICLES or low in _NAME_SUFFIXES:
            continue
        if not tok[:1].isupper():
            return False
        # A token that is ALL-CAPS with digits ("US-EMEA", "AWS") is fine;
        # a token that is otherwise mixed-case is fine too. Anything
        # starting lowercase already returned above.
        if i == 0 and len(bare) == 1:
            if low.endswith(_TOPIC_LABEL_ENDINGS):
                return False
    return True


# ── Markdown shapes ──────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.*?)\s*#*\s*$")
_ACTION_HEADING_RE = re.compile(
    r"action\s*items?|next\s*steps?|follow[\s-]?ups?|to[\s-]?dos?|tasks?"
    r"|commitments?|assignments?",
    re.IGNORECASE,
)

# Bullet or numbered list item. Indentation is captured but unused —
# nested bullets are items too, which the old parser also handled but
# only by accident of `^\s*`.
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(?P<rest>\S.*?)\s*$")
# A GitHub-style checkbox at the head of a bullet's content. `[ ]`,
# `[x]`, `[X]` and the malformed-but-common `[]`.
_CHECKBOX_RE = re.compile(r"^\[\s*[xX]?\s*\]\s*(?P<rest>\S.*)$")
# Markdown table row: at least one interior pipe, leading/trailing pipes
# optional-but-usual.
_TABLE_ROW_RE = re.compile(r"^\s*\|(?P<body>.*)\|\s*$")
_TABLE_RULE_CELL_RE = re.compile(r"^:?-{2,}:?$")

# Owner marked with bold and/or brackets, anchored at the head of the
# item's content: **Sam**, **[Sam]**, [Sam], __Sam__.
_MARKED_OWNER_RE = re.compile(
    r"^(?:"
    r"(?:\*\*|__)\s*\[?\s*(?P<bold>[^\]*_]+?)\s*\]?\s*(?:\*\*|__)"
    r"|"
    r"\[\s*(?P<bracket>[^\]]+?)\s*\]"
    r")\s*(?P<sep>[:—–-])?\s*(?P<desc>.*)$"
)

# Unmarked owner: everything up to the first `:`, em dash or en dash.
# The ASCII hyphen is deliberately absent — see the module docstring.
_UNMARKED_OWNER_RE = re.compile(
    r"^(?P<owner>[^:—–]{1,40}?)\s*[:—–]\s*(?P<desc>\S.*)$"
)

# Table header cells that identify the owner and the task columns.
_OWNER_HEADER_WORDS = {
    "owner", "who", "assignee", "assigned", "assigned to", "responsible",
    "person", "name", "lead",
}
_TASK_HEADER_WORDS = {
    "action", "action item", "action items", "task", "tasks", "item",
    "items", "description", "what", "detail", "details", "next step",
    "next steps", "commitment", "deliverable",
}


@dataclass
class OwnerParse:
    """Outcome of reading `action_items` markdown.

    `by_owner` excludes generic owners; `generic_items` counts what was
    dropped for being generic, which is what lets the caller tell
    "unreadable" from "read fine, all group-owned"."""

    by_owner: Dict[str, List[str]] = field(default_factory=dict)
    candidate_lines: int = 0
    parsed_items: int = 0
    generic_items: int = 0
    # Shape-only sketches of lines we could not read (see `_shape`).
    unparsed_shapes: List[str] = field(default_factory=list)


def _shape(line: str, limit: int = 120) -> str:
    """Reduce a line to a content-free skeleton for debug logging.

    Action-item bodies are meeting content and must never reach the log,
    but the *shape* is exactly what a format diagnosis needs. Every run
    of uppercase letters becomes `A`, lowercase `a`, digits `9`;
    punctuation, markers and whitespace survive verbatim. So

        - [ ] Jane Doe — Send the Q3 deck

    logs as `- [ ] Aa Aa — Aa a A9 a`, which names the deviation (an em
    dash where a colon was expected, no bold) and names nobody.
    """
    out: List[str] = []
    prev = ""
    for ch in line[:limit]:
        if ch.isupper() and ch.isalpha():
            cls = "A"
        elif ch.isalpha():
            cls = "a"
        elif ch.isdigit():
            cls = "9"
        else:
            cls = ""
        if cls:
            if cls != prev:
                out.append(cls)
            prev = cls
        else:
            out.append(ch)
            prev = ""
    return "".join(out)


def _iter_item_lines(md: str) -> Iterable[Tuple[str, bool]]:
    """Yield (line, under_action_heading) for every non-heading line.

    `extract_action_items` emits one markdown blob holding "## Action
    Items", "## Decisions Made" and "## Open Questions". When such a
    heading structure is present we only read the action-items sections,
    so a decision bullet can never be mistaken for somebody's task. When
    the blob has no action-items heading at all, everything is in scope.
    """
    lines = md.splitlines()
    headings = [
        m.group("text") for m in (_HEADING_RE.match(ln) for ln in lines) if m
    ]
    has_action_heading = any(_ACTION_HEADING_RE.search(h) for h in headings)
    in_action = not has_action_heading
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            if has_action_heading:
                in_action = bool(_ACTION_HEADING_RE.search(m.group("text")))
            continue
        if in_action or not has_action_heading:
            yield line, in_action and has_action_heading


def _split_row(body: str) -> List[str]:
    return [c.strip() for c in body.split("|")]


def _table_columns(cells: Sequence[str]) -> Optional[Tuple[int, int]]:
    """If `cells` is a header row, return (owner_col, task_col)."""
    owner_col = task_col = None
    for i, cell in enumerate(cells):
        low = re.sub(r"[*_`]", "", cell).strip().lower().rstrip(":")
        if owner_col is None and low in _OWNER_HEADER_WORDS:
            owner_col = i
        elif task_col is None and low in _TASK_HEADER_WORDS:
            task_col = i
    if owner_col is None or task_col is None:
        return None
    return owner_col, task_col


def _read_owner_and_desc(
    content: str, allow_missing_separator: bool = False,
) -> Optional[Tuple[str, str, bool]]:
    """Read `content` (a list item's text, checkbox already stripped) as
    owner + description. Returns (owner, description, was_marked) or None.

    `allow_missing_separator` accepts `**Sam** send the deck` — a marked
    owner with no punctuation after it. Only the caller knows whether the
    line is item-shaped enough to risk that: it is allowed for checkbox
    bullets and for bullets under an "Action Items" heading, and refused
    for a bare bullet in unknown prose, where `**Note** this matters`
    would otherwise become an owner called "Note".
    """
    m = _MARKED_OWNER_RE.match(content)
    if m:
        owner = (m.group("bold") or m.group("bracket") or "").strip()
        desc = (m.group("desc") or "").strip()
        if not m.group("sep") and not allow_missing_separator:
            return None
        # `**Sam**` with no description is a heading-ish fragment, not
        # an item.
        if owner and desc:
            return owner, desc, True
        return None

    m = _UNMARKED_OWNER_RE.match(content)
    if m:
        # No bracket stripping here: a leading `[Sam]` already matched the
        # marked branch above, so a bracket surviving to this point is
        # part of the name — the calendar organiser form
        # "Roe, Pat Jr. [US-EMEA]" ends in one.
        owner = m.group("owner").strip()
        desc = m.group("desc").strip()
        if owner and desc and looks_like_person(owner):
            return owner, desc, False
    return None


def parse_action_items_by_owner(action_items_md: str) -> OwnerParse:
    """Tolerant read of `action_items` markdown into owner → tasks.

    Accepted shapes (all with optional leading indentation, `-`, `*`,
    `+`, `•` or `1.` markers, and an optional `[ ]` / `[x]` checkbox):

        - [ ] **[Sam]**: Send the deck          (what the prompt asks for)
        - [ ] **Sam**: Send the deck
        - [ ] **Sam** — Send the deck
        - [ ] [Sam] - Send the deck
        - [ ] Sam: Send the deck
        - [ ] Sam – Send the deck
          - [ ] Sam: Send the deck              (nested)
        1. **Sam**: Send the deck
        - **Sam**: Send the deck                (no checkbox)
        | Owner | Action |                      (markdown table, with or
        | Sam   | Send the deck |                without a header row)
    """
    result = OwnerParse()
    if not action_items_md or not action_items_md.strip():
        return result

    table_cols: Optional[Tuple[int, int]] = None
    in_table = False

    for line, under_action_heading in _iter_item_lines(action_items_md):
        if not line.strip():
            in_table = False
            table_cols = None
            continue

        owner = desc = None

        row = _TABLE_ROW_RE.match(line)
        if row:
            cells = _split_row(row.group("body"))
            if cells and all(
                _TABLE_RULE_CELL_RE.match(c) for c in cells if c
            ):
                # The |---|---| rule under a header. Not an item.
                in_table = True
                continue
            header = _table_columns(cells)
            if header and not in_table:
                table_cols, in_table = header, True
                continue
            in_table = True
            o_col, t_col = table_cols or (0, 1)
            if len(cells) > max(o_col, t_col):
                cand_owner = re.sub(r"[*_`\[\]]", "", cells[o_col]).strip()
                cand_desc = cells[t_col].strip()
                if cand_owner and cand_desc:
                    result.candidate_lines += 1
                    # With a header we trust the column; without one we
                    # only assume col 0 is the owner if it reads as a name.
                    if table_cols is not None or looks_like_person(cand_owner):
                        owner, desc = cand_owner, cand_desc
                    else:
                        if len(result.unparsed_shapes) < 8:
                            result.unparsed_shapes.append(_shape(line))
                        continue
                else:
                    continue
            else:
                continue
        else:
            in_table = False
            table_cols = None
            bullet = _BULLET_RE.match(line)
            if not bullet:
                continue
            content = bullet.group("rest")
            cb = _CHECKBOX_RE.match(content)
            has_checkbox = bool(cb)
            if cb:
                content = cb.group("rest")

            read = _read_owner_and_desc(
                content,
                allow_missing_separator=has_checkbox or under_action_heading,
            )
            marked = read[2] if read else False
            # A checkbox-less bullet outside an action-items heading is
            # only an action item if it names its owner explicitly —
            # otherwise every prose bullet in the document is a candidate.
            if not has_checkbox and not under_action_heading and not marked:
                continue

            result.candidate_lines += 1
            if not read:
                if len(result.unparsed_shapes) < 8:
                    result.unparsed_shapes.append(_shape(line))
                continue
            owner, desc, _ = read

            # An ASCII-hyphen separator is only trusted with a marked
            # owner; `_UNMARKED_OWNER_RE` never splits on it, so an
            # unmarked "Sam - send the deck" simply does not parse. That
            # is deliberate: " - " inside a description is far too common.

        if not owner or not desc:
            continue
        result.parsed_items += 1
        if is_generic_owner(owner):
            result.generic_items += 1
            continue
        result.by_owner.setdefault(owner, []).append(desc)

    return result


# ── Commitments source ───────────────────────────────────────────────


def owners_from_commitments(
    commitments: Iterable, alias_index: Optional[AliasIndex] = None,
) -> Tuple[Dict[str, List[str]], int]:
    """Group still-open commitments by resolved owner.

    Returns (by_owner, considered) where `considered` counts the
    still-open commitments we looked at — the caller needs it to tell
    "no commitments" from "commitments, all group-owned".

    Delivered and dismissed commitments are skipped: chasing somebody
    for something they already sent is worse than not writing at all.
    Multi-owner strings ("Mark/Sam") produce one entry per person, which
    is exactly what `resolve_owners()` is for.
    """
    by_owner: Dict[str, List[str]] = {}
    considered = 0
    for c in commitments:
        if getattr(c, "status", "awaiting") != "awaiting":
            continue
        desc = (getattr(c, "description", "") or "").strip()
        if not desc:
            continue
        considered += 1
        due = (getattr(c, "due_date_iso", "") or "").strip()
        if due:
            # Real extracted data, not an invented deadline — safe to
            # hand to the drafting prompt.
            desc = f"{desc} (Due: {due})"
        for person in resolve_owners(getattr(c, "owner", "") or "", alias_index):
            if is_generic_owner(person):
                continue
            by_owner.setdefault(person, []).append(desc)
    return by_owner, considered


# ── The plan ─────────────────────────────────────────────────────────


@dataclass
class DraftPlan:
    """What the platform backends act on."""

    owners: Dict[str, List[str]] = field(default_factory=dict)
    state: str = NO_ACTION_ITEMS
    source: str = ""          # "commitments" | "action_items" | ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.state == READY and bool(self.owners)


def _load_commitments(svc, session_id: str) -> Tuple[List, Optional[AliasIndex]]:
    """Read the session's commitments sidecar, if the service exists.

    Every failure mode here degrades to "no commitments" so the markdown
    fallback still runs — mirrors OwnerAliasStore's degrade-don't-raise
    contract."""
    commitments_svc = getattr(svc, "commitments_svc", None)
    if commitments_svc is None:
        return [], None
    try:
        commitments = commitments_svc.list_for_session(session_id)
    except Exception as e:
        logger.warning(
            f"Follow-up drafts: could not read commitments for "
            f"{session_id} ({e}); falling back to action_items markdown")
        return [], None
    alias_index = load_alias_index(getattr(svc, "owner_alias_store", None))
    return list(commitments or []), alias_index


def build_draft_plan(svc, session_id: str, session_data: dict) -> DraftPlan:
    """Decide who gets a follow-up email, from the best source available.

    Commitments first (structured owner, alias-resolved); the
    `action_items` markdown as a fallback. The returned state
    distinguishes the three empty cases so the UI can say which one
    happened instead of always blaming the model for not attributing.
    """
    commitments, alias_index = _load_commitments(svc, session_id)
    commitment_owners, commitments_considered = owners_from_commitments(
        commitments, alias_index)
    if commitment_owners:
        logger.info(
            f"Follow-up drafts: session {session_id} drafting from "
            f"{commitments_considered} open commitment(s) → "
            f"{len(commitment_owners)} owner(s)")
        return DraftPlan(owners=commitment_owners, state=READY,
                         source="commitments")

    action_items_md = (session_data.get("action_items") or "")
    parse = parse_action_items_by_owner(action_items_md)

    if parse.by_owner:
        if commitments_considered:
            logger.info(
                f"Follow-up drafts: session {session_id} had "
                f"{commitments_considered} open commitment(s) but no "
                f"individual owner among them; using action_items markdown")
        logger.info(
            f"Follow-up drafts: session {session_id} drafting from "
            f"action_items markdown → {len(parse.by_owner)} owner(s) "
            f"({parse.parsed_items} item(s) read, "
            f"{parse.generic_items} group-owned)")
        return DraftPlan(owners=parse.by_owner, state=READY,
                         source="action_items")

    # ── Nothing to draft. Which of the three is it? ──────────────────

    if parse.parsed_items or commitments_considered:
        # We read owners fine; every one of them was a group.
        logger.info(
            f"Follow-up drafts: session {session_id} — "
            f"{parse.parsed_items} action item(s) and "
            f"{commitments_considered} commitment(s) read, but every owner "
            f"is generic (team/all/everyone); nobody to address")
        return DraftPlan(state=GENERIC_OWNERS_ONLY,
                         message=STATE_MESSAGES[GENERIC_OWNERS_ONLY])

    if parse.candidate_lines:
        # Lines that look like action items exist and NOT ONE of them
        # parsed. That is a format problem, and saying "Claude didn't
        # attribute any items" here is the bug this module was written
        # to kill.
        logger.warning(
            f"Follow-up drafts: session {session_id} — "
            f"{parse.candidate_lines} action-item line(s) present, none "
            f"parseable into owner + task")
        # Shapes only, never bodies. See `_shape`.
        for sketch in parse.unparsed_shapes[:2]:
            logger.debug(f"Follow-up drafts: unparsed line shape: {sketch}")
        return DraftPlan(state=UNREADABLE_FORMAT,
                         message=STATE_MESSAGES[UNREADABLE_FORMAT])

    logger.info(
        f"Follow-up drafts: session {session_id} has no action items and "
        f"no open commitments, nothing to draft")
    return DraftPlan(state=NO_ACTION_ITEMS,
                     message=STATE_MESSAGES[NO_ACTION_ITEMS])


@dataclass
class DraftResult:
    """Return value of both platform `draft_follow_up_emails()`.

    `created` is what the old `int` return was; `state`/`source`/`message`
    are what the UI needs to stop collapsing three different outcomes
    into one toast."""

    created: int = 0
    state: str = NO_ACTION_ITEMS
    source: str = ""
    message: str = ""
    owners: int = 0

    @classmethod
    def from_plan(cls, plan: DraftPlan, created: int = 0) -> "DraftResult":
        return cls(created=created, state=plan.state, source=plan.source,
                   message=plan.message, owners=len(plan.owners))

    def to_dict(self) -> dict:
        return {
            "drafts_created": self.created,
            "state": self.state,
            "source": self.source,
            "message": self.message,
            "owners": self.owners,
        }
