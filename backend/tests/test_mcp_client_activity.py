"""
"Did it actually work?" — answered by the app instead of guessed at.

THE FIELD REPORT
----------------
A user set up Claude Desktop, restarted it repeatedly, and saw nothing.
The config was correct the whole time; Claude Desktop had been running
since before the file was written, so it was holding a stale copy. Every
restart they tried was against a config that genuinely had nothing in it,
and then against a running app that had already read the old one.

Nothing anywhere could tell them which state they were in. The app knew
it had never been called by an assistant and said nothing, because
nothing was recording it — the MCP server didn't even identify itself in
its requests.

So: the MCP server sends a User-Agent, the backend notes when it last
saw one, and the card can say "last used by an AI assistant 3 minutes
ago" instead of "quit the app fully and reopen it" for the fifth time.

WHAT THESE TESTS PIN
--------------------
Recognising our own client without recognising everything else — a
match that is too loose turns the Chrome extension or a curl into
"your assistant is connected", which is worse than no signal at all,
because it is a confident wrong answer to the exact question the user is
asking.
"""

from __future__ import annotations

from services import mcp_bundle_service as mbs


class TestUserAgentMatch:
    def test_matches_our_client(self):
        assert mbs.is_mcp_user_agent("meeting-recorder-mcp/0.1.0") is True

    def test_matches_regardless_of_version(self):
        assert mbs.is_mcp_user_agent("meeting-recorder-mcp/9.9.9") is True

    def test_matches_with_trailing_detail(self):
        # httpx appends its own token in some configurations.
        assert mbs.is_mcp_user_agent(
            "meeting-recorder-mcp/0.1.0 python-httpx/0.27.0") is True

    def test_is_case_insensitive(self):
        assert mbs.is_mcp_user_agent("Meeting-Recorder-MCP/0.1.0") is True

    def test_does_not_match_the_chrome_extension(self):
        """The extension calls the same backend with the same token. If
        it counted, the card would report an assistant connection the
        moment the user opened Outlook Web."""
        assert mbs.is_mcp_user_agent(
            "Mozilla/5.0 (Windows NT 10.0) Chrome/140.0") is False

    def test_does_not_match_a_bare_http_client(self):
        for ua in ("python-httpx/0.27.0", "curl/8.4.0", "PostmanRuntime/7.3"):
            assert mbs.is_mcp_user_agent(ua) is False, ua

    def test_does_not_match_empty_or_missing(self):
        assert mbs.is_mcp_user_agent("") is False
        assert mbs.is_mcp_user_agent(None) is False

    def test_does_not_match_a_lookalike_prefix(self):
        """"meeting-recorder" alone is the app itself, not the MCP
        server. Only the -mcp client counts."""
        assert mbs.is_mcp_user_agent("meeting-recorder/2.75.0") is False


class TestActivityRecord:
    def test_starts_empty(self):
        act = mbs.ClientActivity()
        assert act.last_seen_iso() is None

    def test_records_a_matching_agent(self):
        act = mbs.ClientActivity()
        act.record("meeting-recorder-mcp/0.1.0")
        assert act.last_seen_iso() is not None

    def test_ignores_a_non_matching_agent(self):
        """A silent no-op, not an error — most requests to this backend
        are the UI's own and must not register as assistant traffic."""
        act = mbs.ClientActivity()
        act.record("Mozilla/5.0")
        assert act.last_seen_iso() is None

    def test_keeps_the_most_recent(self):
        act = mbs.ClientActivity()
        act.record("meeting-recorder-mcp/0.1.0")
        first = act.last_seen_iso()
        act._clock = lambda: 10_000_000.0  # far future
        act.record("meeting-recorder-mcp/0.1.0")
        assert act.last_seen_iso() != first

    def test_never_raises_on_junk(self):
        """This runs inside request handling. It may not throw."""
        act = mbs.ClientActivity()
        for junk in (None, "", b"bytes", 42, object()):
            act.record(junk)  # type: ignore[arg-type]
        assert act.last_seen_iso() is None
