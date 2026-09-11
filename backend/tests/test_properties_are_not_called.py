"""
No ``@property`` may be invoked as if it were a method.

WHAT THIS COST (field log 2026-09-10)
-------------------------------------
``RecordingService.is_recording`` is a ``@property`` returning a bool.
Three places in server.py called it as ``is_recording()``, so each one
raised ``TypeError: 'bool' object is not callable`` every time it ran:

  * ``probe_mic`` — the Record tab's microphone readiness check,
    shipped in v2.81.0, answered **500 on every request**. The feature
    had never worked for anyone.
  * ``_auto_index_busy`` — the "is anything else running?" guard the
    knowledge indexer defers to.
  * ``_export_sweep_loop`` — the export reconciliation sweep, which
    logged ``Export sweep failed: 'bool' object is not callable`` **123
    times in four hours**: a 100% failure rate since the loop was
    written. That sweep is the safety net for "files reached the
    Designated Folder"; it had never once run.

WHY THE SUITE WAS GREEN
-----------------------
Every fake ``RecordingService`` in the suite declared::

    def is_recording(self) -> bool:

as a METHOD. The fakes answered the call the real object refused, so
124 tests passed while three production code paths raised on contact.
That is precisely the failure AGENTS.md describes — a stub that invents
a shape makes the suite green while the product is broken — and the
fakes are now properties.

WHY A SCAN AND NOT JUST THE FIX
-------------------------------
Fixing three call sites does not stop the fourth. Nothing about
``x.is_recording()`` looks wrong at a glance; the mistake is invisible
without knowing how the attribute is declared three files away, and
Python only finds out at runtime. So this asserts the property for the
whole tree.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

#: Property names that are also ordinary methods on OTHER, unrelated
#: objects, where a call is correct. Each entry is a name plus the
#: reason a call of it is legitimate.
_ALLOWED = {
    # PyObjC EventKit events expose location() as a real method; the
    # same name happens to be a @property on our own draft models.
    "location": "PyObjC EventKit bridge objects",
    # SessionIndex.available is a property, but the keychain backend
    # under test exposes available() as a method.
    "available": "keychain backend probe",
}


def _python_files():
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _declared_properties() -> dict:
    """{name: {"file:Class", ...}} for every @property in the backend."""
    found = collections.defaultdict(set)
    for path in _python_files():
        if "tests" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in item.decorator_list:
                    name = getattr(dec, "id", None) or getattr(dec, "attr", None)
                    if name == "property":
                        found[item.name].add(
                            f"{path.relative_to(BACKEND)}:{node.name}")
    return found


def _zero_arg_calls(names) -> dict:
    """{name: ["file:line", ...]} for `something.<name>()` with no args."""
    hits = collections.defaultdict(list)
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in names
                    and not node.args and not node.keywords):
                hits[node.func.attr].append(
                    f"{path.relative_to(BACKEND)}:{node.lineno}")
    return hits


def test_no_property_is_invoked_as_a_method():
    """The check that would have caught all three sites before release.

    A zero-argument call of a name declared ``@property`` anywhere in
    the backend. Names that are legitimately methods on some other
    object are listed in _ALLOWED with the reason."""
    properties = _declared_properties()
    offenders = {
        name: sites
        for name, sites in _zero_arg_calls(set(properties)).items()
        if name not in _ALLOWED
    }
    assert not offenders, (
        "a @property is being called as a method — this raises "
        "TypeError at runtime and no test that fakes the object will "
        "catch it:\n"
        + "\n".join(
            f"  {name}() at {', '.join(sites)} "
            f"— declared @property in {', '.join(sorted(properties[name]))}"
            for name, sites in sorted(offenders.items())))


def test_is_recording_is_still_a_property():
    """Asserted against the REAL class, not a description of it.

    If this ever becomes a method the scan above stops covering it, and
    the three call sites it protects would silently start needing
    parentheses back."""
    from _app_import import import_app
    import_app()
    from services.recording_service import RecordingService
    assert isinstance(
        RecordingService.__dict__.get("is_recording"), property), (
        "is_recording is no longer a property — every call site and "
        "every fake in the suite assumes it is")


def test_the_fakes_in_this_suite_match_that_shape():
    """The fakes are why this shipped: they answered a call the real
    object refuses. Any new fake that gets it wrong re-opens the hole,
    so they are checked against the real class rather than by eye."""
    import re
    wrong = []
    for path in (BACKEND / "tests").rglob("test_*.py"):
        if path.name == Path(__file__).name:
            continue  # this file quotes the broken shape in its docstring
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^(\s*)def is_recording\(", text, re.M):
            before = text[:match.start()].rstrip().rsplit("\n", 1)[-1]
            if "@property" not in before:
                wrong.append(f"{path.name}:"
                             f"{text[:match.start()].count(chr(10)) + 1}")
    assert not wrong, (
        "these fakes declare is_recording as a method, but "
        "RecordingService declares it as a property — a fake that is "
        "more permissive than the real thing is how the export sweep "
        "failed 123 times with a green suite: " + ", ".join(wrong))
