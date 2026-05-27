"""
Per-user Co-Pilot Modes (stored in USER_DATA_DIR/copilot_modes.json).

A "mode" is the top-level persona framing for the live co-pilot — the
SA's role on the call. Three modes ship by default: SA, Sales,
Executive. Each is a pure role / what-to-care-about framing; meeting-
type-specific guidance ("this is a discovery call", "this is a SOW
review") layers on TOP of the mode via the CoPilotMeetingTypeService.

The JSON output rules are NOT in the mode prompt — they're appended
once by `coach_tick` itself so editing a mode never breaks the wire
format the panel parses.

Same storage / atomic-write / edit-and-reset / hide-vs-delete semantics
as TemplateService and CoPilotMeetingTypeService.
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


DEFAULT_MODES: Dict[str, str] = {
    "SA": (
        "You are a live in-call co-pilot for a Solutions Architect at "
        "[scrubbed] Digital. The SA's focus areas are Amazon Connect, "
        "CCaaS migrations (from Genesys / NICE / Cisco UCCX / Five9 / "
        "Webex Contact Center), contact-center IVR & contact flow "
        "design, Lambda + Bedrock integrations, IAM trust chains, and "
        "agent-experience design.\n\n"
        "Care about: scope boundary clarity, integration assumptions "
        "(CRM / IAM / data sovereignty), contact-flow gotchas (DTMF vs "
        "ASR, queue overflow, after-hours routing), vendor lock-in, "
        "licensing hidden costs, legacy-system constraints nobody "
        "named, and timeline realism.\n\n"
        "Coaching style: concrete, technical, owner-tagged. Skip "
        "generic 'communication risk' or 'team alignment' filler — "
        "the SA already knows that lens. Prefer specific CCaaS / "
        "Connect / integration probes."
    ),
    "Sales": (
        "You are a live in-call co-pilot for an account executive or "
        "pre-sales engineer in a customer-facing conversation. Your "
        "lens is advancing the deal, qualifying the opportunity, and "
        "uncovering objections before they harden into deal-killers.\n\n"
        "Care about: BANT/MEDDIC gaps (budget, decision criteria, "
        "decision process, identified pain, champion strength, "
        "competition, timeline, paper process), single-threaded "
        "champion risk, missing economic buyer, stalling language, "
        "feature-request-without-pain patterns, scope creep delaying "
        "signature, vendor comparison signals.\n\n"
        "Coaching style: focused on deal-velocity actions. Surface the "
        "question that moves the deal forward in this exact moment. "
        "Tie follow-ups to specific commercial next steps (send ROI, "
        "introduce sponsor, send pilot SOW, schedule technical "
        "validation)."
    ),
    "Executive": (
        "You are a live in-call co-pilot for an executive being briefed "
        "by their team, briefing a customer's executive, or sitting in "
        "on a working session at executive altitude. Your job is to "
        "help the executive cut through detail, keep the conversation "
        "at outcomes, and avoid getting pulled into implementation "
        "minutiae.\n\n"
        "Care about: business impact, dollar figures, customer/revenue "
        "exposure, decision deadlines, competitive exposure, sponsor "
        "engagement risk, financial commitments implied but not "
        "approved, reputational risk in current path, decisions being "
        "made at the wrong altitude. Skip implementation-detail risks; "
        "those belong in the working session, not the executive's "
        "brain.\n\n"
        "Coaching style: crisp, decisive, outcome-focused. When the "
        "team drifts into mechanics, pull the conversation up to "
        "outcomes. Executive-grade follow-ups: schedule 1:1 with their "
        "CFO, commission a 1-pager on Option B, escalate to steering, "
        "kill the path wasting the team's time."
    ),
}


@dataclass
class CoPilotMode:
    name: str
    prompt: str
    is_default: bool = False
    default_prompt: Optional[str] = None


class CoPilotModeService:
    """Thread-safe JSON-on-disk store for co-pilot mode personas.

    Mirrors TemplateService exactly so behavior is predictable for the
    user — same edit/reset/delete semantics, same atomic write, same
    hide-vs-delete for defaults."""

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "copilot_modes.json"
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
                    for name, prompt in DEFAULT_MODES.items()
                }
                self._write_all_locked(data)
                return
            try:
                data = self._read_all_locked()
            except Exception:
                data = {}
            dirty = False
            for name, latest in DEFAULT_MODES.items():
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
            logger.warning(f"copilot_modes.json unreadable ({e}); reseeding")
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

    def list_all(self) -> List[CoPilotMode]:
        with self._lock:
            raw = self._read_all_locked()
        out: List[CoPilotMode] = []
        for name, entry in raw.items():
            if (entry or {}).get("hidden"):
                continue
            out.append(CoPilotMode(
                name=name,
                prompt=entry.get("prompt", ""),
                is_default=bool(entry.get("is_default", False)),
                default_prompt=entry.get("default_prompt"),
            ))
        out.sort(key=lambda m: (not m.is_default, m.name.lower()))
        return out

    def get(self, name: str) -> Optional[CoPilotMode]:
        if not name:
            return None
        with self._lock:
            raw = self._read_all_locked()
        entry = raw.get(name)
        if not entry or entry.get("hidden"):
            return None
        return CoPilotMode(
            name=name,
            prompt=entry.get("prompt", ""),
            is_default=bool(entry.get("is_default", False)),
            default_prompt=entry.get("default_prompt"),
        )

    def get_prompt(self, name: str) -> str:
        """Resolve a mode name to its prompt. Falls back to SA (the
        primary user persona), then a hardcoded default."""
        m = self.get(name)
        if m:
            return m.prompt
        m = self.get("SA")
        if m:
            return m.prompt
        return DEFAULT_MODES["SA"]

    def upsert(self, name: str, prompt: str) -> CoPilotMode:
        name = (name or "").strip()
        if not name:
            raise ValueError("Mode name required")
        if len(name) > 80:
            raise ValueError("Mode name too long (80 char max)")
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
            return CoPilotMode(
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

    def reset(self, name: str) -> Optional[CoPilotMode]:
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
            return CoPilotMode(
                name=name, prompt=default_prompt,
                is_default=True, default_prompt=default_prompt,
            )
