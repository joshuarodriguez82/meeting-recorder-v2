"""
Regression tests for Services.load_settings() initialisation.

Field report 2026-08-13 (v2.26.0): the backend answered /settings with
200 but 500'd every /clients/config with

    AttributeError: 'NoneType' object has no attribute 'get_all'

and recording was impossible — the Record tab showed no devices and
"Not ready to record".

Cause: `load_settings()` contained a function-local
`from config.settings import USER_DATA_DIR` partway down. That makes
USER_DATA_DIR local to the WHOLE method, so the session-index
`index_db_path=USER_DATA_DIR / ...` argument added near the top of the
method raised UnboundLocalError. `self.settings` is assigned on the
method's first line, so it stayed set while every service constructed
after the failure point stayed None — and the `if self.settings is None`
guard then skipped re-initialisation forever.

Two invariants are locked in here:

1. No shadowing re-import of a module-level name inside load_settings.
2. Initialisation is gated on COMPLETION (`_services_ready`), not on
   `self.settings` being set, so a partial failure is retried instead of
   becoming permanent.

Both are asserted against the source text rather than by importing the
app, so they hold in the CI venv (numpy/scipy/soundfile/pytest/fastapi
only) without the heavy ML dependencies server.py pulls in at import.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVER_PY = Path(__file__).resolve().parents[1] / "server.py"


def _load_settings_node() -> ast.FunctionDef:
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "load_settings":
            return node
    raise AssertionError("load_settings() not found in server.py")


def _module_level_imported_names() -> set[str]:
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:  # module level only
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


def test_load_settings_does_not_shadow_module_level_imports():
    """A function-local import of a name already imported at module level
    makes that name local to the ENTIRE function, so every reference above
    the import raises UnboundLocalError. That is what broke v2.26.0."""
    module_names = _module_level_imported_names()
    fn = _load_settings_node()

    shadowed = []
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                name = a.asname or a.name
                if name in module_names:
                    shadowed.append(name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                name = a.asname or a.name.split(".")[0]
                if name in module_names:
                    shadowed.append(name)

    assert not shadowed, (
        "load_settings() re-imports name(s) already imported at module "
        f"level: {sorted(set(shadowed))}. This makes them function-local "
        "for the whole method, so any reference ABOVE the import raises "
        "UnboundLocalError. Drop the local import and use the "
        "module-level one."
    )


def test_specifically_user_data_dir_is_not_reimported():
    """The exact name that caused the v2.26.0 outage."""
    fn = _load_settings_node()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                assert (a.asname or a.name) != "USER_DATA_DIR", (
                    "USER_DATA_DIR must NOT be re-imported inside "
                    "load_settings() — it is imported at module level. "
                    "See this module's docstring."
                )


def test_init_is_gated_on_completion_not_on_settings_being_set():
    """`self.settings` is assigned on load_settings()'s first line, so it
    cannot serve as the 'already initialised' guard: a failure partway
    through would leave it set, every later service None, and the guard
    skipping re-initialisation forever. The guard must consult a flag
    that is only set after everything is built."""
    fn = _load_settings_node()

    guard = next(
        (n for n in fn.body if isinstance(n, ast.If)),
        None,
    )
    assert guard is not None, "load_settings() has no top-level if guard"

    guard_src = ast.dump(guard.test)
    assert "_services_ready" in guard_src, (
        "load_settings()'s guard must include the completion flag "
        "(_services_ready), not just `self.settings is None`."
    )

    # …and the flag must be set at the very end of the guarded block, so
    # it can only be true when every service above it was constructed.
    last = guard.body[-1]
    assert isinstance(last, ast.Assign), (
        "the last statement of load_settings()'s init block should assign "
        "the completion flag"
    )
    assert "_services_ready" in ast.dump(last), (
        "_services_ready must be set as the LAST statement of the init "
        "block — setting it earlier would mark a half-built Services "
        "object as ready, which is the bug this guards against."
    )


def test_services_ready_starts_false():
    """A freshly constructed Services must not claim to be initialised."""
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "__init__"):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.AnnAssign) and "_services_ready" in ast.dump(stmt):
                assert isinstance(stmt.value, ast.Constant)
                assert stmt.value.value is False
                found = True
            elif isinstance(stmt, ast.Assign) and "_services_ready" in ast.dump(stmt):
                assert isinstance(stmt.value, ast.Constant)
                assert stmt.value.value is False
                found = True
    assert found, "_services_ready must be initialised to False in __init__"
