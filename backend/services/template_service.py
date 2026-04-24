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
DEFAULT_TEMPLATES: Dict[str, str] = {
    "General": (
        "Please summarize this meeting transcript. "
        "Include: key topics discussed, decisions made, "
        "action items, and any follow-ups needed."
    ),
    "Requirements Gathering": (
        "This is a requirements gathering meeting. Summarize with focus on: "
        "1) Business context and problem statement discussed, "
        "2) Functional requirements identified (what the system should do), "
        "3) Non-functional requirements (performance, security, scalability), "
        "4) Constraints and assumptions mentioned, "
        "5) Open questions that need follow-up, "
        "6) Stakeholder priorities and any conflicts between requirements."
    ),
    "Design Review": (
        "This is a design/architecture review meeting. Summarize with focus on: "
        "1) Solution overview and architecture discussed, "
        "2) Design decisions made and their rationale, "
        "3) Trade-offs considered, "
        "4) Risks and concerns raised, "
        "5) Feedback and requested changes, "
        "6) Next steps and action items."
    ),
    "Sprint Planning": (
        "This is a sprint planning meeting. Summarize with focus on: "
        "1) Sprint goal agreed upon, "
        "2) Stories/tasks committed to with owners, "
        "3) Capacity concerns or blockers raised, "
        "4) Dependencies identified, "
        "5) Carry-over items from previous sprint, "
        "6) Key risks to sprint delivery."
    ),
    "Stakeholder Update": (
        "This is a stakeholder update meeting. Summarize with focus on: "
        "1) Project status and progress reported, "
        "2) Milestones achieved or missed, "
        "3) Risks and issues escalated, "
        "4) Decisions requested from stakeholders, "
        "5) Decisions made by stakeholders, "
        "6) Next steps and timeline updates."
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
            if self._path.exists():
                return
            data = {
                name: {
                    "prompt": prompt,
                    "is_default": True,
                    "default_prompt": prompt,
                    # Hidden templates (deleted defaults) stay in the file so
                    # we can still restore them. The UI filters on this flag.
                    "hidden": False,
                }
                for name, prompt in DEFAULT_TEMPLATES.items()
            }
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
