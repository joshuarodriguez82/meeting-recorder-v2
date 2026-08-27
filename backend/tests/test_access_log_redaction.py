"""The backend auth token must not leak through the access log.

The token is PERSISTED (lib.rs reuses it across launches so the
extension's saved copy keeps working), and two request channels exist:
an Authorization header and — for EventSource / <audio> / <img>, which
cannot set headers — a ?token= query parameter. uvicorn's access log
prints the full request line, query string included, and stdout is
backend.log, whose tail ships inside every diagnostics bundle
(utils/diagnostics_bundle: "backend.log.tail.txt").

Chained together: a user shares a diagnostics zip → the zip carries a
long-lived credential → any local process (or, for the forms that count
as CORS "simple requests", any web page) can drive the API with it.

The fix is a logging.Filter on the uvicorn access logger that rewrites
token=<value> before the line is rendered. These tests pin the filter's
behavior and — because the filter only matters if it is actually
installed — a source pin that server.py wires it up before uvicorn.run.
"""

from __future__ import annotations

import logging
from pathlib import Path

from utils.access_log_redaction import REDACTED, TokenRedactionFilter


def _access_record(path: str) -> logging.LogRecord:
    """A LogRecord shaped like uvicorn.access emits them: the request
    line arrives via args, not pre-rendered into msg."""
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:52011", "GET", path, "1.1", 200),
        exc_info=None,
    )


def test_query_token_is_redacted_from_access_lines():
    rec = _access_record(
        "/recording/transcript/stream?token=deadbeef" + "0" * 56)
    assert TokenRedactionFilter().filter(rec) is True  # never drops lines
    rendered = rec.getMessage()
    assert "deadbeef" not in rendered
    assert f"token={REDACTED}" in rendered


def test_token_amid_other_params_leaves_them_readable():
    rec = _access_record("/calendar/upcoming?hours=72&token=cafe1234&refresh=true")
    TokenRedactionFilter().filter(rec)
    rendered = rec.getMessage()
    assert "cafe1234" not in rendered
    assert "hours=72" in rendered and "refresh=true" in rendered


def test_tokenless_lines_pass_through_untouched():
    rec = _access_record("/health")
    TokenRedactionFilter().filter(rec)
    assert rec.getMessage() == '127.0.0.1:52011 - "GET /health HTTP/1.1" 200'


def test_prerendered_string_messages_are_also_scrubbed():
    """Defence in depth: if a future uvicorn renders the line into msg
    with no args, the filter must still catch it."""
    rec = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='GET /x?token=beefbeef HTTP/1.1', args=None, exc_info=None)
    TokenRedactionFilter().filter(rec)
    assert "beefbeef" not in rec.getMessage()


def test_server_installs_the_filter_before_serving():
    """Source pin, same pattern as the cache-warming pin: the wiring is
    invisible at unit level, and a filter that exists but is never
    attached is exactly as useful as no filter."""
    src = (Path(__file__).resolve().parent.parent / "server.py").read_text(
        encoding="utf-8")
    assert "install_access_log_redaction()" in src, (
        "server.py no longer installs the access-log token redaction")
    assert src.index("install_access_log_redaction()") < src.index(
        "uvicorn.run("), "redaction must be installed before uvicorn.run"
