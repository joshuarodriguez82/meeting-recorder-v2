"""
Per-client configuration (stored in USER_DATA_DIR/client_configs.json).

Currently holds a single field per client — `export_folder` — the
directory the user wants session artifacts (WAV, transcript, summary,
action items, decisions, requirements) automatically copied into after
a recording completes or is re-processed.

The store is keyed on the normalized client name. Normalization is just
lowercase + strip so "Acme Corp" and "acme corp " resolve to the same
entry. Display names keep their original casing on sessions.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def _normalize(name: str) -> str:
    return (name or "").strip().lower()


@dataclass
class ClientConfig:
    export_folder: str = ""


class ClientConfigService:
    """Thread-safe JSON-on-disk store for per-client settings."""

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "client_configs.json"
        self._lock = threading.Lock()

    def _read_all(self) -> Dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"client_configs.json unreadable ({e}); starting fresh")
            return {}

    def _write_all(self, data: Dict[str, dict]) -> None:
        # Same atomic-write pattern used by SessionService — temp in same
        # dir, fsync, os.replace — so a crash never leaves a half-written
        # JSON and silently loses every client's folder.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, suffix=".json.tmp")
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

    def get_all(self) -> Dict[str, ClientConfig]:
        with self._lock:
            raw = self._read_all()
        return {
            name: ClientConfig(
                export_folder=(entry or {}).get("export_folder", "") or "",
            )
            for name, entry in raw.items()
        }

    def get(self, client: str) -> Optional[ClientConfig]:
        if not client:
            return None
        key = _normalize(client)
        with self._lock:
            raw = self._read_all()
        entry = raw.get(key)
        if not entry:
            return None
        return ClientConfig(
            export_folder=entry.get("export_folder", "") or "")

    def set(self, client: str, cfg: ClientConfig) -> None:
        """Write a client's config. Creates the entry if missing."""
        if not client:
            raise ValueError("client name required")
        key = _normalize(client)
        with self._lock:
            raw = self._read_all()
            raw[key] = {
                "display_name": client.strip(),
                **asdict(cfg),
            }
            self._write_all(raw)

    def rename(self, old: str, new: str) -> None:
        """Move a config entry when the user renames a client."""
        old_key = _normalize(old)
        new_key = _normalize(new)
        if old_key == new_key:
            return
        with self._lock:
            raw = self._read_all()
            if old_key not in raw:
                return
            entry = raw.pop(old_key)
            entry["display_name"] = new.strip()
            raw[new_key] = entry
            self._write_all(raw)

    def delete(self, client: str) -> None:
        key = _normalize(client)
        with self._lock:
            raw = self._read_all()
            if key in raw:
                del raw[key]
                self._write_all(raw)
