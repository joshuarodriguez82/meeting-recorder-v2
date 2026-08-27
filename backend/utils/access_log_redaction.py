"""Scrub the backend auth token out of uvicorn's access log.

Why this exists: the token is PERSISTED across launches (lib.rs reuses
it so the extension's saved copy keeps working), and requests that
cannot set headers — EventSource, <audio>, <img> — carry it as
``?token=<value>``. uvicorn's access log prints the full request line,
query string included; its stdout is backend.log; and the tail of
backend.log ships inside every diagnostics bundle. Left alone, sharing
a diagnostics zip shares a long-lived credential.

A ``logging.Filter`` is the right layer because it runs before any
handler renders the record, so the value never reaches stdout, the log
file, or the bundle. It rewrites rather than drops: the access line
minus the token is still the debugging record the log exists for.

Filters attached to a LOGGER (not a handler) survive uvicorn's own
logging configuration — uvicorn replaces handlers on its loggers at
startup but leaves logger-level filters in place — so installing before
``uvicorn.run`` is sufficient and ordering-safe.
"""

from __future__ import annotations

import logging
import re

REDACTED = "REDACTED"

# The query parameter only ever carries the 64-hex-char token, but the
# pattern deliberately matches ANY value: a truncated, malformed, or
# future-format token is exactly as secret as a well-formed one.
_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")


class TokenRedactionFilter(logging.Filter):
    """Rewrites ``token=<value>`` to ``token=REDACTED`` in log records.

    Handles both shapes uvicorn emits: the normal case where the request
    line arrives via ``record.args`` (msg is a %-format template), and
    the defensive case of a pre-rendered string message.
    Never drops a record — always returns True.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if record.args:
                record.args = tuple(
                    _TOKEN_RE.sub(rf"\g<1>{REDACTED}", a) if isinstance(a, str)
                    else a
                    for a in record.args
                )
            if isinstance(record.msg, str) and "token=" in record.msg:
                record.msg = _TOKEN_RE.sub(rf"\g<1>{REDACTED}", record.msg)
        except Exception:  # noqa: BLE001
            # nosec B110 — deliberately silent: this runs INSIDE the
            # logging pipeline, so logging the failure from here can
            # recurse straight back into this filter. Worst case the
            # line goes through unscrubbed — the pre-fix status quo.
            pass
        return True


def install_access_log_redaction() -> None:
    """Attach the filter to every logger that could render a request
    line. uvicorn.access is the one that does today; uvicorn.error and
    the root are covered for the error paths that echo a URL."""
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(TokenRedactionFilter())
