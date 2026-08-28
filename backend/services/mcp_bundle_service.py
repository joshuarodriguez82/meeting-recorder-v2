"""
Turn the MCP server on from inside the app, in one click.

WHY THIS EXISTS
---------------
v2.71.0 added an "AI assistant access" card to Settings that told the
user to ``cd mcp-server``, create a virtualenv, and paste
``/absolute/path/to/mcp-server/.venv/bin/python`` into their AI tool's
config. That works if you cloned the repo. It is impossible if you
INSTALLED the app: the installer ships a backend runtime, not a
checkout, so there is no ``mcp-server/`` directory on the machine the
card is being displayed on. The card asked people to fill in a path to
a file that did not exist.

Three things stood between an installed user and a working MCP server,
and this module removes all three:

1. THE FILES.  ``zip-bundle.py`` now packs ``mcp-server/`` into the same
   zip as ``server.py``, so it extracts as a sibling of server.py inside
   ``<data_root>/runtime/`` — exactly how ``chrome-extension/`` already
   ships (see ``extension_bundle_service``). ``find_bundled_mcp_dir``
   locates it in that layout and in the dev-checkout layout.

2. THE SDK.  ``mcp`` is deliberately NOT in requirements-*.txt. First
   launch already installs ~1.5 GB of wheels and a resolution failure
   there bricks the whole app before it starts — v2.8.0 through v2.69.x
   shipped exactly that bug twice. So the SDK is installed on demand,
   after the app is known to work, by ``install_sdk()``. It is passed
   the same constraints file the bootstrap used, so it cannot drag a
   pinned backend dependency to a different version; if the resolution
   is impossible, pip fails and the app is untouched.

3. THE INTERPRETER.  The app's venv Python is the one interpreter that
   is guaranteed to have both the SDK and its dependencies. The backend
   is running inside it, so ``sys.executable`` names it for free — the
   user never has to know where it is. ``client_interpreter`` makes the
   one adjustment that matters: on Windows the backend runs under
   ``pythonw.exe`` (deliberately — no console window), and an MCP client
   speaks stdio to whatever we name, so we hand out the console
   ``python.exe`` from the same venv when it is there.

NOTHING HERE RUNS THE MCP SERVER. The AI client launches it, as a child
of itself, on demand. This module only answers "can it be launched, and
with exactly which two strings?".
"""

from __future__ import annotations

import importlib.util
import time
from datetime import datetime
import subprocess  # nosec B404 - fixed argv pip invocation, no shell
import sys
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

#: Zero-install entry point shipped next to the package. It puts its own
#: directory on sys.path so `mcp-server` never has to be pip-installed —
#: see mcp-server/run_mcp_server.py.
LAUNCHER_FILENAME = "run_mcp_server.py"

#: Directory name used both in the repo and inside the runtime bundle.
BUNDLE_DIRNAME = "mcp-server"

#: What install_sdk() asks pip for. Mirrors mcp-server/pyproject.toml's
#: `dependencies` — 2.x renamed FastMCP -> MCPServer and the server code
#: targets that API, so the major-version ceiling is load-bearing.
SDK_REQUIREMENTS = ("mcp>=2.0.0,<3", "httpx>=0.27")

#: pip can sit on a slow index for a long time; bound it so a hung
#: install surfaces as a reportable failure instead of a spinner that
#: never resolves.
INSTALL_TIMEOUT_S = 600


# ── "has an assistant actually called us?" ───────────────────────────
#
# A user set up Claude Desktop, restarted it repeatedly, and saw
# nothing. The config had been correct the whole time — Desktop was
# running from before the file was written, holding a stale copy — and
# nothing in the app could tell them which state they were in. The MCP
# server did not even identify itself in its requests, so the backend
# had no way to know it had never been called.
#
# It says so now. The card can report "last used by an AI assistant
# 3 minutes ago", which is the answer to the question the user is
# actually asking, instead of telling them to restart a fifth time.

#: Sent by the MCP server on every backend call (see mcp-server's
#: client.py). The `-mcp` suffix is load-bearing: "meeting-recorder"
#: alone is the app itself.
MCP_USER_AGENT_PREFIX = "meeting-recorder-mcp/"


def is_mcp_user_agent(user_agent: Optional[str]) -> bool:
    """Whether this request came from our MCP server.

    Deliberately narrow. The Chrome extension and the app's own UI call
    the same backend with the same token; a looser match would report
    "your assistant is connected" the moment someone opened Outlook Web
    — a confident wrong answer to the exact question being asked.
    """
    if not isinstance(user_agent, str) or not user_agent:
        return False
    return user_agent.strip().lower().startswith(MCP_USER_AGENT_PREFIX)


class ClientActivity:
    """When an MCP client last called, held in memory.

    In memory rather than on disk on purpose: this answers "is it
    working right now", the backend and the app share a lifetime, and
    writing a file on every request to answer a cosmetic question is a
    bad trade. The UI says "since the app started" so an empty value
    after a restart is not read as "it stopped working".
    """

    def __init__(self) -> None:
        self._last: Optional[float] = None
        self._clock = time.time

    def record(self, user_agent: Optional[str]) -> None:
        """Note a call. Never raises — this runs inside request
        handling, where an exception would turn a cosmetic feature into
        a failed API call."""
        try:
            if is_mcp_user_agent(user_agent):
                self._last = self._clock()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Ignoring MCP activity record failure: {e}")

    def last_seen_iso(self) -> Optional[str]:
        if self._last is None:
            return None
        return datetime.fromtimestamp(self._last).isoformat(timespec="seconds")


#: Process-wide recorder, read by /integrations/mcp/status.
activity = ClientActivity()


def _default_backend_dir() -> Path:
    """This module lives in ``<backend>/services/``; the backend dir is
    one level up — ``backend/`` in a dev checkout, or the extracted
    ``<data_root>/runtime/`` in a packaged build."""
    return Path(__file__).resolve().parent.parent


def _candidate_dirs(backend_dir: Path) -> List[Path]:
    return [
        # Packaged runtime: zip-bundle.py writes mcp-server/ into the
        # SAME zip as server.py, so it extracts as a sibling of it.
        backend_dir / BUNDLE_DIRNAME,
        # Dev checkout: mcp-server/ is at the repo root, a sibling of
        # backend/, not inside it.
        backend_dir.parent / BUNDLE_DIRNAME,
    ]


def find_bundled_mcp_dir(backend_dir: Optional[Path] = None) -> Optional[Path]:
    """The bundled ``mcp-server/`` directory, or None.

    "Present" means present enough to launch: the package and the
    launcher both have to be there. A partially-extracted directory has
    to read as absent, because handing a client a launcher that cannot
    import its own package produces a server that dies at startup with
    an error the user never sees — the client just reports the tool
    surface as unavailable.

    Never raises. A dev checkout that was never run through
    zip-bundle.py and a corrupted extraction are both legitimate,
    reportable states.
    """
    base = backend_dir or _default_backend_dir()
    for candidate in _candidate_dirs(base):
        try:
            if (candidate / "meeting_recorder_mcp" / "server.py").is_file() and (
                candidate / LAUNCHER_FILENAME
            ).is_file():
                return candidate
        except OSError as e:
            # A permission error on one candidate must not hide the
            # other one; log and keep looking.
            logger.debug(f"MCP bundle probe failed for {candidate}: {e}")
    return None


def sdk_installed() -> bool:
    """True when the ``mcp`` SDK is importable by THIS interpreter.

    Which is the question that matters: the interpreter we hand to the
    client is this one (see ``client_interpreter``), so its import
    surface is the client's import surface.
    """
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError) as e:
        # find_spec raises on a half-removed distribution rather than
        # returning None. Treat that as "not installed" — install_sdk()
        # will repair it — but say so, because it is not the ordinary
        # not-installed path.
        logger.debug(f"mcp SDK probe failed: {e}")
        return False


def client_interpreter(interpreter: Optional[Path] = None) -> Path:
    """The Python an MCP client should launch, given the one we run on.

    Identical on POSIX. On Windows the backend is spawned with
    ``pythonw.exe`` so no console window appears; an MCP client attaches
    pipes to the process it starts and expects a normal console
    interpreter, so we name ``python.exe`` from the same Scripts
    directory when it exists — falling back to what we were given
    rather than to a path that isn't there.
    """
    py = Path(interpreter or sys.executable)
    if py.name.lower().startswith("pythonw"):
        console = py.with_name(py.name.replace("pythonw", "python", 1))
        if console.exists():
            return console
    return py


def _constraints_file(backend_dir: Path) -> Optional[Path]:
    """The pin set the venv was bootstrapped with, if it shipped.

    Passing it to pip is what makes an on-demand install safe: pip may
    add ``mcp`` and whatever it needs, but it cannot move a package the
    backend is already pinned to. Mirrors src-tauri/src/lib.rs's
    ``constraints_filename``.
    """
    name = "constraints-cpu.txt" if sys.platform == "win32" else "constraints-mac.txt"
    path = backend_dir / name
    return path if path.is_file() else None


def status(backend_dir: Optional[Path] = None) -> Dict[str, object]:
    """Everything the Settings card needs to render, in one call.

    ``bundled`` and ``installed`` are reported separately on purpose.
    They fail for different reasons and have different fixes — "this
    build doesn't carry the files" is not something a user can click
    their way out of, "the SDK isn't installed yet" is one button — and
    collapsing them into a single ``ready`` flag would leave the card
    unable to say which one it is.
    """
    base = backend_dir or _default_backend_dir()
    mcp_dir = find_bundled_mcp_dir(base)
    installed = sdk_installed()
    return {
        "bundled": mcp_dir is not None,
        "installed": installed,
        "ready": mcp_dir is not None and installed,
        "mcp_dir": str(mcp_dir) if mcp_dir else None,
        "launcher": str(mcp_dir / LAUNCHER_FILENAME) if mcp_dir else None,
        "python": str(client_interpreter()),
        # None until an assistant actually calls. The card
        # renders this instead of asking the user to guess
        # whether their restart took.
        "last_client_seen_at": activity.last_seen_iso(),
    }


def install_sdk(backend_dir: Optional[Path] = None) -> Dict[str, object]:
    """pip-install the MCP SDK into the interpreter we're running on.

    Returns ``{"ok": bool, "output": str}`` rather than raising, because
    every realistic failure here — no network, a proxy that MITMs PyPI,
    a resolution conflict — is something the user needs to READ. pip's
    own last lines say more than any message this module could invent,
    so they are passed through to the UI.
    """
    base = backend_dir or _default_backend_dir()
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input",
    ]
    constraints = _constraints_file(base)
    if constraints:
        cmd += ["-c", str(constraints)]
    else:
        # Not fatal: a dev checkout has no shipped constraints file and
        # a floating resolve there is fine. Worth a line in the log,
        # because on a PACKAGED build its absence means the bundle is
        # incomplete.
        logger.info("No constraints file next to server.py; resolving freely.")
    cmd += list(SDK_REQUIREMENTS)

    logger.info(f"Installing MCP SDK: {' '.join(cmd)}")
    try:
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell, no user input
            cmd,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "output": (
                f"pip did not finish within {INSTALL_TIMEOUT_S // 60} minutes. "
                "It is usually a slow or blocked package index; try again on a "
                "different network."
            ),
        }
    except OSError as e:
        return {"ok": False, "output": f"Could not run pip: {e}"}

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        logger.error(f"MCP SDK install failed (exit {proc.returncode})")
        return {"ok": False, "output": _tail(output)}

    # importlib caches negative lookups; without this the very install we
    # just ran stays invisible until the backend restarts, and the card
    # would report "not installed" immediately after a successful click.
    importlib.invalidate_caches()
    logger.info("MCP SDK installed.")
    return {"ok": True, "output": _tail(output)}


def _tail(text: str, lines: int = 25) -> str:
    """pip's install log is long and its last lines carry the reason."""
    parts = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(parts[-lines:])
