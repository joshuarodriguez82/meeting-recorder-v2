"""
Finding — and fixing — a speech library that is installed but broken.

FIELD LOG (2026-09-15)
----------------------
Voice fingerprinting was off on one install for every meeting, with
only this in the log::

    Speaker fingerprinting unavailable: cannot import name
    'DependencyGraph' from 'speechbrain.utils.depgraph'

That error means speechbrain is HALF-upgraded: files from two versions
side by side — typically an update interrupted on Windows. The startup
dependency check only asked whether the package existed (find_spec),
which a half-installed package passes, so nothing was ever repaired and
nobody was told; the user just found speakers never saved.

So at startup, off the critical path, speechbrain is actually imported —
in a separate process, so the app never pays for loading torch to find
out — and a half-upgraded install is reinstalled at the pinned version.
speechbrain is pure Python, so nothing is locked while the app runs.
The outcome is kept so "fingerprinting isn't available" can say why.

The decisions are pure functions; the subprocess calls are injectable.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

PACKAGE = "speechbrain"

# A name missing from a module that IS present: two versions' files mixed.
_HALF_INSTALLED_RE = re.compile(
    r"cannot import name .+ from '?speechbrain", re.IGNORECASE)
_MISSING_RE = re.compile(r"No module named '?speechbrain", re.IGNORECASE)


def classify_import_error(text: str) -> str:
    """'half_installed', 'missing' or 'other' for an import failure."""
    t = text or ""
    if _HALF_INSTALLED_RE.search(t):
        return "half_installed"
    if _MISSING_RE.search(t):
        return "missing"
    return "other"


def pinned_version(constraints_text: str, package: str = PACKAGE) -> Optional[str]:
    """The ``package==X`` pin in a constraints file, if any."""
    for line in (constraints_text or "").splitlines():
        m = re.match(rf"^\s*{re.escape(package)}\s*==\s*([^\s;#]+)", line,
                     re.IGNORECASE)
        if m:
            return m.group(1)
    return None


@dataclass
class EncoderHealth:
    #: unknown | ok | repaired | broken | missing | error
    state: str = "unknown"
    detail: str = ""

    def user_reason(self) -> str:
        """Why fingerprinting is off, in the user's terms; "" when it
        isn't (or we don't know)."""
        if self.state == "broken":
            return ("the speech library it needs is damaged (part of an "
                    "update didn't install) and the automatic repair "
                    "failed — reinstalling Meeting Recorder fixes it")
        if self.state == "missing":
            return "the speech library it needs is not installed"
        return ""


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)


def probe(python: str = sys.executable, timeout: float = 180.0,
          run: Callable = _run) -> tuple:
    """(ok, error_text) for importing speechbrain in a child process."""
    try:
        r = run([python, "-c", f"import {PACKAGE}"], timeout)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout or "").strip()[-2000:]


def check_and_repair(python: str = sys.executable,
                     constraints: Optional[Path] = None,
                     run: Callable = _run) -> EncoderHealth:
    """Probe; reinstall a half-installed speechbrain at its pinned
    version; probe again. Never raises."""
    ok, err = probe(python, run=run)
    if ok:
        return EncoderHealth("ok")
    kind = classify_import_error(err)
    if kind == "missing":
        return EncoderHealth("missing", err[-300:])
    if kind != "half_installed":
        return EncoderHealth("error", err[-300:])

    version = None
    try:
        if constraints is not None and constraints.exists():
            version = pinned_version(constraints.read_text(encoding="utf-8"))
    except OSError:
        version = None
    spec = f"{PACKAGE}=={version}" if version else PACKAGE
    logger.warning(f"speechbrain is half-installed ({err.splitlines()[-1] if err else ''}); "
                   f"reinstalling {spec}")
    try:
        r = run([python, "-m", "pip", "install", "--disable-pip-version-check",
                 "--force-reinstall", "--no-deps", spec], 900)
        if r.returncode != 0:
            return EncoderHealth(
                "broken", (r.stderr or r.stdout or "").strip()[-300:])
    except Exception as e:  # noqa: BLE001
        return EncoderHealth("broken", f"{type(e).__name__}: {e}")

    ok, err = probe(python, run=run)
    if not ok:
        return EncoderHealth("broken", err[-300:])
    # A failed import can leave the stale submodules cached in THIS
    # process; drop them so the next use imports the repaired files.
    for name in [m for m in sys.modules if m == PACKAGE
                 or m.startswith(PACKAGE + ".")]:
        sys.modules.pop(name, None)
    logger.info(f"speechbrain repaired ({spec})")
    return EncoderHealth("repaired", spec)


#: The result of the startup check, read by the fingerprint explanation.
CURRENT = EncoderHealth()
