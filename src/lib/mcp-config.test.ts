/**
 * The string the user actually pastes.
 *
 * v2.72.0 shipped this snippet with a POSIX line continuation:
 *
 *     claude mcp add meeting-recorder --scope user \
 *       -- "<python>" "<launcher>"
 *
 * cmd.exe has no line continuation. The first Windows user to paste it
 * got:
 *
 *     Added stdio MCP server meeting-recorder with command: \
 *     MCP server meeting-recorder already exists in user config
 *
 * — a server registered with a backslash as its command, and a retry
 * that refused to overwrite it. Worse than failing outright.
 *
 * The backend half of that feature had 21 tests. This string had none,
 * because there was no frontend harness to put one in. These are the
 * first tests in it.
 */

import { describe, expect, it } from "vitest";
import { mcpConfigSnippet, MCP_CLIENTS } from "@/lib/mcp-config";

// Realistic inputs: Windows has backslashes and no spaces; macOS has a
// space in the middle of the path, courtesy of "Application Support".
const WIN_PY = "C:\\Users\\sampleuser\\AppData\\Local\\MeetingRecorder\\.venv\\Scripts\\python.exe";
const WIN_LAUNCHER = "C:\\Users\\sampleuser\\AppData\\Local\\MeetingRecorder\\runtime\\mcp-server\\run_mcp_server.py";
const MAC_PY = "/Users/sampleuser/Library/Application Support/MeetingRecorder/.venv/bin/python3";
const MAC_LAUNCHER = "/Users/sampleuser/Library/Application Support/MeetingRecorder/runtime/mcp-server/run_mcp_server.py";

describe("the Claude Code command", () => {
  it("is a single line", () => {
    // THE REGRESSION. A trailing backslash is POSIX line continuation
    // and means nothing to cmd.exe, which takes the backslash as the
    // whole command and silently drops everything after it.
    const s = mcpConfigSnippet("claude-code", WIN_PY, WIN_LAUNCHER);
    expect(s).not.toContain("\n");
    expect(s).not.toContain("\\\n");
  });

  it("quotes both paths so a space cannot split an argument", () => {
    // macOS puts the app's data under "Application Support". Unquoted,
    // the shell sees two arguments and the server never starts.
    const s = mcpConfigSnippet("claude-code", MAC_PY, MAC_LAUNCHER);
    expect(s).toContain(`"${MAC_PY}"`);
    expect(s).toContain(`"${MAC_LAUNCHER}"`);
  });

  it("passes the paths after -- so the CLI does not parse them as flags", () => {
    const s = mcpConfigSnippet("claude-code", WIN_PY, WIN_LAUNCHER);
    expect(s.indexOf("--scope user")).toBeLessThan(s.indexOf("--"
      + ` "${WIN_PY}"`));
  });

  it("keeps Windows backslashes verbatim", () => {
    // Shell arguments are not JSON; escaping them here would produce a
    // path that does not exist.
    const s = mcpConfigSnippet("claude-code", WIN_PY, WIN_LAUNCHER);
    expect(s).toContain(WIN_PY);
    expect(s).not.toContain("\\\\Users");
  });
});

describe("the JSON config", () => {
  it("is valid JSON for every JSON-shaped client", () => {
    for (const client of MCP_CLIENTS.filter((c) => c.kind === "json")) {
      const s = mcpConfigSnippet(client.id, WIN_PY, WIN_LAUNCHER);
      expect(() => JSON.parse(s), `${client.id} produced unparseable JSON`)
        .not.toThrow();
    }
  });

  it("escapes Windows backslashes, because JSON is not a shell", () => {
    // The inverse of the CLI rule above, and the reason these are two
    // code paths rather than one string with quotes around it.
    const s = mcpConfigSnippet("claude-desktop", WIN_PY, WIN_LAUNCHER);
    expect(s).toContain("\\\\Users");
    const parsed = JSON.parse(s);
    expect(parsed.mcpServers["meeting-recorder"].command).toBe(WIN_PY);
  });

  it("puts the launcher in args, not in the command", () => {
    // Clients spawn `command` with `args`; folding both into one string
    // makes the whole thing an executable path that does not exist.
    const parsed = JSON.parse(mcpConfigSnippet("cursor", MAC_PY, MAC_LAUNCHER));
    const entry = parsed.mcpServers["meeting-recorder"];
    expect(entry.command).toBe(MAC_PY);
    expect(entry.args).toEqual([MAC_LAUNCHER]);
  });

  it("carries no token", () => {
    // The server resolves the app's address and token from disk. A
    // token in the snippet would end up pasted into shared configs and
    // screen-shared during onboarding.
    const s = mcpConfigSnippet("claude-desktop", MAC_PY, MAC_LAUNCHER).toLowerCase();
    expect(s).not.toContain("token");
    expect(s).not.toContain("authorization");
  });
});

describe("the client list", () => {
  it("gives every client somewhere to put the snippet", () => {
    // A config with no stated destination is a support ticket.
    for (const c of MCP_CLIENTS) {
      expect(c.where, `${c.id} has no destination`).toBeTruthy();
      expect(c.label).toBeTruthy();
    }
  });

  it("has no duplicate ids", () => {
    const ids = MCP_CLIENTS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("falls back to the JSON shape for an unknown client id", () => {
    // The picker's state is a plain string; an id that no longer exists
    // must not render `undefined` into something the user pastes.
    const s = mcpConfigSnippet("not-a-real-client", MAC_PY, MAC_LAUNCHER);
    expect(() => JSON.parse(s)).not.toThrow();
  });
});

describe("before the paths are known", () => {
  it("never renders undefined or null into the snippet", () => {
    // The card asks the backend for these; until it answers they are
    // empty strings. An "undefined" in a pasted config fails with no
    // diagnosable error in every client.
    for (const client of MCP_CLIENTS) {
      const s = mcpConfigSnippet(client.id, "", "");
      expect(s).not.toContain("undefined");
      expect(s).not.toContain("null");
    }
  });
});
