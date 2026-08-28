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

**Port.** The app prefers `17645` and falls back to an OS-assigned port
if something else already holds it. It writes whichever it got to a
`backend-port` file **beside the token** (same folder as the table
below), so you rarely need to look it up:

- **The MCP server reads that file automatically.** Nothing to configure.
- **Anything else** can read the same file, or take the port from the
  app's Settings → Templates & Integrations → AI assistant access.

Precedence if you want to override: `MEETING_RECORDER_URL` beats
`MEETING_RECORDER_PORT`, which beats the port file, which beats the
pinned `17645`. An explicit override always wins, so a tunnel or a
stand-in backend is never overruled by whatever the last local run
wrote.

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

### Read this first: every tool is set up separately

This is the thing that costs people an evening. **Configuring one AI
tool does not configure the others.** They do not share settings:

| Tool | Reads |
| --- | --- |
| Claude Code | `~/.claude.json` (Windows: `C:\Users\<you>\.claude.json`) |
| Claude Desktop | `claude_desktop_config.json`, a different file |
| Cursor | `~/.cursor/mcp.json` |
| VS Code (Cline / Continue) | the extension's own settings |

Running `claude mcp add` sets up **Claude Code only**. Claude Desktop
will still see nothing, because nothing wrote its file.

And one more that catches everyone: **a Claude session running in the
cloud can never see this.** claude.ai in a browser, Claude on your
phone, and a remote Claude Code session all run on Anthropic's machines,
and this server is a program on your disk talking to `127.0.0.1`. It
works in tools running **on the same computer as the app** — that is not
a bug to work around, it is why your transcripts never leave your
machine. Check which kind you are in by asking the session to run `pwd`:
a path on your own disk means local.

### The one-click path (installed builds)

Open **Settings → Templates & Integrations → AI assistant access**.

1. Click **Turn on** — once, ever. That installs the MCP protocol
   library into the app's own Python. Nothing else to download, no
   virtualenv of your own, and the app keeps working whether or not it
   succeeds.
2. Pick your AI tool. Each button shows that tool's own state: a green
   tick means it is set up, an amber warning means it is set up but
   pointing at paths that have moved.
3. Click **Set up <tool>**. The app writes that tool's config file for
   you — merging into whatever is already there, keeping your other
   servers and settings, and backing the file up first.
4. **Quit that tool completely and reopen it.** On Windows that means
   the system-tray icon too, not just the window.

Repeat steps 2–4 for each tool you use.

**Claude Desktop and Cursor** are written for you. **Claude Code** keeps
its one-line command, because it owns its own config file and has a
supported CLI for this. **VS Code** stays manual, because the location
depends on which extension you have installed and guessing wrong writes
a file nothing reads.

Whatever the app writes, the equivalent snippet is on the card too, with
the two absolute paths already filled in for your machine. There is
nothing to substitute and no token to copy — the server finds the app's
address and token by itself.

Why the paths cannot be guessed: they live under your per-user app data
directory, which differs by OS and by user. That is exactly why the card
resolves them from the running backend instead of printing a
placeholder.

> **"This build doesn't carry the MCP server files."** You are running
> from a source checkout that was never packaged with `zip-bundle.py`.
> Use the manual path below.

### The manual path (source checkouts)

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

#### Claude Code

One line. A trailing `\` is POSIX line continuation and means nothing to
`cmd.exe`, which registers a server whose command is literally `\` and
silently drops the rest — so this is written unbroken on purpose, and
should be pasted the same way.

```sh
claude mcp add meeting-recorder --scope user -- /absolute/path/to/mcp-server/.venv/bin/python -m meeting_recorder_mcp
```

On Windows, quote both paths:

```
claude mcp add meeting-recorder --scope user -- "C:\path\to\mcp-server\.venv\Scripts\python.exe" -m meeting_recorder_mcp
```

#### Claude Desktop, Cursor, Windsurf, Zed, Cline, Continue

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

### Checking it works

Either path, run the launcher's doctor by hand — it never speaks MCP, so
it is safe to run in a terminal:

```sh
# installed build — both paths are on the Settings card
"<the app's python>" "<the launcher>" --doctor

# source checkout
python -m meeting_recorder_mcp --doctor
```

It prints the base URL it resolved, where it found the token, and
whether an authenticated call succeeded. That is the fastest way to tell
"the app isn't running" apart from "the client's config is wrong".

### What it gives the assistant

| Tool | Answers |
| --- | --- |
| `search_meetings` | "what did we say about the cutover window?" — semantic search across every transcript **and** client Knowledge Folder document |
| `ask_knowledge_base` | a cited answer rather than a list of hits |
| `list_open_commitments` | "what do I still owe?" — outstanding commitments, **overdue first**, with owner, due date, client and the session to cite |
| `list_clients` | your clients, their indexed document counts, and the portal opportunity each is bound to |
| `list_meetings` | recent meetings, filterable by client/project |
| `get_meeting` | one meeting's transcript / summary / actions / decisions / requirements |
| `get_portal_binding` | "which SA Tools Portal opportunity is this client bound to?" — the `customerId` to cross systems by, plus a warning if the binding points at a parent company rather than an engagement |

> **Renamed in v2.72.0:** `list_sessions` → `list_meetings` and
> `get_session` → `get_meeting`. "Session" also means a Claude Code
> session, and an ambiguous tool name is one an assistant gets right
> only *sometimes*. The id parameter is still `session_id` — that is the
> backend's key, in every stored file and export. If you had a saved
> prompt naming the old tools, update it.

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
# installed build (both paths come from the Settings card)
"<the app's python>" "<the launcher>" --doctor

# source checkout
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

**"The tools don't appear."** In order:

1. **Is it that tool that you set up?** Setting up Claude Code does not
   set up Claude Desktop. The card shows a tick against each tool it has
   written for; check the one you are actually using.
2. **Is the session running on this computer?** A cloud session — the
   browser, your phone, a remote Claude Code session — cannot reach a
   server on your disk, ever. Ask it to run `pwd`.
3. **Did the tool restart fully?** Quit it, including any system-tray
   icon, and reopen.
4. **Is Meeting Recorder running?** The backend only exists while the
   app is open.
5. On a source checkout, confirm you used the venv's absolute Python
   path — a bare `python` resolves to an interpreter with no `mcp`
   installed.

**"The Turn on button failed."** The card shows pip's own output, and
the reason is in those lines: no network, a proxy that intercepts the
package index, or a version conflict. Nothing was changed — the app is
unaffected and you can click it again. The install is deliberately not
part of first launch, because a failure there would stop the app from
starting at all.

**"It says the app isn't running."** It isn't, or it's on a different
port. The backend only exists while the desktop app is open.
