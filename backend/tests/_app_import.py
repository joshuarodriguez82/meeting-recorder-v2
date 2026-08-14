"""
Headless import of the FastAPI app for structural tests.

WARNING — real trap, cost a debugging session (2026-08): never run
`python -c "import server"` (or anything else that imports server.py)
directly in a shell, ad hoc, without MEETING_RECORDER_SKIP_DEP_REPAIR=1
set. server.py self-heals its venv at import time by shelling out to
`pip install -r requirements.txt` — the FULL ML requirements (torch,
faster-whisper, pyannote deps, …), not the lightweight test set. Run
that against the minimal CI venv (numpy/scipy/soundfile/pytest/fastapi/
httpx only) and it happily installs several GB of packages into it,
including a `torch` whose C-extension download can truncate if the
command is killed/times out mid-install — which then breaks unrelated
tests (audio finalize, recovery) in every LATER pytest run in that venv
with confusing `AttributeError: module 'torch' has no attribute
'Tensor'` failures, because `import torch` "succeeds" against the
broken partial install. There is no in-process fix once this happens —
the venv itself is contaminated; rebuilding it from scratch is the only
way back. ALWAYS import server.py through `import_app()` below (or with
MEETING_RECORDER_SKIP_DEP_REPAIR=1 set yourself) instead of importing
it raw.

server.py can't normally be imported outside the packaged app: at import
time it runs `pip install` to self-heal the venv, and its transitive
imports touch platform/ML modules (sounddevice, faster_whisper, …) that
aren't in the lightweight CI test env. This helper makes `import server`
work headlessly so we can assert on the route table without a GB of ML
deps:

  1. Sets MEETING_RECORDER_SKIP_DEP_REPAIR=1 so the import-time pip call
     is skipped (see server.py).
  2. Stubs the handful of optional/heavy modules that server.py's
     transitive imports reference at module load — but ONLY if they
     aren't really installed, so a full env is used as-is.

Heavy LLM/ML libs (torch, anthropic, pyannote, sentence_transformers,
speechbrain) are imported lazily at their use sites, never at module
load, so they need no stub — the route table builds without them.
"""

import importlib
import os
import sys
import types
from unittest.mock import MagicMock

# Modules server.py's import graph pulls in at load time that aren't in
# the minimal test env (fastapi + numpy/scipy/soundfile). Kept small on
# purpose — if this list has to grow, prefer making the offending import
# lazy in the backend over stubbing more here.
_OPTIONAL_MODULES = ("dotenv", "faster_whisper", "sounddevice", "uvicorn")


def _stub_optional_modules() -> None:
    for name in _OPTIONAL_MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            mod = types.ModuleType(name)
            mod.__path__ = []  # mark as package so submodule imports work
            mod.__getattr__ = lambda _attr: MagicMock()  # type: ignore[attr-defined]
            sys.modules[name] = mod


def import_app():
    """Import server.py headlessly and return its FastAPI `app`."""
    os.environ.setdefault("MEETING_RECORDER_SKIP_DEP_REPAIR", "1")
    _stub_optional_modules()
    import server  # noqa: E402  (deliberate: after env + stubs)
    return server.app


def route_table(app) -> list:
    """[(path, sorted(methods)), …] for every real route, sorted — the
    canonical structural fingerprint the split must preserve."""
    rows = [
        [r.path, sorted(r.methods or [])]
        for r in app.routes
        if getattr(r, "path", None)
    ]
    return sorted(rows)
