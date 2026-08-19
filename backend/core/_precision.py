"""The no-invented-precision rule — one text, referenced by every prompt.

v2.35.1 added a DATE ANCHOR to the summarizer because the model kept
inventing dates: a transcript said "come October" with no year stated
anywhere, and the summary asserted a year two years in the PAST, twice,
once as a section heading. The anchor fixed that. It did not fix the
defect, because the defect was never specifically about dates.

The very next summary off the same recording produced three more
instances of the same shape:

  1. A section stated "she identified SEVEN candidate intents" and then
     listed six. The transcript contained exactly six. The count was
     manufactured out of nothing and contradicted the list directly
     underneath it.
  2. An action item was given the target "By end of meeting". The
     source gave no timing at all for that item.
  3. A demo was scheduled for a named calendar week. The source said
     only "rolling out this week and next week" and "a week or two
     out" — a vague window sharpened into a specific one.

The generalisation is: **adding precision the source did not carry**.
A date is one instance. A count, a deadline, an identifier and an
attribution are four more, and there will be others. So the rule is
stated once, here, as a positive principle plus the four concrete
classes it has been observed to break in — and every prompt builder in
the application references THIS text rather than carrying its own
paraphrase. Fifteen paraphrases is how this class of bug survives: the
paraphrases drift, and the weakest one becomes the one that ships.

`date_anchor()` moved here from core/summarizer.py unchanged (byte for
byte — it works, and its resolution half is genuinely date-specific)
so that the anchor and the general rule compose from one place;
`grounding_rules()` is the composition every caller with a date uses.

Kept dependency-free on purpose: services/ modules reference these
constants without dragging in the anthropic SDK that core.summarizer
imports at module load.
"""

from __future__ import annotations

# ── The rule ────────────────────────────────────────────────────────
#
# One header, one lead, four clauses, one close. Callers select which
# clauses apply to their output shape (see `no_invented_precision`);
# nobody restates the wording.

PRECISION_HEADER = (
    "\n\n=== NO INVENTED PRECISION — read this before you write ===\n"
)

# The positive principle. Stated first and stated plainly, because it
# is what makes the rule generalise past the four listed classes to
# whatever the model invents next. A list of banned mistakes only ever
# bans the mistakes already made.
PRECISION_LEAD = (
    "Write only what the source material in front of you actually "
    "carries. Never add precision the source did not have."
)

PRECISION_COUNTS = (
    "- COUNTS AND QUANTITIES. Never state a number of items, people, "
    "branches, options or occurrences that is not stated in the source "
    "or literally countable from it. If you enumerate a list, any count "
    "you give must equal the number of items you actually wrote. Prefer "
    "omitting a count to guessing one."
)

PRECISION_TIMING = (
    "- TIMINGS AND DEADLINES. If the source gave no timing, say the "
    "timing was not specified. Never manufacture a deadline, and never "
    "sharpen a vague one: \"next week\" must not become a specific "
    "date, \"a week or two\" must not become a named week."
)

PRECISION_IDENTIFIERS = (
    "- IDENTIFIERS AND SPECIFICS. Never introduce a name, number, "
    "system, product, version or identifier that appears nowhere in "
    "the source."
)

PRECISION_ATTRIBUTION = (
    "- ATTRIBUTION. Never attribute a statement, question, decision or "
    "action to a participant who is not shown saying it."
)

# The real fabrications landed in a section heading and a table cell,
# so the close names those surfaces out loud rather than trusting
# "everything you write" to cover them.
PRECISION_CLOSE = (
    "This applies to section headings, table cells and JSON field "
    "values exactly as it applies to prose. An unqualified or absent "
    "detail is correct; an invented one is a factual error the reader "
    "cannot catch."
)

# Compressed single-paragraph form for latency-sensitive prompts that
# re-send the whole instruction on every tick. Same four classes, same
# closing principle, roughly a quarter of the tokens.
PRECISION_COMPACT = (
    "\n\nNO INVENTED PRECISION: state only what the transcript actually "
    "carries. Never invent a count, a deadline, a name, a number or a "
    "system that is not in it; never sharpen a vague timing into a "
    "specific one; never attribute anything to someone not shown "
    "saying it. An absent detail is correct; an invented one is a "
    "factual error the reader cannot catch."
)


def no_invented_precision(
    *,
    counts: bool = True,
    timing: bool = True,
    identifiers: bool = True,
    attribution: bool = True,
    compact: bool = False,
) -> str:
    """Render the rule for one call site.

    Every flag defaults True: the full block is the right answer unless
    a builder's output shape makes a clause meaningless, and defaulting
    to "on" means a new prompt builder is covered by construction
    rather than by somebody remembering.

    `compact=True` returns the one-paragraph form and ignores the
    per-clause flags — it is a whole-rule/no-rule choice for prompts
    that pay the token cost repeatedly.
    """
    if compact:
        return PRECISION_COMPACT
    clauses = [
        text for text, wanted in (
            (PRECISION_COUNTS, counts),
            (PRECISION_TIMING, timing),
            (PRECISION_IDENTIFIERS, identifiers),
            (PRECISION_ATTRIBUTION, attribution),
        ) if wanted
    ]
    if not clauses:
        return ""
    return (
        PRECISION_HEADER
        + PRECISION_LEAD
        + "\n\n"
        + "\n".join(clauses)
        + "\n\n"
        + PRECISION_CLOSE
        + "\n"
    )


def json_timing_note(field: str, container: str = "object") -> str:
    """Reconcile the TIMINGS clause with a JSON contract.

    "Say the timing was not specified" is prose advice, and a builder
    whose whole output is a JSON object has no prose to say it in —
    left unreconciled it invites the model to editorialise inside a
    string field, or worse, outside the JSON entirely. The empty string
    IS how these schemas say "not specified", so spell that out.

    (The date anchor already needed exactly this reconciliation for
    `extract_structured`; the general timing rule needs it everywhere
    the same shape occurs.)
    """
    return (
        f"\n\nThis is a JSON contract, so \"the timing was not "
        f"specified\" is expressed as an empty string, not as prose. "
        f"For \"{field}\", an unstated or unclear deadline is \"\" — "
        f"never a guessed date. Still output nothing but the JSON "
        f"{container}."
    )


# ── The date anchor (moved here unchanged from core/summarizer.py) ───


def fmt_anchor_date(anchor: str) -> str:
    """Render an anchor date for a prompt.

    Accepts an ISO date or datetime (``2026-08-19``,
    ``2026-08-19T14:03:11.412``) and returns
    ``"Wednesday, 19 August 2026 (2026-08-19)"``. The weekday matters:
    "next Tuesday" is only resolvable if the model knows what day the
    anchor was. Anything unparseable is passed through verbatim rather
    than dropped — a fuzzy anchor still beats none.
    """
    raw = (anchor or "").strip()
    if not raw:
        return ""
    date_part = raw.split("T", 1)[0].strip()
    try:
        from datetime import date as _date_cls
        d = _date_cls.fromisoformat(date_part)
    except (ValueError, TypeError):
        return raw
    return f"{d.strftime('%A, %d %B %Y')} ({d.isoformat()})"


def date_anchor(anchor: str, kind: str = "meeting") -> str:
    """Pin every date the model writes to a known real-world date.

    Field repro (2026-08): a 43-minute recording contained one bare
    relative reference — a speaker saying "come October", with no year
    stated anywhere in the call. The summary asserted a year two years
    in the PAST, twice, once as a section heading. Nothing in the prompt
    carried the meeting's own date, so the model had no anchor for the
    relative reference and supplied a year from nowhere. A summary that
    gets forwarded to a customer turned a deadline six weeks out into a
    missed one.

    Two rules do the work, and they have to be stated together:
    RESOLUTION (relative references resolve forward from the anchor) and
    the PROHIBITION (never emit a date component that is neither stated
    in the source nor derivable from the anchor). Resolution alone
    invites the model to "helpfully" pin down dates it is guessing at;
    the prohibition alone leaves "come October" unresolvable.

    This is the date-shaped instance of the general no-invented-
    precision rule above. It is kept as its own block rather than
    folded into the TIMINGS clause because only the date case has a
    RESOLUTION half — an anchor to compute against — and only the date
    case can be checked arithmetically. The two compose (see
    `grounding_rules`); the general rule covers the undated commitment
    the anchor cannot resolve.

    `kind` selects the lead sentence — "meeting" for a transcript being
    summarized, "today" for a forward-looking brief generated now.
    Returns "" for an empty anchor so every caller can pass through
    whatever it has and callers with no date emit the prompt they
    emitted before.
    """
    shown = fmt_anchor_date(anchor)
    if not shown:
        return ""
    lead = (
        f"This meeting took place on {shown}."
        if kind == "meeting"
        else f"Today's date is {shown}."
    )
    return (
        "\n\n=== DATE ANCHOR — read this before you write any date ===\n"
        f"{lead}\n"
        "Resolve every relative time reference against that date. "
        '"Come October", "next Tuesday", "end of quarter", "in two '
        'weeks", "later this year" all mean the next such point on or '
        "after the anchor date above — never an earlier one.\n\n"
        "NEVER state a year, month or day that is not either stated "
        "explicitly in the source material or derived from the anchor "
        "date above. Specifically:\n"
        "- A bare month with no year stays bare, or is resolved forward "
        'from the anchor. "Come October" becomes "October" or the '
        "October on or after the anchor date — never a different "
        "year.\n"
        "- Do not add a year, quarter or day-of-month to a source "
        "reference that did not carry one just to make it look "
        "precise.\n"
        "- Do not invent a date for an undated commitment. If the "
        "source did not say when, write that the timing was not "
        "specified.\n"
        "- This applies to section headings and table cells exactly as "
        "it applies to prose.\n"
        "An unqualified date is correct. An invented one is a factual "
        "error the reader cannot catch.\n"
    )


def grounding_rules(
    anchor: str = "",
    kind: str = "meeting",
    *,
    counts: bool = True,
    timing: bool = True,
    identifiers: bool = True,
    attribution: bool = True,
) -> str:
    """The date anchor (when there is a date) plus the general rule.

    The single entry point for prompt builders that take a transcript
    or prior notes. An empty/unparseable `anchor` drops only the anchor
    half — the no-invented-precision rule does not depend on knowing
    what day it is, and a session with no recorded date is exactly the
    one most likely to get a date invented for it.
    """
    return date_anchor(anchor, kind) + no_invented_precision(
        counts=counts, timing=timing,
        identifiers=identifiers, attribution=attribution,
    )
