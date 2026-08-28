"""Locate the running Meeting Recorder backend: base URL + auth token.

The app is a Tauri shell that spawns `backend/server.py` as a sidecar.
Two things have to be recovered from outside that process tree:

PORT
    src-tauri/src/lib.rs::pick_free_port() tries the pinned
    PREFERRED_PORT (17645) first and only falls back to an OS-assigned
    ephemeral port if 17645 is already held. The fallback port is NOT
    written anywhere on disk — the Chrome extension has the same
    constraint and solves it by asking the user to re-paste the URL, so
    we solve it the same way: default to 17645, allow an explicit
    override via env.

TOKEN
    src-tauri/src/lib.rs::generate_backend_token() writes a 64-char hex
    token to ``<data_root>/extension-token`` (v2.16+) and passes the
    same value to the sidecar as MEETING_RECORDER_TOKEN. Every non
    ``/health`` request must carry it (backend/server.py
    ``require_backend_token``, ~line 538).

    NOTE: the file is called ``extension-token``, not ``backend_token``.
    ``backend_token`` is only the name of the Rust accessor function.
    We also probe ``backend_token`` / ``backend-token`` as courtesy
    fallbacks in case a future build renames it, but ``extension-token``
    is the real, current name.

data_root differs per platform AND — on Linux only — differs between
the Rust shell and the Python backend:

    Windows   %LOCALAPPDATA%\\MeetingRecorder            (both)
    macOS     ~/Library/Application Support/MeetingRecorder  (both)
    Linux     $XDG_DATA_HOME/MeetingRecorder            (Rust, writes the token)
              $XDG_CONFIG_HOME/MeetingRecorder          (Python, writes config.env)

so on Linux both are probed, data dir first.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17645

#: Token filenames to probe inside each candidate data root, in order.
TOKEN_FILENAMES = ("extension-token", "backend_token", "backend-token")

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class TokenNotFound(Exception):
    """No token could be resolved from env or from any data directory.

    Carries the list of paths that were checked so the failure message
    can name them — "we looked here and here" is actionable; "missing
    token" is not.
    """

    def __init__(self, searched: List[Path]) -> None:
        self.searched = searched
        super().__init__("No backend token found.")


@dataclass(frozen=True)
class BackendLocation:
    base_url: str
    token: str
    #: Where the token came from, for diagnostics: "env" or a file path.
    token_source: str
    #: True when the token doesn't look like the app's 64-hex format.
    #: Not fatal (a user running the backend standalone can pick any
    #: string) but worth surfacing when auth then fails.
    token_looks_unusual: bool


def candidate_data_dirs() -> List[Path]:
    """Every directory that might hold the token file, most likely first."""
    dirs: List[Path] = []

    def add(p: Optional[Path]) -> None:
        if p is not None and p not in dirs:
            dirs.append(p)

    # Explicit override wins outright.
    override = (os.environ.get("MEETING_RECORDER_DATA_DIR") or "").strip()
    if override:
        add(Path(override).expanduser())

    if _is_windows():
        for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            val = os.environ.get(var)
            if val:
                add(Path(val) / "MeetingRecorder")
        add(Path.home() / "MeetingRecorder")
    elif _is_macos():
        add(Path.home() / "Library" / "Application Support" / "MeetingRecorder")
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
        if xdg_cfg:
            add(Path(xdg_cfg) / "MeetingRecorder")
    else:
        # Linux: Rust writes the token under the *data* dir, Python
        # writes config.env under the *config* dir. Probe both.
        xdg_data = os.environ.get("XDG_DATA_HOME")
        add(Path(xdg_data) / "MeetingRecorder" if xdg_data
            else Path.home() / ".local" / "share" / "MeetingRecorder")
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
        add(Path(xdg_cfg) / "MeetingRecorder" if xdg_cfg
            else Path.home() / ".config" / "MeetingRecorder")

    return dirs


# Platform detection goes through these two functions rather than
# reading os.name / sys.platform inline, so the per-platform tests can
# patch THESE instead of mutating `os.name` globally (which breaks
# pathlib for the rest of the process).
def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    import sys
    return sys.platform == "darwin"


def token_search_paths() -> List[Path]:
    """Full list of files probed for a token, in probe order."""
    return [d / name for d in candidate_data_dirs() for name in TOKEN_FILENAMES]


def resolve_token() -> tuple[str, str]:
    """Return (token, source). Raises TokenNotFound if nothing is found.

    Precedence: MEETING_RECORDER_TOKEN env var, then the first readable,
    non-empty token file.
    """
    env_token = (os.environ.get("MEETING_RECORDER_TOKEN") or "").strip()
    if env_token:
        return env_token, "env:MEETING_RECORDER_TOKEN"

    searched: List[Path] = []
    for path in token_search_paths():
        searched.append(path)
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        token = raw.strip()
        if token:
            return token, str(path)
    raise TokenNotFound(searched)


PORT_FILENAME = "backend-port"


def read_port_file() -> Optional[int]:
    """The live backend port, written by the Tauri shell at startup.

    `pick_free_port()` prefers 17645 and falls back to an ephemeral port
    when it is taken; the shell persists whichever it got, next to the
    auth token. Without this an external client silently assumes 17645
    and reports "the app isn't running" while it is.

    Anything unreadable, malformed or out of range returns None so the
    caller falls back — a truncated file must not make the tools
    unavailable, and the eventual connection error explains itself
    better than a startup refusal would.
    """
    for directory in candidate_data_dirs():
        try:
            raw = (directory / PORT_FILENAME).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            port = int(raw.strip())
        except ValueError:
            continue
        if 1 <= port <= 65535:
            return port
    return None


def resolve_base_url() -> str:
    """Base URL of the backend.

    Precedence, most specific first:

        MEETING_RECORDER_URL  >  MEETING_RECORDER_PORT  >  the port file
        >  the pinned 17645

    An explicit override outranks the file deliberately: someone
    tunnelling, or testing against a stand-in backend, must not be
    overruled by whatever the last local app run happened to write. The
    file only replaces the guess.
    """
    url = (os.environ.get("MEETING_RECORDER_URL") or "").strip()
    if url:
        return url.rstrip("/")

    host = (os.environ.get("MEETING_RECORDER_HOST") or "").strip() or DEFAULT_HOST
    port_env = (os.environ.get("MEETING_RECORDER_PORT") or "").strip()
    port = DEFAULT_PORT
    if port_env:
        try:
            port = int(port_env)
        except ValueError:
            # Same posture as backend/server.py __main__: warn-and-fall-back
            # rather than refusing to start. A typo'd port shouldn't make
            # the tool list unavailable; the connection error will say so.
            port = DEFAULT_PORT
    else:
        port = read_port_file() or DEFAULT_PORT
    return f"http://{host}:{port}"


def resolve_location() -> BackendLocation:
    """Full resolution. Raises TokenNotFound when no token is available."""
    token, source = resolve_token()
    return BackendLocation(
        base_url=resolve_base_url(),
        token=token,
        token_source=source,
        token_looks_unusual=not bool(_HEX64.match(token)),
    )
