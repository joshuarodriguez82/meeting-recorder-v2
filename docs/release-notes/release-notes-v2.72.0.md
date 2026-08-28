# v2.72.0 — connect your AI assistant to your meeting archive

## Install (macOS)

> v2.72.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.72.0_universal.zip`.
>
> Still unsigned for Gatekeeper purposes. First launch needs the
> Gatekeeper bypass — pick whichever path you prefer:
>
> **Path A — System Settings (no Terminal):** double-click the `.zip`
> in Finder (Archive Utility auto-extracts to `Meeting Recorder.app`),
> drag the `.app` to `/Applications`, double-click, dismiss the
> "damaged" warning, then **System Settings → Privacy & Security →
> Open Anyway**, double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_2.72.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.72.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## Your AI assistant can now read your archive

**Settings → Templates & Integrations → AI assistant access.** Click
**Turn on**, pick your AI tool, click **Copy**, paste into that tool,
restart it. That's the whole setup.

Once connected, you can ask your assistant things it previously had no
way to answer:

- *"What did we agree about the cutover window on the Globex account?"*
  — searched across every transcript **and** every document in your
  clients' Knowledge Folders, through the app's own semantic index.
- *"What do I still owe anyone?"* — outstanding commitments, overdue
  first, with owner, due date, client and the meeting to cite.
- *"Summarise the last three meetings with this client."*
- *"Which opportunity is this client bound to in the portal?"*

Seven tools in all. Everything is **read-only** — an assistant cannot
delete a session, change a setting or start a reindex — **local**, and
only available while the app is running. Nothing is sent to a cloud
service.

### It is not a Claude feature

This runs on MCP, an open protocol. The card has one-click configs for
**Claude Code**, **Claude Desktop**, **Cursor** and **VS Code**
(Cline / Continue), and the same config shape works in Windsurf, Zed and
anything else that speaks MCP. For tools that don't, the same card gives
you the OpenAPI spec URL and access token for the plain HTTP API.

### What changed since v2.71.0

The card existed in v2.71.0 but could not actually be followed: it asked
you to fill in a path inside a `mcp-server` folder that only exists if
you cloned the source repo. If you installed the app — which is
everyone — that folder was not on your machine.

Now the app ships that folder inside itself, installs the one missing
library on demand when you click **Turn on**, and prints the config with
**your machine's real paths already filled in**. There is nothing left
to substitute, and no token to copy into the config: your assistant
finds the app's address and credentials by itself.

If turning it on fails — no network, a corporate proxy in front of the
package index — the card shows you exactly what the installer said, and
your app is untouched. The install is deliberately kept out of first
launch, where a failure would stop the app from starting at all.

## Assistants can find the app when it isn't on its usual port

The app prefers port 17645 and quietly moves to another one if something
else already holds it. Nothing recorded which port it got, so an
assistant (or any other local tool) checked 17645, found nothing, and
reported that the app wasn't running — while it was open on screen.

The app now writes its chosen port next to its access token every time
it starts, and the MCP server reads it automatically. If you ever do hit
a connection error, the message now tells you the right thing to try
first: quit and reopen the app.

## Documentation

`docs/ai-integrations.md` is the full guide for both doors — MCP and
plain HTTP — including where every client keeps its config, how to check
a connection from a terminal, and what each of the seven tools answers.
