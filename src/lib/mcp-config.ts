/**
 * The MCP client configuration the Settings card hands the user.
 *
 * A separate module rather than inline in settings-view.tsx for the
 * same reason `health_payload` and `calendar_availability` are pure
 * functions in the backend: this is the string a user pastes into
 * another program, and it needs to be assertable without rendering a
 * 3,000-line settings screen.
 *
 * v2.72.0 built it inline and shipped a POSIX line continuation in it.
 * cmd.exe has no line continuation, so the first Windows user to paste
 * it registered an MCP server whose command was a single backslash,
 * and the retry then refused to overwrite the broken entry. See
 * mcp-config.test.ts.
 *
 * TWO SHAPES, TWO ESCAPING RULES, deliberately not unified:
 *
 *   CLI  — a shell command. Paths are wrapped in double quotes because
 *          macOS puts app data under "Application Support" and an
 *          unquoted space splits one argument into two. Backslashes
 *          stay verbatim; a shell is not JSON and escaping them would
 *          name a path that does not exist.
 *   JSON — a config file. JSON.stringify escapes the backslashes, which
 *          is correct here and wrong above.
 */

export type McpClientKind = "cli" | "json";

export interface McpClient {
  id: string;
  label: string;
  kind: McpClientKind;
  /** Where this client keeps its MCP config. A snippet with no stated
   *  destination is a support ticket. */
  where: string;
}

export const MCP_CLIENTS: McpClient[] = [
  {
    id: "claude-code",
    label: "Claude Code",
    kind: "cli",
    where:
      "Run it in any terminal — it is one line, so paste it whole. "
      + "--scope user makes it available in every project.",
  },
  {
    id: "claude-desktop",
    label: "Claude Desktop",
    kind: "json",
    where:
      "Settings → Developer → Edit Config (claude_desktop_config.json), "
      + "then restart Claude Desktop.",
  },
  {
    id: "cursor",
    label: "Cursor",
    kind: "json",
    where: "Settings → MCP → Add new global MCP server (~/.cursor/mcp.json).",
  },
  {
    id: "vscode",
    label: "VS Code (Cline / Continue)",
    kind: "json",
    where:
      "Your extension's MCP settings — Cline: “MCP Servers → Configure”; "
      + "Continue: the mcpServers block in config.json.",
  },
  {
    id: "other",
    label: "Other MCP client",
    kind: "json",
    where:
      "Any client that speaks MCP over stdio takes this shape; some nest "
      + "it under a different top-level key.",
  },
];

/** The name the server registers under, in every client. */
export const MCP_SERVER_NAME = "meeting-recorder";

export function mcpClient(id: string): McpClient {
  // Falls back to the JSON shape rather than returning undefined: the
  // picker's state is a plain string, and an id that no longer exists
  // must not render "undefined" into something the user pastes.
  return MCP_CLIENTS.find((c) => c.id === id) ?? MCP_CLIENTS[1];
}

/**
 * The config for one client, with this machine's real paths in it.
 *
 * `python` and `launcher` come from the backend (GET /integrations/mcp/
 * status), which resolves them from the interpreter it is itself
 * running on — the user never has to know where their install lives.
 * They are empty strings until it answers; the snippet stays valid and
 * simply carries empty paths rather than "undefined".
 *
 * No token appears anywhere. The server resolves the app's address and
 * credentials from disk by itself, and a token here would end up in
 * shared config files and screen-shared onboarding sessions.
 */
export function mcpConfigSnippet(
  clientId: string,
  python: string,
  launcher: string,
): string {
  const client = mcpClient(clientId);
  if (client.kind === "cli") {
    // ONE LINE. See the module comment — a trailing backslash here is
    // a broken registration on every Windows machine.
    return `claude mcp add ${MCP_SERVER_NAME} --scope user`
      + ` -- "${python}" "${launcher}"`;
  }
  return JSON.stringify(
    { mcpServers: { [MCP_SERVER_NAME]: { command: python, args: [launcher] } } },
    null,
    2,
  );
}
