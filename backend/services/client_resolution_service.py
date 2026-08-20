"""
Server-side client resolution for calendar-driven meetings.

WHY THIS EXISTS
---------------
An auto-recorded meeting used to land with `client == ""` and the user
had to reopen the session afterwards and tag it by hand. The only
resolver that existed (`suggestClientFromAttendees` in
`src/components/record-view.tsx`) could never help, for two independent
reasons:

  1. It keys entirely on attendee EMAIL DOMAINS and bails immediately
     when there are none (`if (meetingDomains.size === 0) return null`).
     Extension-sourced calendar events carry attendee NAMES only —
     `Session.attendees` is a `List[str]` and the Chrome extension's
     scraped event object has no attendee emails at all — so for anyone
     whose calendar comes from the extension it returned null every
     single time.
  2. It is frontend-only, invoked from `useMeeting` when the user clicks
     **Use**. Auto-record starts server-side (`auto_record_service` →
     `server._auto_record_start`) and never reaches any of it.

Meanwhile the signal was usually sitting in plain sight in the meeting
SUBJECT — the client's name is in the meeting title, and that client is
already in the user's client list.

SIGNAL ORDER (strongest first)
------------------------------
  1. **Subject match against known client names.** Directly stated,
     source-blind (works for extension events, which have no emails at
     all), and it is the signal that was actually present in the field
     report. Candidates come from `/clients/config` plus every distinct
     `client` value across existing sessions, so a client the user has
     only ever typed onto a session still counts.
  2. **Attendee email-domain history** — the existing frontend
     algorithm, ported verbatim in behaviour (own-domain detection by
     "the domain appearing in the most sessions", per-client overlap
     scoring, tie → no answer). It is second because it needs emails,
     which only local-calendar meetings carry; it is kept because it
     already works for those users and must not regress.
  3. **Project** — once a client is resolved, the most recent project
     previously used under that client. Mirrors what
     `meeting-brief-modal.tsx` already does client-side. Never resolves
     a project without a client.

BOUNDARY RULE
-------------
Client names are matched on TOKENS, not substrings, and not with
`\\b` — a bug of exactly that shape shipped in this repo (a `\\bRicoh\\b`
pattern failed to match `transcript_Ricoh`, because `_` is a word
character to the regex engine, so it is NOT a word boundary). Both the
subject and each candidate name are canonicalised by `_canon`: casefold,
every non-alphanumeric character becomes a separator, and every
letter↔digit transition becomes a separator too. The comparison is then
whole-token containment of one space-padded string in the other.

Consequences, all of them deliberate:
  * `_`, `-`, `/`, `.`, `|` and whitespace are separators, so
    `transcript_Ricoh`, `Acme-Globex` and `Acme/Globex` all match.
  * digits are separators, so `Acme2026` matches `Acme`…
  * …but digits are kept as their own tokens, so `Initech 360` stays a
    distinct, longer name than `Initech` rather than collapsing onto it.
  * a short name is never matched inside a longer word: `Zorg` does not
    match `Zorgeous`.

AMBIGUITY IS NEVER GUESSED
--------------------------
If two genuinely different clients match the subject, NOTHING is tagged
and the reason is recorded. A wrongly-tagged session is worse than an
untagged one: it silently files a client conversation under another
client, and `export_worker` will then copy the artifacts into that other
client's Designated Folder. The one case where several matches collapse
to one answer is nesting — `Acme` and `Acme Financial` both matching is
one client, and the LONGEST name wins.

PROVENANCE
----------
The result carries `method` (`subject_match` / `domain_history` /
`ambiguous` / `none`) and a human-readable `detail`. Both are persisted
onto the session (`client_source`, `client_source_detail`) so the UI can
say WHERE an auto-applied client came from. An auto-applied value with
no explanation is one the user has to re-verify by hand, which defeats
the point of applying it.

This module is pure: no I/O, no LLM, no imports from `server`. Callers
pass in the client list and the session summaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


# ── canonicalisation / boundary rule ─────────────────────────────────

def _canon(text: str) -> str:
    """Canonical token string: casefolded, alphanumeric runs separated
    by single spaces, with letter↔digit transitions split apart.

        "transcript_Ricoh"  -> "transcript ricoh"
        "Acme-Globex | MVP" -> "acme globex mvp"
        "Acme2026"          -> "acme 2026"
        "Initech 360"       -> "initech 360"

    Everything that is not a letter or a digit is a separator, which is
    the whole point: `\\b` treats `_` as a word character and would not
    have split the first example.
    """
    out: List[str] = []
    prev: Optional[str] = None  # "a" (alpha), "d" (digit) or None (sep)
    for ch in text or "":
        if ch.isalpha():
            cls = "a"
        elif ch.isdigit():
            cls = "d"
        else:
            cls = None
        if cls is None:
            if out and out[-1] != " ":
                out.append(" ")
            prev = None
            continue
        if prev is not None and cls != prev:
            out.append(" ")
        out.append(ch.casefold())
        prev = cls
    return "".join(out).strip()


def _padded(text: str) -> str:
    """Canonical form wrapped in spaces, so `in` is a whole-token test."""
    return " " + _canon(text) + " "


def subject_contains_client(subject: str, client: str) -> bool:
    """True when `client` appears in `subject` as whole tokens.

    Exposed (rather than kept private) because this is the boundary rule
    itself, and it is the thing worth pinning down in a test.
    """
    c = _canon(client)
    if not c:
        return False
    return _padded(client) in _padded(subject)


# ── result type ──────────────────────────────────────────────────────

#: `method` values. `none` and `ambiguous` both mean "nothing tagged";
#: they are distinct so the UI can explain WHY nothing was tagged.
METHOD_SUBJECT = "subject_match"
METHOD_DOMAIN = "domain_history"
METHOD_AMBIGUOUS = "ambiguous"
METHOD_NONE = "none"


@dataclass(frozen=True)
class ClientResolution:
    """Outcome of resolving a calendar meeting to a client.

    `client` is "" whenever nothing was resolved — including the
    ambiguous case, which is a deliberate refusal, not a failure.
    """
    client: str = ""
    project: str = ""
    method: str = METHOD_NONE
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.client)

    def to_dict(self) -> dict:
        return {
            "client": self.client,
            "project": self.project,
            "method": self.method,
            "detail": self.detail,
        }


# ── candidate clients ────────────────────────────────────────────────

def known_client_names(
    client_configs: Optional[dict] = None,
    sessions: Optional[Sequence[dict]] = None,
) -> List[str]:
    """Every client name we could possibly tag, in display casing.

    Union of the configured clients (`/clients/config` — includes ones
    created but never yet used on a session) and every distinct `client`
    value across existing session summaries. De-duplicated on the
    canonical form so "Acme Corp" and "acme corp " are one candidate;
    the configured display name wins when both sources have it, since
    that is the casing the user chose.
    """
    out: List[str] = []
    seen: Set[str] = set()

    def _add(name: object) -> None:
        text = str(name or "").strip()
        key = _canon(text)
        if not key or key in seen:
            return
        seen.add(key)
        out.append(text)

    for key, cfg in (client_configs or {}).items():
        display = ""
        if isinstance(cfg, dict):
            display = str(cfg.get("display_name") or "").strip()
        else:  # ClientConfig dataclass
            display = str(getattr(cfg, "display_name", "") or "").strip()
        _add(display or key)

    for s in sessions or []:
        if isinstance(s, dict):
            _add(s.get("client"))

    return out


# ── signal 1: subject match ──────────────────────────────────────────

def match_clients_in_subject(
    subject: str,
    candidates: Iterable[str],
) -> List[str]:
    """All candidate clients whose name appears in `subject` as whole
    tokens, with nested matches collapsed onto the longest name.

    "Nested" means one canonical name is a whole-token sub-phrase of
    another — `Acme` inside `Acme Financial`. Those are the SAME client
    written at two levels of specificity, so the longer one wins. Two
    matches that are not nested (`Acme` and `Globex` in a joint-call
    invite) are genuinely different clients and BOTH are returned, so
    the caller can refuse to guess.

    Returned longest-first.
    """
    hits: List[Tuple[str, str]] = []  # (display name, canonical)
    seen: Set[str] = set()
    for cand in candidates:
        c = _canon(cand)
        if not c or c in seen:
            continue
        if _padded(cand) in _padded(subject):
            seen.add(c)
            hits.append((str(cand).strip(), c))

    # Drop any hit that is a whole-token sub-phrase of another hit.
    kept = [
        (name, c) for name, c in hits
        if not any(other != c and f" {c} " in f" {other} "
                   for _, other in hits)
    ]
    kept.sort(key=lambda pair: (-len(pair[1]), pair[0].casefold()))
    return [name for name, _ in kept]


# ── signal 2: attendee email-domain history ──────────────────────────

def _domains(addresses: Iterable[object]) -> Set[str]:
    """Email domains from a list of attendee strings.

    Attendees are free-form: extension-sourced events give display names
    with no `@` at all, and those simply contribute nothing here — which
    is precisely why the subject signal has to exist.
    """
    out: Set[str] = set()
    for a in addresses or []:
        text = str(a or "")
        at = text.rfind("@")
        if at < 0:
            continue
        domain = text[at + 1:].strip().strip(">").strip().lower()
        if domain:
            out.add(domain)
    return out


def match_client_by_domain_history(
    attendees: Sequence[object],
    sessions: Sequence[dict],
) -> Optional[str]:
    """Port of `suggestClientFromAttendees` (record-view.tsx).

    Same algorithm, same refusals — kept behaviour-identical on purpose
    so the users it already works for see no change:
      * external domains only. Every meeting includes the user's own
        work address, so counting it would make every meeting look like
        every client. The user's own domain is self-calibrating: it is
        the domain appearing across the most sessions, which needs no
        configuration and works at any employer.
      * score each client by how many prior sessions share an external
        domain with this meeting; highest wins.
      * a tie is not an answer — return None and let the caller fall
        through to "no client".
    """
    meeting_domains = _domains(attendees)
    if not meeting_domains:
        return None

    sessions_with_domain: dict[str, int] = {}
    for s in sessions or []:
        for d in _domains((s or {}).get("attendees") or []):
            sessions_with_domain[d] = sessions_with_domain.get(d, 0) + 1
    own_domain = None
    if sessions_with_domain:
        own_domain = max(sessions_with_domain.items(), key=lambda kv: kv[1])[0]

    scores: dict[str, int] = {}
    for s in sessions or []:
        client = str((s or {}).get("client") or "").strip()
        if not client:
            continue
        session_domains = _domains((s or {}).get("attendees") or [])
        overlap = sum(
            1 for d in meeting_domains
            if d != own_domain and d in session_domains
        )
        if overlap > 0:
            scores[client] = scores.get(client, 0) + overlap

    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None  # tie → punt, same as the frontend
    return ranked[0][0]


# ── signal 3: project ────────────────────────────────────────────────

def most_recent_project(client: str, sessions: Sequence[dict]) -> str:
    """The project most recently used under `client`, or "".

    Mirrors meeting-brief-modal.tsx:91-107 — newest `started_at` wins.
    Matched case-insensitively so a client typed with different casing
    on an old session still contributes its project.
    """
    if not client:
        return ""
    key = _canon(client)
    best_started = ""
    best_project = ""
    for s in sessions or []:
        if _canon(str((s or {}).get("client") or "")) != key:
            continue
        project = str((s or {}).get("project") or "").strip()
        if not project:
            continue
        started = str((s or {}).get("started_at") or "")
        if started >= best_started:
            best_started = started
            best_project = project
    return best_project


# ── the resolver ─────────────────────────────────────────────────────

def resolve_client(
    *,
    subject: str = "",
    attendees: Optional[Sequence[object]] = None,
    client_configs: Optional[dict] = None,
    sessions: Optional[Sequence[dict]] = None,
) -> ClientResolution:
    """Resolve a calendar meeting to a client, strongest signal first.

    Never raises for ordinary bad input — the callers (the record-start
    path and the auto prep-brief loop) treat tagging as strictly
    additive, and a tagging failure must never cost someone a recording.
    """
    sessions = list(sessions or [])
    candidates = known_client_names(client_configs, sessions)

    # 1 — subject match.
    matches = match_clients_in_subject(subject or "", candidates)
    if len(matches) == 1:
        client = matches[0]
        return ClientResolution(
            client=client,
            project=most_recent_project(client, sessions),
            method=METHOD_SUBJECT,
            detail=f"Matched “{client}” in the meeting title.",
        )
    if len(matches) > 1:
        listed = ", ".join(f"“{m}”" for m in matches)
        return ClientResolution(
            method=METHOD_AMBIGUOUS,
            detail=(
                f"Meeting title matches {len(matches)} clients ({listed}); "
                f"left untagged rather than guessing."
            ),
        )

    # 2 — attendee email-domain history.
    by_domain = match_client_by_domain_history(attendees or [], sessions)
    if by_domain:
        return ClientResolution(
            client=by_domain,
            project=most_recent_project(by_domain, sessions),
            method=METHOD_DOMAIN,
            detail=(
                f"Matched “{by_domain}” from attendee email domains "
                f"seen on earlier sessions."
            ),
        )

    return ClientResolution(
        method=METHOD_NONE,
        detail="No client name in the meeting title and no matching "
               "attendee email domains.",
    )
