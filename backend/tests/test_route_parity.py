"""
Route-parity guard.

server.py is a 6,800-line, 113-route monolith with no endpoint-level
tests. This guard is the safety net for the router split (and any future
route change): it imports the app headlessly and asserts the FULL route
table matches a committed golden snapshot. Moving a route into a router
module must leave (path, methods) byte-for-byte identical — if the split
drops, renames, or changes the method of any route, this fails.

The golden file (route_table.golden.json) is regenerated intentionally
only when routes are deliberately added/removed:

    MEETING_RECORDER_SKIP_DEP_REPAIR=1 python -c \
      "import json,sys; sys.path.insert(0,'backend'); \
       from tests._app_import import import_app, route_table; \
       json.dump(route_table(import_app()), \
                 open('backend/tests/route_table.golden.json','w'), indent=1)"

A red diff here on a refactor PR means a route was lost — not that the
golden is stale.
"""

import json
from pathlib import Path

from _app_import import import_app, route_table

_GOLDEN = Path(__file__).parent / "route_table.golden.json"


def test_app_imports_headlessly():
    app = import_app()
    assert app is not None
    assert len(route_table(app)) > 100  # sanity: full table, not a stub


def test_route_table_matches_golden():
    app = import_app()
    current = route_table(app)
    golden = json.loads(_GOLDEN.read_text())
    # Normalize golden's inner lists (JSON has no tuples) for comparison.
    golden = sorted([[p, sorted(m)] for p, m in golden])

    cur_set = {(p, tuple(m)) for p, m in current}
    gold_set = {(p, tuple(m)) for p, m in golden}
    missing = sorted(gold_set - cur_set)
    added = sorted(cur_set - gold_set)
    assert not missing and not added, (
        f"Route table drifted from golden.\n"
        f"  MISSING (in golden, not in app): {missing}\n"
        f"  ADDED   (in app, not in golden): {added}\n"
        f"If this change is intentional, regenerate the golden (see "
        f"module docstring)."
    )


def test_critical_routes_present():
    """A few load-bearing routes spelled out explicitly, so a golden
    regenerated at the wrong moment can't silently bless their removal."""
    paths = {p for p, _ in route_table(import_app())}
    for must in (
        "/health",
        "/recording/start",
        "/recording/stop",
        "/sessions",
        "/briefing/extension-import",
    ):
        assert must in paths, f"missing critical route {must}"
