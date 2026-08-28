"""
Auth for the remote (HTTP) transport.

WHY THIS EXISTS
---------------
The stdio transport needs no authentication: the client launches the
server as its own child process, so being able to talk to it already
means having the user's account on the machine. HTTP is the opposite —
the whole point is that something not on this machine can reach it, and
in the intended deployment that something is Anthropic's servers on the
far end of a tunnel.

So every request carries a bearer token, and the token is checked before
anything reaches the MCP layer.

THE TOKEN IS ITS OWN, deliberately not the backend's. That one is held
by the Chrome extension and pasted into local tools; this one is typed
into a hosted connector configuration and travels off the machine.
Separate secrets mean revoking remote access does not break the
extension, and a leak of one is not a leak of the other.

WHAT THESE TESTS PIN
--------------------
The failure modes that turn an auth check into decoration: a missing
header, a wrong token, a right token under the wrong scheme, and — the
one that actually ships in real code — an empty expected token, which a
naive equality check treats as "accept anything empty".

Also that /health stays open, because a tunnel needs an unauthenticated
liveness path and it reveals nothing.
"""

from __future__ import annotations

from meeting_recorder_mcp import remote


class TestTokenCheck:
    def test_accepts_the_right_token(self):
        assert remote.authorized("Bearer " + "a" * 64, "a" * 64) is True

    def test_rejects_the_wrong_token(self):
        assert remote.authorized("Bearer " + "b" * 64, "a" * 64) is False

    def test_rejects_a_missing_header(self):
        assert remote.authorized(None, "a" * 64) is False
        assert remote.authorized("", "a" * 64) is False

    def test_rejects_the_bare_token_without_a_scheme(self):
        """Sloppy acceptance here is how a token ends up in a URL or a
        log line that was only ever meant to hold a header."""
        assert remote.authorized("a" * 64, "a" * 64) is False

    def test_rejects_a_different_scheme(self):
        assert remote.authorized("Basic " + "a" * 64, "a" * 64) is False

    def test_scheme_is_case_insensitive_per_rfc7235(self):
        assert remote.authorized("bearer " + "a" * 64, "a" * 64) is True
        assert remote.authorized("BEARER " + "a" * 64, "a" * 64) is True

    def test_tolerates_surrounding_whitespace(self):
        assert remote.authorized("  Bearer   " + "a" * 64 + "  ", "a" * 64) is True

    def test_a_prefix_of_the_token_is_not_enough(self):
        assert remote.authorized("Bearer " + "a" * 32, "a" * 64) is False

    def test_an_empty_expected_token_never_authorizes(self):
        """If the app failed to generate or load a token, the correct
        behaviour is to refuse everything — NOT to accept everything,
        which is what a naive equality check would do for an empty
        header."""
        assert remote.authorized("Bearer ", "") is False
        assert remote.authorized("", "") is False
        assert remote.authorized(None, "") is False


class TestOpenPaths:
    def test_health_needs_no_token(self):
        """A tunnel needs an unauthenticated liveness path, and this one
        reveals nothing but 'a server is here'."""
        assert remote.is_open_path("/health") is True

    def test_everything_else_is_closed(self):
        for path in ("/", "/mcp", "/mcp/", "/messages", "/sse",
                     "/health/../mcp"):
            assert remote.is_open_path(path) is False, path


class TestBindAddress:
    def test_defaults_to_loopback(self):
        """Never 0.0.0.0 by default. Binding every interface turns "I
        chose to publish this" into "it was on the office wifi the whole
        time" — the tunnel is what makes exposure deliberate."""
        assert remote.bind_host(None) == "127.0.0.1"
        assert remote.bind_host("") == "127.0.0.1"

    def test_an_explicit_host_is_honoured(self):
        assert remote.bind_host("0.0.0.0") == "0.0.0.0"  # nosec B104 - asserting the override is honoured


class TestTokenGeneration:
    def test_is_long_and_hex(self):
        tok = remote.generate_token()
        assert len(tok) == 64
        int(tok, 16)  # raises if not hex

    def test_is_not_the_same_twice(self):
        assert remote.generate_token() != remote.generate_token()
