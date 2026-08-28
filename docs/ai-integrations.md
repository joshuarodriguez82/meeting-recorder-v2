# Connecting an AI assistant to Meeting Recorder

Meeting Recorder exposes your meeting archive to AI assistants through
**two doors**. Both are read-only, both are local, and both authenticate
with the same token.

| Door | Use it when | Works with |
| --- | --- | --- |
| **MCP server** (`mcp-server/`) | your tool speaks MCP | Claude Desktop, Claude Code, Cursor, VS Code (Copilot / Cline / Continue), Zed, Windsurf, and any other MCP client |
| **HTTP + OpenAPI** (the backend itself) | your tool doesn't speak MCP | custom GPTs, LangChain / LlamaIndex, n8n, Zapier, a script, anything that can call a REST API |

**MCP is an open protocol, not a Claude feature.** The same server works
across every client in the first row — the only thing that differs is
where you paste the config.

Both doors require the **desktop app to be running**: it owns the
backend process, the semantic index and the token. Nothing here reaches
a cloud service, and nothing here can modify your data.

---

## Before you start: base URL and token

Two values that are NOT fixed constants — read them, don't hardcode them.

**Port.** The app prefers `17645` but falls back to an OS-assigned port
if something else already holds it. Check the app's Settings → Data &
Diagnostics, or read the log line `Backend listening on 127.0.0.1:<port>`
in `backend.log`.

**Token.** A 64-character hex string the app generates on first launch
and reuses afterwards. It lives in a file named `extension-token`:

| OS | Path |
| --- | --- |
| Windows | `%LOCALAPPDATA%\MeetingRecorder\extension-token` |
| macOS | `~/Library/Application Support/MeetingRecorder/extension-token` |
| Linux | `$XDG_DATA_HOME/MeetingRecorder/extension-token` |

> **Treat it as a password.** Anything holding this token can read every
> transcript, summary and document in your archive. Don't paste it into
> a ticket, a shared doc, or a cloud tool's config field that other
> people can read. If it leaks: quit the app, delete that file, relaunch
> — a fresh token is generated and the Chrome extension will ask to be
> re-paired.

The MCP server finds both of these by itself. You only need them for the
HTTP door.

---

## Door 1 — MCP

Install once:

```sh
cd mcp-server
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
python -m meeting_recorder_mcp --doctor    # should print "OK"
```

Then point your client at the **absolute path of that venv's Python**.
Clients launch the server with a minimal environment and do not activate
virtualenvs, so a bare `python` usually resolves to an interpreter with
no `mcp` installed and fails with no obvious reason.

### Claude Code

```sh
claude mcp add meeting-recorder --scope user \
  -- /absolute/path/to/mcp-server/.venv/bin/python -m meeting_recorder_mcp
```

### Claude Desktop, Cursor, Windsurf, Zed, Cline, Continue

All of these use the same JSON shape; only the file differs.

```json
{
  "mcpServers": {
    "meeting-recorder": {
      "command": "/absolute/path/to/mcp-server/.venv/bin/python",
      "args": ["-m", "meeting_recorder_mcp"]
    }
  }
}
```

| Client | Where the config lives |
| --- | --- |
| Claude Desktop | Settings → Developer → Edit Config |
| Cursor | Settings → MCP → Add new server (or `~/.cursor/mcp.json`) |
| Windsurf | Settings → Cascade → MCP servers |
| Cline / Continue (VS Code) | the extension's MCP settings panel |
| Zed | `settings.json` → `context_servers` |

On Windows use the full path to `.venv\Scripts\python.exe` and escape
backslashes in JSON.

### What it gives the assistant

| Tool | Answers |
| --- | --- |
| `search_meetings` | "what did we say about the cutover window?" — semantic search across every transcript **and** client Knowledge Folder document |
| `ask_knowledge_base` | a cited answer rather than a list of hits |
| `list_open_commitments` | "what do I still owe?" — outstanding commitments, **overdue first**, with owner, due date, client and the session to cite |
| `list_clients` | your clients and their indexed document counts |
| `list_sessions` | recent meetings, filterable by client/project |
| `get_session` | one meeting's transcript / summary / actions / decisions / requirements |

All are read-only: no tool can delete a session, change a setting, or
start a reindex.

---

## Door 2 — HTTP + OpenAPI

For anything that doesn't speak MCP. The backend is a normal REST API
with a machine-readable spec: **120 documented endpoints**.

```sh
BASE=http://127.0.0.1:17645
TOKEN=$(cat ~/Library/Application\ Support/MeetingRecorder/extension-token)

# The full OpenAPI 3 spec — hand this to any tool that consumes one
curl -s "$BASE/openapi.json" -H "Authorization: Bearer $TOKEN" > meeting-recorder.json

# Or browse it
open "$BASE/docs?token=$TOKEN"
```

Authenticate with either `Authorization: Bearer <token>` or `?token=<token>`
(the query form exists for clients that cannot set headers).

Endpoints worth knowing:

| Endpoint | Returns |
| --- | --- |
| `GET /health` | liveness — the only endpoint that needs no token |
| `GET /sessions` | every recorded meeting |
| `GET /sessions/{id}` | one meeting in full |
| `POST /search/semantic` | semantic search over meetings + documents |
| `GET /commitments?status=overdue` | outstanding commitments |
| `GET /insights/summary` | cross-meeting analytics and open loops |

### Feeding the spec to a custom GPT or agent framework

The spec is standard OpenAPI 3, so most tools accept it directly. Two
things to get right:

- **The API is localhost-only.** A cloud-hosted assistant (a custom GPT,
  a hosted agent) cannot reach `127.0.0.1` on your laptop. Those need a
  local agent or a tunnel, and a tunnel exposes your archive to whoever
  finds the URL — think before you open one.
- **120 endpoints is more than most tools want.** Trim the spec to the
  handful above before uploading; it reduces both confusion and how much
  of your surface a third party sees.

---

## Troubleshooting

Run the doctor first — it distinguishes the three failure modes:

```sh
python -m meeting_recorder_mcp --doctor
```

| Exit | Meaning | Fix |
| --- | --- | --- |
| `0` | healthy | — |
| `2` | can't reach the backend, or no token found | start the app; confirm the port |
| `3` | token rejected | the token was rotated — re-read the file |

To test the MCP protocol independently of the app:

```sh
python scripts/handshake_check.py   # real stdio client: initialize + list_tools
python scripts/e2e_stub_check.py    # every tool against a stub backend
```

**"The tools don't appear."** Restart the client fully (quit, don't just
close the window). Confirm you used the absolute venv Python path.

**"It says the app isn't running."** It isn't, or it's on a different
port. The backend only exists while the desktop app is open.
