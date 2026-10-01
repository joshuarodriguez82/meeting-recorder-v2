"""
The log has to hold what happened, not who polled.

Diagnostics bundle 2026-10-01: 30,859 of 31,015 lines in the shipped 2 MB
tail were access lines for polling endpoints, so it covered four hours.
And every app log line was written twice (basicConfig's root handler
plus the app logger's own), halving what was left.
"""

from __future__ import annotations

import io
import logging

from utils.access_log_redaction import QuietPollingFilter
from utils.logger import dedupe_root_handlers


def _access(method, path, status):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", method, path, "1.1", status), None)


def test_successful_status_polls_are_dropped():
    f = QuietPollingFilter()
    assert not f.filter(_access("GET", "/recording/status", 200))
    assert not f.filter(_access("GET", "/health", 200))
    assert not f.filter(_access("GET", "/recording/copilot/state?x=1", 200))


def test_a_failed_poll_is_kept():
    f = QuietPollingFilter()
    assert f.filter(_access("GET", "/recording/status", 500))
    assert f.filter(_access("GET", "/health", 401))


def test_everything_else_is_kept():
    f = QuietPollingFilter()
    assert f.filter(_access("POST", "/recording/start", 200))
    assert f.filter(_access("GET", "/sessions/ABC/full", 200))
    assert f.filter(_access("POST", "/recording/status", 200))


def test_an_unexpected_record_shape_is_kept():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            "odd", None, None)
    assert QuietPollingFilter().filter(rec)


def test_an_app_log_line_is_written_once():
    root = logging.getLogger()
    buf = io.StringIO()
    root_handler = logging.StreamHandler(buf)
    app_buf = io.StringIO()
    app = logging.getLogger("test_log_noise.app")
    app_handler = logging.StreamHandler(app_buf)
    app.addHandler(app_handler)
    app.setLevel(logging.INFO)
    root.addHandler(root_handler)
    try:
        dedupe_root_handlers()
        app.info("recording started")
        logging.getLogger("third.party").warning("lib warning")
    finally:
        root.removeHandler(root_handler)
        app.removeHandler(app_handler)
    assert app_buf.getvalue().count("recording started") == 1
    assert "recording started" not in buf.getvalue()
    assert "lib warning" in buf.getvalue()
