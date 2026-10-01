"""
Provider URLs from Settings are fetched only over http(s).

urllib.request opens file:// and ftp:// as readily as https://. The
Settings provider base URL feeds two fetches (the model list and the
diagnostics probe), so a mistyped or hostile value could read a local
file. Bandit flagged all three urlopen calls (B310); this pins the fix.
"""

from __future__ import annotations

import pytest

from tests._app_import import import_app

import_app()
import server  # noqa: E402


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "C:\\\\secrets.txt", "",
])
def test_non_http_urls_are_refused(url):
    with pytest.raises(ValueError):
        server._require_http_url(url)
    ok, detail = server._probe_http(url)
    assert ok is False and "http(s)" in detail


def test_http_and_https_pass():
    assert server._require_http_url("http://localhost:11434/v1")
    assert server._require_http_url("https://api.example.com/v1/models")


def test_the_model_list_fetch_refuses_a_file_url():
    with pytest.raises(ValueError):
        server._stdlib_get_json("file:///etc/passwd")
