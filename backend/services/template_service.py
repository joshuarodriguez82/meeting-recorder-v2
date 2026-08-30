"""
Per-user Summary Templates (stored in USER_DATA_DIR/summary_templates.json).

Each template is a `{name, prompt}` pair. The summarizer's summarize() call
looks up the template by name and uses its prompt verbatim (the
`_with_user_notes` wrapper still handles the notes-+-transcript envelope).

On first call the store is seeded with the five built-in defaults so the
user has something to edit rather than a blank slate. Defaults can be
overridden in place; each entry keeps a `default_prompt` record so the
user can restore the original at any time via `reset(name)`. Deleting a
template that shipped as a default re-hides it from the UI but keeps the
default_prompt around for future restore.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


# Seeded on first launch. Kept here (not in summarizer.py) so the
# summarizer has no compile-time coupling to the template set — anything
# beyond these five is purely user data.
# Shared appendix every default template ends with. Adding it once at
# the top means a single edit keeps the visuals-handling policy in sync
# across every built-in template. User-created templates inherit it
# only if the user types it themselves — by design, since custom
# templates may want different rules.
_VISUALS_DIRECTIVE = (
    " If screenshots are attached to this meeting, treat them as "
    "primary evidence alongside the transcript and reference specific "
    "ones inline when relevant (e.g. \"as shown in screenshot 1\"). "
    "End the summary with a `## Visuals` section that names each "
    "screenshot in order and briefly describes what it shows; skip "
    "this section entirely when no screenshots are attached."
)


DEFAULT_TEMPLATES: Dict[str, str] = {
    "General": (
        "Please summarize this meeting transcript. "
        "Include: key topics discussed, decisions made, "
        "action items, and any follow-ups needed."
        + _VISUALS_DIRECTIVE
    ),
    "Requirements Gathering": (
        "This is a requirements gathering meeting. Summarize with focus on: "
        "1) Business context and problem statement discussed, "
        "2) Functional requirements identified (what the system should do), "
        "3) Non-functional requirements (performance, security, scalability), "
        "4) Constraints and assumptions mentioned, "
        "5) Open questions that need follow-up, "
        "6) Stakeholder priorities and any conflicts between requirements."
        + _VISUALS_DIRECTIVE
    ),
    "Design Review": (
        "This is a design/architecture review meeting. Summarize with focus on: "
        "1) Solution overview and architecture discussed, "
        "2) Design decisions made and their rationale, "
        "3) Trade-offs considered, "
        "4) Risks and concerns raised, "
        "5) Feedback and requested changes, "
        "6) Next steps and action items."
        + _VISUALS_DIRECTIVE
    ),
    "Sprint Planning": (
        "This is a sprint planning meeting. Summarize with focus on: "
        "1) Sprint goal agreed upon, "
        "2) Stories/tasks committed to with owners, "
        "3) Capacity concerns or blockers raised, "
        "4) Dependencies identified, "
        "5) Carry-over items from previous sprint, "
        "6) Key risks to sprint delivery."
        + _VISUALS_DIRECTIVE
    ),
    "Stakeholder Update": (
        "This is a stakeholder update meeting. Summarize with focus on: "
        "1) Project status and progress reported, "
        "2) Milestones achieved or missed, "
        "3) Risks and issues escalated, "
        "4) Decisions requested from stakeholders, "
        "5) Decisions made by stakeholders, "
        "6) Next steps and timeline updates."
        + _VISUALS_DIRECTIVE
    ),
    # ── Delivery-phase set ──────────────────────────────────────────
    # The five above grew out of pre-sales SA work. The six below cover
    # the post-SOW lifecycle — the delivery engineers who build, test,
    # cut over, and stabilize what pre-sales scoped. Ordering here is
    # roughly the order the meetings happen in a real engagement.
    "Delivery Kickoff": (
        "This is a delivery kickoff / pre-sales-to-delivery handoff "
        "meeting. Summarize with focus on: "
        "1) Scope as confirmed against the SOW — call out anything the "
        "team flagged as differing from what was sold, "
        "2) Explicitly out-of-scope items mentioned (these prevent scope "
        "creep disputes later), "
        "3) Assumptions from pre-sales that delivery must validate, with "
        "who owns validating each, "
        "4) Environment, credential, and access requests raised and who "
        "is providing them, "
        "5) Roles and responsibilities agreed (customer side and "
        "delivery side), "
        "6) Key dates committed: phase boundaries, environment "
        "availability, go-live target, "
        "7) Risks raised in the handoff and open questions for pre-sales."
        + _VISUALS_DIRECTIVE
    ),
    "Technical Working Session": (
        "This is a technical working/build session between engineers. "
        "Summarize with focus on: "
        "1) Integration points discussed — systems, endpoints, "
        "authentication methods, and data formats agreed, "
        "2) Configuration decisions made with their exact agreed values "
        "where stated (names, IDs, limits, timeouts), "
        "3) Data mappings or transformations agreed, "
        "4) Technical blockers raised, who owns unblocking each, and by "
        "when, "
        "5) Access or credential requests made in the session, "
        "6) Items parked for a spike, follow-up session, or "
        "architecture decision, "
        "7) Anything agreed verbally that must be written into design "
        "documentation."
        + _VISUALS_DIRECTIVE
    ),
    "UAT & Defect Triage": (
        "This is a UAT or defect triage meeting. Summarize with focus "
        "on: "
        "1) Defects reviewed — for each: identifier (if stated), a "
        "one-line description, severity/priority as agreed in the "
        "meeting, the owner, and the expected fix or retest date, "
        "2) Defects disputed as out of scope, working as designed, or "
        "actually change requests — and how each dispute was resolved, "
        "3) New defects raised during the session, "
        "4) Retest criteria and who performs the retest, "
        "5) Overall UAT progress: pass/fail counts or completion "
        "percentages if mentioned, "
        "6) Anything blocking test execution itself (environment, data, "
        "access), "
        "7) Exit-criteria implications: does anything discussed move the "
        "UAT completion date?"
        + _VISUALS_DIRECTIVE
    ),
    "Go-Live Readiness": (
        "This is a go-live readiness / cutover planning meeting. "
        "Summarize with focus on: "
        "1) Go/no-go criteria reviewed and the current status of each, "
        "2) The go/no-go decision itself if one was made, who made it, "
        "and any conditions attached, "
        "3) Cutover runbook items discussed: sequence, owners, timing, "
        "and dependencies (including carrier/port dates or third-party "
        "cutovers), "
        "4) Rollback plan: triggers, procedure, and who has authority to "
        "invoke it, "
        "5) Freeze windows and communication plans agreed, "
        "6) Open readiness gaps with owners and dates, "
        "7) Sign-offs given, pending, or refused — and by whom."
        + _VISUALS_DIRECTIVE
    ),
    "Hypercare Review": (
        "This is a hypercare / post-launch stabilization review. "
        "Summarize with focus on: "
        "1) Issues reported since launch, grouped by severity, with "
        "status and owner for each, "
        "2) Trends: is issue volume rising, falling, or flat — and any "
        "recurring root causes, "
        "3) Progress against hypercare exit criteria and the expected "
        "exit date, "
        "4) Items being handed to the ongoing support/operations team, "
        "including knowledge gaps or documentation still owed, "
        "5) Fixes deployed during hypercare and their verification "
        "status, "
        "6) Escalations made or threatened by the customer, "
        "7) Actions with owners and dates."
        + _VISUALS_DIRECTIVE
    ),
    "Change Request Scoping": (
        "This is a change request scoping discussion. Summarize with "
        "focus on: "
        "1) The change being requested and the business reason given "
        "for it, "
        "2) The case for why it is in scope or out of scope of the "
        "current SOW, as argued by each side, "
        "3) The effort discussed: sizing, level-of-effort estimates, or "
        "resource impacts mentioned, "
        "4) Schedule impact if the change is accepted, "
        "5) Alternatives or phasing options proposed, "
        "6) What was actually agreed: proceed, decline, defer, or "
        "estimate formally — and who approves next, "
        "7) Anything the delivery team committed to investigate before "
        "the next conversation."
        + _VISUALS_DIRECTIVE
    ),

    # ── Account management ──────────────────────────────────────────
    #
    # Third wave. The SA templates specify a system; the delivery ones
    # build it; these sell it. An account manager's meetings turn on
    # commercial facts — who signs, what the budget cycle is, which
    # competitor is incumbent, what was conceded — and an SA-shaped
    # summary of a pricing call faithfully records the architecture and
    # loses the discount that was verbally offered.
    #
    # NAMING, deliberately: this set has no "Discovery". That name is
    # taken by an SA meeting about requirements, and an AM's first call
    # is about qualification. Same word, different meeting; merging them
    # would lose whichever half wasn't being written that week. See
    # tests/test_sales_templates.py.
    "Qualification Call": (
        "This is a qualification call with a prospective or existing "
        "customer. Summarize with focus on: "
        "1) The problem in the customer's own words, and what it is "
        "costing them — quote figures, volumes or deadlines they gave, "
        "2) Their current environment and vendors, including anything "
        "said about how it was bought or when it renews, "
        "3) The decision process: who has authority to sign, who "
        "influences, what steps a purchase has to pass through, "
        "4) Budget signals: whether money is allocated, which cycle it "
        "sits in, any number stated or deflected, "
        "5) Timeline and what is driving it — a compelling event, a "
        "contract end, an audit, a board commitment, "
        "6) Any competitor or incumbent named, and how the customer "
        "spoke about them, "
        "7) Objections or hesitations raised, verbatim where possible, "
        "8) What we committed to send or do next, and by when. "
        "Where the customer was asked a qualifying question and did not "
        "answer it, say so explicitly rather than leaving it out — an "
        "unanswered question is the most useful thing in this summary."
        + _VISUALS_DIRECTIVE
    ),
    "Executive Briefing": (
        "This is an executive briefing. The audience is senior and the "
        "content is business outcomes, not architecture. Summarize with "
        "focus on: "
        "1) The business outcome the executives said they care about, "
        "in their language, "
        "2) Their stated priorities for the year or quarter, and where "
        "this initiative sits among them, "
        "3) Who in the room is the sponsor, who is the sceptic, and "
        "what each of them pushed on, "
        "4) Concerns raised about risk, disruption, timing or cost, "
        "5) Any commitment, direction or preference an executive stated "
        "— these carry more weight than anything else in the meeting, "
        "6) Questions asked that we could not answer in the room, "
        "7) The agreed next step and who owns it on the customer side. "
        "Keep technical detail out unless an executive raised it "
        "themselves."
        + _VISUALS_DIRECTIVE
    ),
    "Solution Demo": (
        "This is a solution demonstration. What matters is the "
        "customer's reaction, not the walkthrough. Summarize with focus "
        "on: "
        "1) What was actually shown, briefly, "
        "2) Reaction by capability: what drew interest, what drew "
        "silence, what drew a visible objection — attribute reactions "
        "to people where the transcript makes it clear, "
        "3) Objections raised, verbatim where possible, and how they "
        "were handled in the room, "
        "4) Any gap identified between what was shown and what the "
        "customer needs, and whether it was acknowledged as a gap, "
        "5) Comparisons the customer drew to another product or their "
        "current tooling, "
        "6) Feature requests or 'can it do X' questions, flagged as "
        "answered or unanswered, "
        "7) What we agreed to follow up with, including anything we "
        "promised to prove."
        + _VISUALS_DIRECTIVE
    ),
    "Pricing & Commercial": (
        "This is a pricing or commercial discussion. Precision matters "
        "more here than anywhere else — a number said out loud in this "
        "meeting can end up in a contract. Summarize with focus on: "
        "1) Every figure discussed, with what it covers and whether it "
        "was presented as firm, indicative or a placeholder, "
        "2) The commercial model discussed: fixed price, T&M, "
        "subscription, per-seat, phased, "
        "3) Any concession, discount or extra scope offered — by whom, "
        "under what condition, and whether it was accepted, deferred or "
        "refused, "
        "4) Procurement path: the approval steps named, who signs, what "
        "paperwork or vendor process is required, "
        "5) Budget cycle and funding constraints mentioned, "
        "6) Contractual points raised — terms, liability, payment "
        "milestones, termination, "
        "7) What each side committed to produce, and by when. "
        "Quote figures exactly as stated. Where a number was implied but "
        "not stated, say that it was implied rather than recording it as "
        "agreed."
        + _VISUALS_DIRECTIVE
    ),
    "Account Review / QBR": (
        "This is an account review or quarterly business review with an "
        "existing customer. Summarize with focus on: "
        "1) How the customer characterised the relationship and the "
        "work delivered so far — praise and complaints both, quoted, "
        "2) Adoption: what is actually being used, by whom, and any "
        "usage or volume figures given, "
        "3) Outstanding issues, escalations or unmet expectations, and "
        "their current state, "
        "4) Risk signals: budget pressure, reorganisation, sponsor "
        "change, dissatisfaction, evaluation of alternatives — say "
        "plainly if none appeared, "
        "5) Expansion signals: new initiatives, adjacent teams, "
        "problems mentioned in passing that we could address, "
        "6) Renewal or contract dates mentioned, "
        "7) Commitments made on both sides before the next review."
        + _VISUALS_DIRECTIVE
    ),
    "Competitive Displacement": (
        "This meeting concerns replacing an incumbent vendor or winning "
        "against a named competitor. Summarize with focus on: "
        "1) The incumbent or competitor named, and precisely what the "
        "customer said about them — strengths as well as frustrations, "
        "2) What is driving the customer to look, and how urgent it is, "
        "3) Switching costs and barriers discussed: integrations, "
        "retraining, data migration, sunk investment, internal "
        "champions of the incumbent, "
        "4) Contract end date, renewal window or notice period, if "
        "mentioned — capture the exact date given, "
        "5) Where we were compared directly, and on which criteria we "
        "were said to be ahead or behind, "
        "6) Any claim made about the competitor that we should verify "
        "before repeating it, "
        "7) What the customer needs to see in order to move, and what "
        "we committed to."
        + _VISUALS_DIRECTIVE
    ),
    "Sales-to-Delivery Handoff": (
        "This meeting hands a sold engagement to the delivery team. Its "
        "purpose is to transfer what was PROMISED during the sale, "
        "which is the one thing the delivery team cannot reconstruct "
        "from the SOW. Summarize with focus on: "
        "1) Every commitment made to the customer during the sales "
        "cycle — dates, capabilities, outcomes, people — and whether "
        "each is written into the contract or was verbal, "
        "2) What is in scope and, explicitly, what was excluded and "
        "why, "
        "3) Assumptions the estimate depends on, and who on the "
        "customer side has accepted them, "
        "4) Customer stakeholders: sponsor, day-to-day contact, "
        "sceptics, and anything about how they prefer to work, "
        "5) Known risks, sensitivities and history the delivery team "
        "would otherwise walk into blind, "
        "6) Environment and access facts already established, "
        "7) Open items the delivery team is inheriting, with owners. "
        "Flag any gap between what was promised verbally and what the "
        "signed scope covers — that gap is the single most valuable "
        "output of this meeting."
        + _VISUALS_DIRECTIVE
    ),
}


@dataclass
class Template:
    name: str
    prompt: str
    # Whether this template name shipped as a built-in. Determines whether
    # "Reset to default" is offered and whether delete is hide-vs-erase.
    is_default: bool = False
    # Original prompt if this is a default-origin template; None for
    # user-created entries. Used by reset(name) to undo local edits.
    default_prompt: Optional[str] = None


class TemplateService:
    """Thread-safe JSON-on-disk store for the summary-template library."""

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "summary_templates.json"
        self._lock = threading.Lock()
        # Seed the file with built-ins on first access so there's never a
        # window where the UI shows "no templates." After that we trust
        # whatever's on disk (user edits persist).
        self._ensure_seeded()

    # ── disk I/O ────────────────────────────────────────────────────
    def _ensure_seeded(self) -> None:
        with self._lock:
            if not self._path.exists():
                data = {
                    name: {
                        "prompt": prompt,
                        "is_default": True,
                        "default_prompt": prompt,
                        # Hidden templates (deleted defaults) stay in the
                        # file so we can still restore them. The UI
                        # filters on this flag.
                        "hidden": False,
                    }
                    for name, prompt in DEFAULT_TEMPLATES.items()
                }
                self._write_all_locked(data)
                return
            # File exists — migrate stored built-ins to the current
            # DEFAULT_TEMPLATES revision so improvements to the canonical
            # prompts (e.g. the visuals directive added in v2.7.9) reach
            # existing users instead of being a fresh-install-only change.
            #
            # Rules:
            #   - default_prompt is always refreshed so "Reset to default"
            #     reflects the latest canonical text.
            #   - prompt is refreshed only when the user hasn't customized
            #     it (i.e. prompt currently equals the OLD default_prompt).
            #     If the user edited it, we leave their version alone.
            try:
                data = self._read_all_locked()
            except Exception:
                data = {}
            dirty = False
            for name, latest in DEFAULT_TEMPLATES.items():
                entry = data.get(name)
                if not isinstance(entry, dict):
                    data[name] = {
                        "prompt": latest, "is_default": True,
                        "default_prompt": latest, "hidden": False,
                    }
                    dirty = True
                    continue
                old_default = entry.get("default_prompt")
                if old_default != latest:
                    if entry.get("prompt") == old_default:
                        entry["prompt"] = latest
                    entry["default_prompt"] = latest
                    entry["is_default"] = True
                    dirty = True
            if dirty:
                self._write_all_locked(data)

    def _read_all_locked(self) -> Dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"summary_templates.json unreadable ({e}); reseeding")
            return {}

    def _write_all_locked(self, data: Dict[str, dict]) -> None:
        # Atomic write: temp file in the same dir + os.replace so a crash
        # can never leave a half-written JSON and silently wipe the store.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── public API ──────────────────────────────────────────────────
    def list_all(self) -> List[Template]:
        """All non-hidden templates. Defaults first, then user-created, alphabetical within each."""
        with self._lock:
            raw = self._read_all_locked()
        out: List[Template] = []
        for name, entry in raw.items():
            if (entry or {}).get("hidden"):
                continue
            out.append(Template(
                name=name,
                prompt=entry.get("prompt", ""),
                is_default=bool(entry.get("is_default", False)),
                default_prompt=entry.get("default_prompt"),
            ))
        out.sort(key=lambda t: (not t.is_default, t.name.lower()))
        return out

    def get(self, name: str) -> Optional[Template]:
        if not name:
            return None
        with self._lock:
            raw = self._read_all_locked()
        entry = raw.get(name)
        if not entry or entry.get("hidden"):
            return None
        return Template(
            name=name,
            prompt=entry.get("prompt", ""),
            is_default=bool(entry.get("is_default", False)),
            default_prompt=entry.get("default_prompt"),
        )

    def get_prompt(self, name: str) -> str:
        """
        Resolve a template name to its prompt. Falls back to the General
        template (or the first available) when the requested name is
        missing, so a session tagged with a now-deleted template still
        summarizes instead of erroring out.
        """
        t = self.get(name)
        if t:
            return t.prompt
        general = self.get("General")
        if general:
            return general.prompt
        # Last resort — user somehow hid everything. Return a neutral prompt.
        return DEFAULT_TEMPLATES["General"]

    def upsert(self, name: str, prompt: str) -> Template:
        """Create a new template or update an existing one's prompt."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Template name required")
        if len(name) > 80:
            raise ValueError("Template name too long (80 char max)")
        with self._lock:
            raw = self._read_all_locked()
            existing = raw.get(name) or {}
            entry = {
                "prompt": prompt,
                "is_default": bool(existing.get("is_default", False)),
                "default_prompt": existing.get("default_prompt"),
                "hidden": False,  # un-hide if this was a restored default
            }
            raw[name] = entry
            self._write_all_locked(raw)
            return Template(
                name=name,
                prompt=prompt,
                is_default=entry["is_default"],
                default_prompt=entry["default_prompt"],
            )

    def delete(self, name: str) -> None:
        """
        Remove a user template, or hide a default (it stays in the JSON
        so the user can restore it later). Silently ignores unknown names.
        """
        with self._lock:
            raw = self._read_all_locked()
            entry = raw.get(name)
            if not entry:
                return
            if entry.get("is_default"):
                entry["hidden"] = True
                raw[name] = entry
            else:
                del raw[name]
            self._write_all_locked(raw)

    def reset(self, name: str) -> Optional[Template]:
        """Restore a default template's prompt to its original text. No-op for user-created entries."""
        with self._lock:
            raw = self._read_all_locked()
            entry = raw.get(name)
            if not entry or not entry.get("is_default"):
                return None
            default_prompt = entry.get("default_prompt") or ""
            entry["prompt"] = default_prompt
            entry["hidden"] = False
            raw[name] = entry
            self._write_all_locked(raw)
            return Template(
                name=name,
                prompt=default_prompt,
                is_default=True,
                default_prompt=default_prompt,
            )
