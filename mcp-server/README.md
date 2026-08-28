# Meeting Recorder MCP server

Gives Claude Desktop and Claude Code read-only access to your Meeting
Recorder archive — recorded meeting transcripts, summaries, action items,
decisions and requirements, plus every document in your clients' Knowledge
Folders — through the app's **own semantic index**, not by file-searching
Google Drive.

- **Transport:** stdio (what Claude Desktop and Claude Code speak).
- **SDK:** the official Anthropic Python MCP SDK, `mcp >= 2.0` (in 2.0 the
  high-level server class was renamed `FastMCP` → `MCPServer` and moved to
  `mcp.server.mcpserver`; code written against the 1.x `mcp.server.fastmcp`
  import will not run).
- **Scope:** read-only. It cannot delete a session, change a setting, or
  start a reindex.
- **Isolation:** this directory is standalone. It adds nothing to
  `backend/requirements-*.txt` and imports nothing from `backend/` — it
  talks to the app over HTTP like any other client. It *ships* inside the
  app's runtime bundle (`zip-bundle.py`) so an installed build can turn it
  on without a checkout, but that is packaging, not coupling.

---

## Requirements

- Python 3.10+
- The Meeting Recorder desktop app installed. **The backend only runs while
  the app is open** — every tool here fails with a clear "start the app"
  message when it isn't.

## Install

**Most people should not do any of this.** An installed build of the app
ships this whole directory inside its runtime bundle and turns it on from
**Settings → Templates & Integrations → AI assistant access** — one button,
then a config snippet with both absolute paths already filled in for that
machine. See `docs/ai-integrations.md`.

What that button does, so it isn't a black box: it pip-installs `mcp` into
the app's own venv (with the same constraints file the app was
bootstrapped with, so it cannot move a pinned backend dependency), then
hands the client `<app python> <runtime>/mcp-server/run_mcp_server.py`.
`run_mcp_server.py` puts its own directory on `sys.path`, which is why no
install of *this package* is needed there. The wiring lives in
`backend/services/mcp_bundle_service.py`.

The rest of this section is the **developer** path — a source checkout,
where you want an editable install and the test suite.

```sh
cd mcp-server
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -e .
```

## Verify it works

Start Meeting Recorder, then:

```sh
python -m meeting_recorder_mcp --doctor
```

Healthy output:

```
Meeting Recorder MCP — connection check
  base URL: http://127.0.0.1:17645
  token:    a1b2c3...9f0e  (from /Users/you/Library/Application Support/MeetingRecorder/extension-token)
  health:   ok (backend reports version 2.0.0)
  auth:     ok
  index:    71 of 73 sessions embedded

OK — the MCP server can reach Meeting Recorder.
```

Exit codes: `0` healthy, `2` can't reach the backend or no token, `3` token
rejected.

To prove the MCP protocol side independently of the app:

```sh
python scripts/handshake_check.py   # real stdio client: initialize + list_tools
python scripts/e2e_stub_check.py    # every tool against a stub backend over real HTTP
```

---

## Configure Claude Desktop

Edit `claude_desktop_config.json`:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

(In the app: **Settings → Developer → Edit Config** opens the same file.)

**macOS / Linux**

```json
{
  "mcpServers": {
    "meeting-recorder": {
      "command": "/absolute/path/to/meeting-recorder-v2/mcp-server/.venv/bin/python",
      "args": ["-m", "meeting_recorder_mcp"]
    }
  }
}
```

**Windows**

```json
{
  "mcpServers": {
    "meeting-recorder": {
      "command": "C:\\path\\to\\meeting-recorder-v2\\mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "meeting_recorder_mcp"]
    }
  }
}
```

`pip install -e .` also puts a `meeting-recorder-mcp` console script in the
venv's `bin`/`Scripts`; using it instead of `python -m …` (with `"args": []`)
works identically.

Use the **absolute path to the venv's Python**. Claude Desktop launches the
server with a minimal environment and does not activate virtualenvs; a bare
`"python"` will usually resolve to a system interpreter that has no `mcp`
installed, and the server will fail to start with no obvious reason.

Restart Claude Desktop fully (quit, don't just close the window). The tools
appear under the tools icon in the composer.

## Configure Claude Code

Keep it on one line — a trailing `\` is POSIX line continuation and means
nothing to `cmd.exe`, which takes the `\` as the whole command and drops
the rest.

```sh
claude mcp add meeting-recorder -- /absolute/path/to/mcp-server/.venv/bin/python -m meeting_recorder_mcp
```

Add `--scope user` to make it available in every project rather than just
the current one. Then confirm:

```sh
claude mcp list
```

Or commit it to a project by writing `.mcp.json` at the repo root:

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

## This does NOT work with claude.ai in a browser

claude.ai connects to **remote** MCP servers over a publicly reachable
HTTPS URL. This server speaks stdio to a process on your machine and talks
to a backend bound to `127.0.0.1`. There is no URL for claude.ai to reach,
and exposing one would mean publishing an endpoint that serves confidential
client transcripts and SOWs to the internet. Use Claude Desktop or Claude
Code.

---

## Tools

All five are annotated read-only, non-destructive. All return plain text.

### `search_meetings(query, client?, project?, top_k?)`
Semantic search over the whole index. Every hit is labelled **MEETING** or
**DOCUMENT** and carries its own provenance:

```
Semantic search for 'when is the cutover?'
3 result(s): 2 from meeting transcripts, 1 from Knowledge Folder documents.

[1] MEETING — ACME — Discovery Call
    date: 2026-07-14 10:15    client/project: ACME / CCaaS Migration
    session_id: session_20260714_101500    at 06:52-07:28    similarity: 0.812
    excerpt: We agreed the cutover window is the first weekend in October, ...
    (cite as: "ACME — Discovery Call", 2026-07-14 10:15; full text via get_session('session_20260714_101500'))

[2] DOCUMENT — ACME_SOW_v3.docx
    client: ACME    similarity: 0.771
    path: /Users/sampleuser/Knowledge/ACME/ACME_SOW_v3.docx
    excerpt: Cutover is scheduled for the first weekend of October 2026. ...
    (this is a Knowledge Folder document, NOT a meeting — it has no session_id
     and cannot be passed to get_session; cite it by filename)
```

The distinction is load-bearing: the backend returns a discriminated union
and a document hit genuinely has no `session_id`. `top_k` is clamped to
1–50. Note that a `project` filter excludes documents entirely — documents
are scoped to a client and have no project.

### `ask_knowledge_base(question, client?, top_k?)`
Wraps the app's streaming Q&A. Returns the synthesised answer plus the
meetings and documents it drew on, each with full provenance. Requires an
AI provider configured in the app; if none is, it says so.

### `list_clients()`
Every client with its designated (export) folder, Knowledge Folder, whether
that folder is currently reachable, indexed document/chunk counts, and
overall index coverage. Also warns when a client's folders overlap (see
below). Use it to get exact client names for the other tools' filters.

### `list_sessions(client?, project?, limit?)`
Recent meetings, newest first, with `session_id`, date, duration,
client/project, which parts actually exist, and any audio-integrity or
processing warnings. `limit` is clamped to 1–200 (default 20). Filters
match names exactly.

### `get_session(session_id, part)`
One part of one meeting. `part` is `transcript`, `summary`, `action_items`,
`decisions`, `requirements`, or `metadata` (defaults to `summary`).
Transcripts are rendered with speaker names and timestamps and capped at
12,000 characters; other parts at 8,000. Truncation is always stated
inline with the omitted count, e.g.:

```
[TRUNCATED by the MCP server: showing the first 12,000 of 41,388 characters; 29,388 omitted.]
```

---

## How it finds the backend

### Port
The Tauri shell pins the backend to **`127.0.0.1:17645`** and only falls
back to an OS-assigned ephemeral port when 17645 is already taken
(`src-tauri/src/lib.rs::pick_free_port`). That fallback port is not written
to disk anywhere — the Chrome extension has the same constraint. So this
server defaults to 17645, and you override it if the app fell back:

- `MEETING_RECORDER_URL` — full base URL, e.g. `http://127.0.0.1:52111`
- `MEETING_RECORDER_PORT` / `MEETING_RECORDER_HOST` — parts, if you prefer

The app shows its real URL under **Settings → Chrome Extension**.

### Token
Every request except `/health` needs `Authorization: Bearer <token>`
(`backend/server.py::require_backend_token`). Since v2.16 the app persists
that token as a 64-character hex string in a file named **`extension-token`**
in its user data folder, probed here in this order:

| OS | Directory |
|---|---|
| Windows | `%LOCALAPPDATA%\MeetingRecorder`, then `%APPDATA%`, `%USERPROFILE%` |
| macOS | `~/Library/Application Support/MeetingRecorder` |
| Linux | `$XDG_DATA_HOME/MeetingRecorder` (default `~/.local/share/…`), then `$XDG_CONFIG_HOME/MeetingRecorder` (default `~/.config/…`) |

Linux probes two directories because the Rust shell writes the token under
the *data* dir while the Python backend keeps `config.env` under the
*config* dir.

Overrides:

- `MEETING_RECORDER_TOKEN` — use this exact token, skip the file search
- `MEETING_RECORDER_DATA_DIR` — search this directory first

To rotate: delete `extension-token` and restart Meeting Recorder.

### Other environment variables
- `MEETING_RECORDER_MCP_LOG_LEVEL` — `DEBUG` / `INFO` / `WARNING`
  (default) / `ERROR`. Logs go to stderr; stdout is the MCP wire.

---

## Failure messages and what they mean

Every failure comes back as text beginning `MEETING RECORDER ERROR —`, so a
failed call can never be mistaken for an empty one.

| Message begins | Meaning | Fix |
|---|---|---|
| `Meeting Recorder isn't running — nothing is listening on http://127.0.0.1:17645` | Nothing accepted the TCP connection. | Start the app. If it's already running, it fell back to another port — read the URL from Settings → Chrome Extension and set `MEETING_RECORDER_URL`. |
| `No Meeting Recorder auth token found, so the request was never sent` | No token in the env and no `extension-token` file in any probed directory. Nothing was sent. | Launch Meeting Recorder once (it writes the file), or set `MEETING_RECORDER_TOKEN`. The message lists every path it checked. |
| `Meeting Recorder rejected the auth token (HTTP 401)` | A token *was* sent and the backend refused it. | The token is stale — copy a fresh one from Settings → Chrome Extension, or delete `extension-token` and restart the app. The message names where the token came from. |
| `Meeting Recorder accepted the connection … but didn't answer within 30s` | Backend is alive but busy (indexing, transcribing) or wedged. | Wait and retry. Q&A gets 180s before this fires. |
| `Meeting Recorder can't serve this right now: Q&A needs both the semantic-search index and an AI provider configured…` | Backend is up; that capability isn't set up. | Follow the app's own instruction in the message (Settings → AI Provider). |
| `No session with id '…'` | Bad or stale `session_id` — most often a DOCUMENT hit was passed to `get_session`. | Use `list_sessions`, or a MEETING hit's `session_id`. |
| `unknown part '…'` | Bad `part` argument. | Use one of the six listed in the message. |
| `unexpected failure in the MCP server itself` | A bug here, not in the app. Nothing was read or changed. | Re-run with `MEETING_RECORDER_MCP_LOG_LEVEL=DEBUG` and check Claude's MCP log. |

**Empty is not an error.** Zero results always says so explicitly —
`0 result(s)` … `This is an empty result, not an error`, and an
unpopulated field says `This is an empty field, not a failed call`. If you
see a bare blank response, that's a bug worth reporting.

---

## Gotcha: don't point a Knowledge Folder at a Designated Folder

**Verified against the indexing code — this really does double-index.**

Meeting Recorder writes per-session exports into a client's **Designated
Folder** as plain text (`backend/services/export_service.py`):

```
transcript_<Meeting Name>.txt
summary_<Meeting Name>.txt
action_items_<Meeting Name>.txt
decisions_<Meeting Name>.txt
requirements_<Meeting Name>.txt
<Meeting Name>.wav          (audio copy)
```

Knowledge Folder indexing (`backend/services/document_service.py`)
walks its folder with `rglob("*")` and skips only dot-prefixed paths —
nothing excludes exported transcripts — and `.txt` is in
`_PLAIN_TEXT_EXTENSIONS`, so every one of those files is extracted,
chunked and embedded as a *document*. Nothing anywhere compares
`export_folder` to `knowledge_folder`.

So if both point at the same directory (or the Knowledge Folder is a
parent of the Designated Folder), each meeting is in the index twice:

1. as **session** chunks, from the transcript embedding, and
2. as **document** chunks, from the exported `.txt`.

`/search/semantic` ranks both, so results come back as near-duplicate
pairs under two different labels, top_k gets eaten by redundancy, and Q&A
retrieval wastes its context window on the same text twice. (The `.wav`
copy is harmless — `.wav` is in `NON_TEXT_EXTENSIONS` and is reported as an
expected skip.)

`list_clients` flags this when it sees it:

```
- Globex
    designated (export) folder: /Users/sampleuser/Drive/Globex/Exports
    knowledge folder: /Users/sampleuser/Drive/Globex/Exports
    WARNING: this client's knowledge folder is the same as (or inside) its
    designated export folder, so exported transcripts are indexed twice —
    once as meetings, once as documents. Searches will return near-duplicates.
```

**Recommended layout — sibling directories, never nested:**

```
ACME/
├── Exports/          ← Designated Folder   (app writes here; app owns it)
│   ├── transcript_Discovery Call.txt
│   └── Discovery Call.wav
└── Knowledge/        ← Knowledge Folder    (you put source docs here)
    ├── ACME_SOW_v3.docx
    ├── ACME_RFP_Response.pdf
    └── Current_Architecture.pptx
```

The Knowledge Folder is for material the app did *not* produce: SOWs,
estimates, RFPs, architecture docs, contracts, notes. Transcripts are
already fully indexed as sessions and searchable as meetings — adding them
again as documents gains nothing and costs recall.

If you've already made this mistake: repoint the Knowledge Folder at a
directory that holds no exports, then reindex it from the client's
Knowledge Folder card. The app's reindex drops sidecars for documents that
are no longer under the folder (`document_service.remove_stale`), which
clears the duplicates.

---

## Development

```sh
pip install -e ".[dev]"
pytest -q
```

The tests run against `tests/stub_backend.py`, an `httpx.MockTransport`
whose response shapes are copied from the backend source (search's
session/document union, the SSE framing of `/qa/stream`, the RFC 7807 401
body). Coverage includes the union, a backend-down case, a bad-token case,
a missing-token case, a timeout, a mid-stream Q&A failure, and an assertion
that the client only ever issues an allowlisted set of GET/POST requests —
nothing that deletes, mutates, or reindexes.

`scripts/e2e_stub_check.py` re-serves those same fixtures over a real
localhost HTTP socket and drives every tool through a real stdio MCP
client, so the whole stack (JSON-RPC framing → HTTP → bearer auth → SSE →
rendering) is exercised with only the data stubbed.
