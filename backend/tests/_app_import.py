"""
Headless import of the FastAPI app for structural tests.

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


def _walk_routes(routes) -> list:
    """Flatten a route list, recursing into mounted routers.

    FastAPI's ``include_router`` (0.13x+) doesn't copy a router's routes
    flat into ``app.routes`` — it mounts a single ``_IncludedRouter``
    wrapper that dispatches into the original router. To reconstruct the
    real served (path, methods) table we recurse through each wrapper's
    ``original_router.routes``. Routes decorated directly on the app
    (``@app.get``) still appear flat and are captured as-is. Handles
    arbitrary nesting."""
    out = []
    for r in routes:
        orig = getattr(r, "original_router", None)
        if orig is not None and hasattr(orig, "routes"):
            out.extend(_walk_routes(orig.routes))
        elif getattr(r, "path", None):
            out.append([r.path, sorted(r.methods or [])])
    return out


def route_table(app) -> list:
    """[(path, sorted(methods)), …] for every real route, sorted — the
    canonical structural fingerprint the split must preserve. Recurses
    into mounted routers so a route reads the same whether it's declared
    on the app or lives in an included router module."""
    return sorted(_walk_routes(app.routes))
