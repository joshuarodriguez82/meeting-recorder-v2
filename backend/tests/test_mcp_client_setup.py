"""
Setting up each AI tool from the app, instead of asking for JSON edits.

THE FIELD REPORT
----------------
v2.72's card showed a Claude Code command and a Claude Desktop JSON
block side by side, and never said that configuring one does not
configure the other. A user ran the Claude Code command, restarted
everything, and found Claude Desktop still blind — because
`claude mcp add` writes `~/.claude.json` and Claude Desktop reads
`claude_desktop_config.json`, a different file the card never wrote.
Doing exactly what the app said, in the order it said it, produced a
setup that did not work.

Hand-editing JSON was never a reasonable ask: the app already knows both
absolute paths and the config location for every client it lists. So it
writes the file.

WHAT THE TESTS PIN
------------------
Mostly the ways writing someone else's config file can go wrong:

  - merging into a config that already has other servers, without
    disturbing them or any unrelated top-level key;
  - replacing a stale entry (the app's data directory moved) rather
    than appending a second one;
  - refusing to write at all when the existing file is unparseable —
    the alternative is silently destroying a config we could not read,
    which is the same defect as rendering an unreadable result as an
    absent one;
  - per-platform config locations, because getting these wrong writes a
    file nothing ever reads and reports success.

All pure functions over strings and paths. Nothing here touches a real
config file.
"""

from __future__ import annotations

import json

import pytest

from services import mcp_client_setup as mcs

PY = r"C:\Users\sampleuser\AppData\Local\MeetingRecorder\.venv\Scripts\python.exe"
LAUNCHER = r"C:\Users\sampleuser\AppData\Local\MeetingRecorder\runtime\mcp-server\run_mcp_server.py"


class TestConfigPaths:
    def test_claude_desktop_windows(self):
        p = mcs.config_path("claude-desktop", platform="win32",
                            env={"APPDATA": r"C:\Users\sampleuser\AppData\Roaming"},
                            home="/unused")
        # Asserted on parts, not on separators: pathlib renders with the
        # RUNNER's separator, so a Linux CI box cannot produce
        # backslashes here no matter what the code does. The parts are
        # the actual contract.
        assert p is not None
        assert p.name == "claude_desktop_config.json"
        assert p.parent.name == "Claude"
        assert r"C:\Users\sampleuser\AppData\Roaming" in str(p)

    def test_claude_desktop_macos(self):
        p = mcs.config_path("claude-desktop", platform="darwin", env={},
                            home="/Users/sampleuser")
        assert str(p) == ("/Users/sampleuser/Library/Application Support/"
                          "Claude/claude_desktop_config.json")

    def test_cursor_is_the_same_place_on_every_platform(self):
        for plat, home in (("win32", r"C:\Users\sampleuser"),
                           ("darwin", "/Users/sampleuser")):
            p = mcs.config_path("cursor", platform=plat, env={}, home=home)
            assert p.name == "mcp.json"
            assert ".cursor" in str(p)

    def test_a_client_we_do_not_write_for_has_no_path(self):
        """Claude Code owns ~/.claude.json and has its own CLI for this;
        VS Code's location depends on which extension is installed. Both
        keep the copy-a-snippet path rather than us guessing at a file
        and reporting success for a write nothing reads."""
        assert mcs.config_path("claude-code", platform="darwin", env={},
                               home="/Users/sampleuser") is None
        assert mcs.config_path("vscode", platform="darwin", env={},
                               home="/Users/sampleuser") is None

    def test_unknown_client(self):
        assert mcs.config_path("not-a-client", platform="darwin", env={},
                               home="/h") is None

    def test_windows_without_appdata_is_none_not_a_guess(self):
        """A path built from an empty APPDATA lands somewhere arbitrary
        and the write "succeeds" into a file no client reads."""
        assert mcs.config_path("claude-desktop", platform="win32", env={},
                               home=r"C:\Users\sampleuser") is None


class TestMerge:
    def test_creates_the_block_when_the_file_does_not_exist(self):
        out = mcs.merge_entry(None, PY, LAUNCHER)
        parsed = json.loads(out)
        entry = parsed["mcpServers"]["meeting-recorder"]
        assert entry["command"] == PY
        assert entry["args"] == [LAUNCHER]

    def test_creates_the_block_in_an_empty_file(self):
        parsed = json.loads(mcs.merge_entry("", PY, LAUNCHER))
        assert "meeting-recorder" in parsed["mcpServers"]

    def test_leaves_other_servers_alone(self):
        existing = json.dumps({"mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
        }})
        parsed = json.loads(mcs.merge_entry(existing, PY, LAUNCHER))
        assert parsed["mcpServers"]["filesystem"]["command"] == "npx"
        assert "meeting-recorder" in parsed["mcpServers"]

    def test_leaves_unrelated_top_level_keys_alone(self):
        """Claude Desktop keeps other settings in this file. Rewriting it
        from scratch would silently drop them."""
        existing = json.dumps({"globalShortcut": "Alt+Space",
                               "theme": "dark", "mcpServers": {}})
        parsed = json.loads(mcs.merge_entry(existing, PY, LAUNCHER))
        assert parsed["globalShortcut"] == "Alt+Space"
        assert parsed["theme"] == "dark"

    def test_replaces_a_stale_entry_rather_than_adding_a_second(self):
        """The app's data directory can move between installs. A second
        entry under a different name would leave the client launching a
        python that no longer exists."""
        existing = json.dumps({"mcpServers": {"meeting-recorder": {
            "command": "/old/path/python", "args": ["/old/path/run.py"]}}})
        parsed = json.loads(mcs.merge_entry(existing, PY, LAUNCHER))
        assert len(parsed["mcpServers"]) == 1
        assert parsed["mcpServers"]["meeting-recorder"]["command"] == PY

    def test_refuses_to_write_over_a_config_it_cannot_parse(self):
        """Never destroy a file we could not read. The user gets told to
        fix it or paste the snippet by hand."""
        with pytest.raises(mcs.ConfigUnreadable):
            mcs.merge_entry("{ this is not json", PY, LAUNCHER)

    def test_refuses_when_the_top_level_is_not_an_object(self):
        with pytest.raises(mcs.ConfigUnreadable):
            mcs.merge_entry("[1, 2, 3]", PY, LAUNCHER)

    def test_refuses_when_mcpservers_is_not_an_object(self):
        with pytest.raises(mcs.ConfigUnreadable):
            mcs.merge_entry(json.dumps({"mcpServers": "nope"}), PY, LAUNCHER)

    def test_output_is_readable_json(self):
        """Someone will open this file. Indent it."""
        out = mcs.merge_entry(None, PY, LAUNCHER)
        assert "\n" in out and "  " in out

    def test_windows_paths_survive_the_round_trip(self):
        """JSON escaping is the whole reason hand-editing goes wrong."""
        parsed = json.loads(mcs.merge_entry(None, PY, LAUNCHER))
        assert parsed["mcpServers"]["meeting-recorder"]["command"] == PY


class TestEntryState:
    def test_absent(self):
        assert mcs.entry_state(None, PY, LAUNCHER) == "absent"
        assert mcs.entry_state(json.dumps({"mcpServers": {}}), PY,
                               LAUNCHER) == "absent"

    def test_current(self):
        text = mcs.merge_entry(None, PY, LAUNCHER)
        assert mcs.entry_state(text, PY, LAUNCHER) == "current"

    def test_stale_when_the_paths_moved(self):
        """The state that produces a client which lists the server and
        fails every call — worth naming separately from 'absent' so the
        UI can say "set up, but pointing somewhere else"."""
        old = mcs.merge_entry(None, "/old/python", "/old/run.py")
        assert mcs.entry_state(old, PY, LAUNCHER) == "stale"

    def test_unreadable_is_its_own_state_not_absent(self):
        assert mcs.entry_state("{ broken", PY, LAUNCHER) == "unreadable"
