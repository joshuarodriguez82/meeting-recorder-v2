"""Per-domain FastAPI routers extracted from the server.py monolith.

Each module defines an ``APIRouter`` carrying one domain's endpoints and
is wired into the app via ``app.include_router`` near the bottom of
server.py. Handlers reach shared state through ``from server import svc``
— safe because the includes run at the end of server.py, after ``svc``
and every helper are defined, so the (already fully-populated) server
module satisfies the import.

The route-parity test (tests/test_route_parity.py) guards that moving a
handler here leaves its (path, methods) byte-identical.
"""
