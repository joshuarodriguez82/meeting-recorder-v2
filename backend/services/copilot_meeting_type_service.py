"""
Per-user Co-Pilot Meeting Types (stored in copilot_meeting_types.json).

Meeting-type modifiers layer on TOP of a co-pilot mode. The mode sets
the persona ("you are an SA / a sales engineer / an executive"); the
meeting type narrows the guidance to a specific kind of meeting
("this is a discovery call — push back when they jump to solutions").

At tick time `coach_tick` composes:
  {mode.prompt}\\n\\nThis meeting is a {type.name}. {type.prompt}\\n\\n{OUTPUT_RULES}

So a Sales+SOW Review combination gets the deal-velocity persona plus
the redline-catching guidance — automatically, without having to
pre-stitch a "Sales — SOW Review" template for every combination.

Same shape and storage semantics as CoPilotModeService (mirrors
TemplateService).
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


DEFAULT_MEETING_TYPES: Dict[str, str] = {
    "General": (
        "No specific meeting-type lens applies. Use general meeting "
        "judgment — surface what the participants seem to be working "
        "toward and what's getting in the way."
    ),
    "Discovery": (
        "The goal of this meeting is to understand the customer's "
        "environment, current pain, stakeholders, decision process, "
        "and what success looks like — before any solutioning happens.\n\n"
        "Probe the layers being skipped: current-state diagram, who "
        "actually uses the system day-to-day, where data lives, how "
        "the current pain shows up in metrics, who has authority to "
        "change it, what they've already tried. Push back gently when "
        "the room jumps to solutions before defining problems.\n\n"
        "Flag risks like: conversation is shallow on a key dimension "
        "(no metrics, no users, no constraints named), the stated "
        "problem isn't the real problem, the room is solutioning "
        "before discovery is complete."
    ),
    "SOW / Contract Review": (
        "The room is walking through a draft SOW, MSA, or contract "
        "redlines. Catch scope gaps, unprotective language, and "
        "commercial risk before the document is signed.\n\n"
        "Probe scope ambiguity, exclusion gaps, change-order triggers, "
        "acceptance criteria definitions, payment milestone "
        "definitions, termination clauses.\n\n"
        "Flag risks like: missing acceptance criteria, no change-order "
        "process, deliverables defined as verbs not nouns, payment "
        "tied to customer-controlled events, vague 'reasonable "
        "assistance' language, dependencies on third parties without "
        "contractual basis, missing data-handling / PII / compliance "
        "clauses for the industry."
    ),
    "Status / Sync": (
        "This is a recurring status meeting reviewing progress, "
        "blockers, and commitments across a workstream. Surface "
        "slippage early, catch quiet blockers, keep commitments from "
        "going stale.\n\n"
        "Probe vague updates ('on track' without evidence), slip-"
        "without-mitigation patterns, blockers stated but not owned, "
        "decisions deferred to next week for the third week running, "
        "dependencies that haven't moved.\n\n"
        "Flag risks like: a commitment from a prior meeting being "
        "quietly forgotten, the same blocker re-raised without "
        "progress, scope creep going uncalled-out, deadlines at risk "
        "with no flag, an owner going silent on their items."
    ),
    "Technical Deep-Dive": (
        "This is a technical working session — architecture review, "
        "design discussion, integration deep-dive, debugging session. "
        "The room is in the weeds on a specific technical problem.\n\n"
        "Probe assumptions not yet stated (network topology, IAM "
        "trust, data sovereignty, throughput, retention), edge cases "
        "the design hasn't addressed (failure modes, retry logic, "
        "idempotency), and constraints the design might be silently "
        "violating.\n\n"
        "Flag risks like: architectural decision made without "
        "considering an obvious failure mode, contract being "
        "designed that doesn't match a known caller, scaling "
        "assumptions that don't survive 10x volume, security "
        "boundaries left implicit, brittle integration patterns, a "
        "technology choice that silently locks in a vendor."
    ),
    "Customer Demo": (
        "The room is delivering or watching a product demo. Coaching "
        "is calibrated to keeping the demo on the value story and "
        "catching the audience's reactions.\n\n"
        "Probe: are the demo flows tied to the customer's stated "
        "use cases? Are objections being heard or just answered? Is "
        "the demo over-tuned to features the customer didn't ask "
        "about? Are next steps being lined up while interest is high?\n\n"
        "Flag risks like: demo getting too deep into UI mechanics, "
        "objections being deflected instead of explored, audience "
        "going quiet (disengagement signal), the demo flow not "
        "matching the customer's actual workflow, no clear post-demo "
        "commitment in sight."
    ),
    "Internal Working Session": (
        "This is an internal-team working session — no customer in "
        "the room. The team is solving a problem together. Coaching "
        "is calibrated to decision velocity and clear ownership.\n\n"
        "Probe: is the decision actually being made or just discussed? "
        "Is there a clear DRI? Are the constraints stated? Are the "
        "options enumerated honestly or is the conversation circling "
        "the preferred option?\n\n"
        "Flag risks like: no decision actually emerging from the "
        "discussion, the loudest opinion winning by default, "
        "constraints being assumed not verified, scope expanding "
        "without anyone pricing it, action items being volunteered "
        "without realistic capacity."
    ),

    # ── Account-management lenses ───────────────────────────────────
    #
    # The LIVE half of the sales summary templates. A template records
    # what happened; a lens is trying to change what happens next, so
    # these are written as things to probe and risks to flag, not as
    # sections to produce.
    #
    # "Qualification" rather than "Discovery" on purpose — Discovery
    # above is an SA lens about the system, this one is about the deal.
    # See tests/test_sales_copilot_types.py.
    "Qualification": (
        "This is a qualification conversation. The goal is to find out "
        "whether there is a deal here, who can buy it, and what would "
        "make it move — not to solution.\n\n"
        "Probe the dimensions being left unexamined: what the problem "
        "costs them in money or time, who has budget authority as "
        "opposed to interest, what the compelling event is and whether "
        "it is real, what happens if they do nothing, who else is being "
        "looked at, what their buying process actually requires.\n\n"
        "Flag risks like: a qualifying question asked and never "
        "answered, enthusiasm from someone with no authority, a "
        "timeline with no event behind it, budget assumed rather than "
        "confirmed, the room drifting into a demo before the problem is "
        "understood, our side talking more than the customer."
    ),
    "Pricing / Negotiation": (
        "The room is discussing money. A number said out loud here can "
        "end up in a contract, so precision and restraint matter more "
        "than rapport.\n\n"
        "Probe: what the figure actually covers and what it excludes, "
        "whether an anchor has been set and by whom, what the customer "
        "is comparing the price to, what their approval path requires, "
        "which concession is being asked for and what we would get in "
        "return, whether the person asking has authority to close.\n\n"
        "Flag risks like: a concession offered without a condition "
        "attached, a number given as indicative being repeated back as "
        "firm, discounting before value is established, agreeing to a "
        "date or a scope change in passing, negotiating against "
        "ourselves when the customer has not countered, a commitment "
        "made by someone who cannot approve it."
    ),
    "Executive Briefing": (
        "The audience is senior and their time is short. The subject is "
        "business outcomes; architecture is only relevant if an "
        "executive raises it.\n\n"
        "Probe: which outcome this executive is measured on, where this "
        "sits against their other priorities, who the real sponsor is, "
        "what would make them personally look good or bad, what they "
        "believe the risk of acting is versus not acting.\n\n"
        "Flag risks like: jargon or product detail creeping in, "
        "answering a business question with a technical answer, the "
        "sponsor going quiet, a sceptic's objection being talked over "
        "rather than addressed, the meeting ending without a decision "
        "or a named next step, spending the executive's time on "
        "material they did not ask for."
    ),
    "Renewal / Account Review": (
        "This is an existing customer reviewing the relationship. "
        "Retention is decided in rooms like this one, usually before "
        "anyone says the word renewal.\n\n"
        "Probe: what they are actually using versus what they bought, "
        "which promised outcomes have and have not landed, who the "
        "sponsor is now and whether that has changed, what has "
        "frustrated them that they have not raised, what else is "
        "happening in their organisation that could affect budget.\n\n"
        "Flag risks like: a complaint mentioned once and moved past, "
        "the sponsor being replaced or reorganised, adoption lower than "
        "expected, an evaluation of alternatives hinted at, an "
        "expansion signal going unexplored, the renewal date "
        "approaching without anyone naming it."
    ),
}


@dataclass
class CoPilotMeetingType:
    name: str
    prompt: str
    is_default: bool = False
    default_prompt: Optional[str] = None


class CoPilotMeetingTypeService:
    """Thread-safe JSON-on-disk store for co-pilot meeting-type modifiers."""

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "copilot_meeting_types.json"
        self._lock = threading.Lock()
        self._ensure_seeded()

    def _ensure_seeded(self) -> None:
        with self._lock:
            if not self._path.exists():
                data = {
                    name: {
                        "prompt": prompt,
                        "is_default": True,
                        "default_prompt": prompt,
                        "hidden": False,
                    }
                    for name, prompt in DEFAULT_MEETING_TYPES.items()
                }
                self._write_all_locked(data)
                return
            try:
                data = self._read_all_locked()
            except Exception:
                data = {}
            dirty = False
            for name, latest in DEFAULT_MEETING_TYPES.items():
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
            logger.warning(
                f"copilot_meeting_types.json unreadable ({e}); reseeding")
            return {}

    def _write_all_locked(self, data: Dict[str, dict]) -> None:
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

    def list_all(self) -> List[CoPilotMeetingType]:
        with self._lock:
            raw = self._read_all_locked()
        out: List[CoPilotMeetingType] = []
        for name, entry in raw.items():
            if (entry or {}).get("hidden"):
                continue
            out.append(CoPilotMeetingType(
                name=name,
                prompt=entry.get("prompt", ""),
                is_default=bool(entry.get("is_default", False)),
                default_prompt=entry.get("default_prompt"),
            ))
        out.sort(key=lambda m: (not m.is_default, m.name.lower()))
        return out

    def get(self, name: str) -> Optional[CoPilotMeetingType]:
        if not name:
            return None
        with self._lock:
            raw = self._read_all_locked()
        entry = raw.get(name)
        if not entry or entry.get("hidden"):
            return None
        return CoPilotMeetingType(
            name=name,
            prompt=entry.get("prompt", ""),
            is_default=bool(entry.get("is_default", False)),
            default_prompt=entry.get("default_prompt"),
        )

    def get_prompt(self, name: str) -> str:
        """Resolve a meeting-type name to its prompt. Falls back to
        General so coaching never errors out on a missing type."""
        t = self.get(name)
        if t:
            return t.prompt
        general = self.get("General")
        if general:
            return general.prompt
        return DEFAULT_MEETING_TYPES["General"]

    def upsert(self, name: str, prompt: str) -> CoPilotMeetingType:
        name = (name or "").strip()
        if not name:
            raise ValueError("Meeting type name required")
        if len(name) > 80:
            raise ValueError("Meeting type name too long (80 char max)")
        with self._lock:
            raw = self._read_all_locked()
            existing = raw.get(name) or {}
            entry = {
                "prompt": prompt,
                "is_default": bool(existing.get("is_default", False)),
                "default_prompt": existing.get("default_prompt"),
                "hidden": False,
            }
            raw[name] = entry
            self._write_all_locked(raw)
            return CoPilotMeetingType(
                name=name, prompt=prompt,
                is_default=entry["is_default"],
                default_prompt=entry["default_prompt"],
            )

    def delete(self, name: str) -> None:
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

    def reset(self, name: str) -> Optional[CoPilotMeetingType]:
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
            return CoPilotMeetingType(
                name=name, prompt=default_prompt,
                is_default=True, default_prompt=default_prompt,
            )
