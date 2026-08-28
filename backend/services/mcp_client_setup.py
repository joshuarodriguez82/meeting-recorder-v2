"""
Write each AI tool's MCP config for it, instead of asking for JSON edits.

WHY
---
v2.72's Settings card showed a Claude Code command and a Claude Desktop
JSON block side by side and never said that configuring one does not
configure the other. `claude mcp add` writes ``~/.claude.json``; Claude
Desktop reads ``claude_desktop_config.json``. A user ran the command the
card gave them, restarted everything, and Claude Desktop was still
blind — having done exactly what the app said, in the order it said it.

Hand-editing JSON was never a reasonable thing to ask for. The app
already knows both absolute paths (see ``mcp_bundle_service``) and where
each client keeps its config. So it writes the file, and the user
presses a button.

WHAT THIS WILL AND WILL NOT WRITE
---------------------------------
Claude Desktop and Cursor only. Both read a documented file whose whole
schema is an ``mcpServers`` map, so a merge into it is well-defined.

Deliberately excluded:

  Claude Code   owns ``~/.claude.json`` and has a supported CLI for
                this. Writing another tool's private state file behind
                its back is how you end up fighting its own writer.
  VS Code       has no single answer — the location depends on which
                extension is installed (Cline and Continue keep
                different files in different places). Guessing means
                writing a file nothing reads and reporting success,
                which is worse than the snippet.

Both keep the copy-a-snippet path, and the card says so rather than
implying every client is covered.

SAFETY RULES
------------
This edits a file the app does not own, so:

  - MERGE, never replace. Other servers and unrelated top-level keys
    (Claude Desktop keeps other settings here) are preserved exactly.
  - REPLACE a stale entry rather than adding a second one. The app's
    data directory can move between installs, and a leftover entry
    leaves the client launching an interpreter that no longer exists.
  - REFUSE on a config that will not parse. Overwriting a file we could
    not read is the same defect as rendering an unreadable result as an
    absent one — the user is told to fix it or paste the snippet.
  - BACK UP before writing, and write atomically via a temp file in the
    same directory, so an interrupted write cannot truncate a working
    config.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

#: The key the entry is written under, in every client. Matches
#: src/lib/mcp-config.ts's MCP_SERVER_NAME.
SERVER_NAME = "meeting-recorder"


class ConfigUnreadable(Exception):
    """The existing config could not be parsed, so it was not touched."""


def config_path(
    client_id: str,
    *,
    platform: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    home: Optional[str] = None,
) -> Optional[Path]:
    """Where this client keeps its MCP config, or None if we don't write
    for it. Pure — platform, environment and home are passed in so every
    branch is testable from any machine.
    """
    plat = platform if platform is not None else sys.platform
    environ = env if env is not None else dict(os.environ)
    home_dir = Path(home if home is not None else str(Path.home()))

    if client_id == "claude-desktop":
        if plat.startswith("win"):
            appdata = (environ.get("APPDATA") or "").strip()
            # No APPDATA means any path we build lands somewhere
            # arbitrary and the write "succeeds" into a file no client
            # reads. Report that we can't, rather than pretend.
            if not appdata:
                return None
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
        if plat == "darwin":
            return (home_dir / "Library" / "Application Support" / "Claude"
                    / "claude_desktop_config.json")
        return home_dir / ".config" / "Claude" / "claude_desktop_config.json"

    if client_id == "cursor":
        return home_dir / ".cursor" / "mcp.json"

    # claude-code, vscode, other — see the module docstring.
    return None


def can_write_for(client_id: str) -> bool:
    return config_path(client_id) is not None


def _load(text: Optional[str]) -> Dict[str, Any]:
    """Parse an existing config, or start a fresh one. Raises rather
    than returning an empty dict on unparseable input — the caller must
    not go on to overwrite it."""
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ConfigUnreadable(f"not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigUnreadable(
            f"the top level is a {type(data).__name__}, not an object")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ConfigUnreadable(
            f"\"mcpServers\" is a {type(servers).__name__}, not an object")
    return data


def _entry(python: str, launcher: str) -> Dict[str, Any]:
    return {"command": python, "args": [launcher]}


def merge_entry(existing: Optional[str], python: str, launcher: str) -> str:
    """The new file content, with our entry merged into whatever was
    there. Pure. Raises ConfigUnreadable rather than clobbering."""
    data = _load(existing)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = _entry(python, launcher)
    data["mcpServers"] = servers
    # Indented because a human will open this file to check it.
    return json.dumps(data, indent=2) + "\n"


def entry_state(existing: Optional[str], python: str, launcher: str) -> str:
    """"absent" | "current" | "stale" | "unreadable".

    `stale` is named separately from `absent` on purpose: it is the
    state that produces a client which lists the server and then fails
    every call, because the paths it was given no longer exist. "Not set
    up" and "set up, pointing at the wrong place" need different words
    on screen and different buttons.
    """
    try:
        data = _load(existing)
    except ConfigUnreadable:
        return "unreadable"
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return "absent"
    return "current" if servers[SERVER_NAME] == _entry(python, launcher) else "stale"


def read_config(path: Path) -> Optional[str]:
    """The file's text, or None when it isn't there. Never raises for
    absence — no config yet is the ordinary first-run state."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigUnreadable(f"could not read {path}: {e}") from e


def client_status(client_id: str, python: str, launcher: str) -> Dict[str, Any]:
    """Per-client state for the Settings card. Never raises."""
    path = config_path(client_id)
    if path is None:
        return {"client": client_id, "writable": False, "path": None,
                "state": "manual"}
    try:
        state = entry_state(read_config(path), python, launcher)
    except ConfigUnreadable:
        state = "unreadable"
    return {"client": client_id, "writable": True, "path": str(path),
            "state": state}


def install(client_id: str, python: str, launcher: str) -> Dict[str, Any]:
    """Write our entry into this client's config.

    Returns a result dict rather than raising, because every realistic
    failure — a config we can't parse, a permission error, a client we
    don't write for — is something the user needs to read and act on.
    """
    path = config_path(client_id)
    if path is None:
        return {"ok": False, "client": client_id, "path": None,
                "error": ("This tool has to be set up by hand — copy the "
                          "snippet below into its own MCP config.")}

    try:
        existing = read_config(path)
    except ConfigUnreadable as e:
        return {"ok": False, "client": client_id, "path": str(path),
                "error": str(e)}

    try:
        merged = merge_entry(existing, python, launcher)
    except ConfigUnreadable as e:
        # Deliberately NOT overwritten. A config we cannot read may hold
        # settings the user cares about.
        return {"ok": False, "client": client_id, "path": str(path),
                "error": (f"{path.name} is already there but {e}. It has been "
                          "left untouched — fix it, or paste the snippet "
                          "below in by hand.")}

    backup = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing is not None:
            backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
            shutil.copy2(path, backup)
        # Same directory so the replace is atomic on both platforms.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(merged, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.exception(f"Writing {client_id} MCP config failed")
        return {"ok": False, "client": client_id, "path": str(path),
                "error": f"Couldn't write {path}: {e}"}

    logger.info(f"Wrote MCP entry for {client_id} to {path}")
    return {"ok": True, "client": client_id, "path": str(path),
            "backup": str(backup) if backup else None,
            "created": existing is None}
