# v2.74.0 — the app sets up your AI tools for you

## Install (macOS)

> v2.74.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.74.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.74.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.74.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## One button per AI tool

**Settings → Templates & Integrations → AI assistant access.**

Pick your tool, click **Set up**, then quit that tool completely and
reopen it. That's the whole thing. No JSON to paste, no file paths to
find, no config to edit.

Each tool's button now shows its own state — a green tick when it's set
up, an amber warning when it's set up but pointing at paths that have
moved (which produces a tool that lists the recorder and then fails
every question you ask it).

**Claude Desktop** and **Cursor** are written for you. **Claude Code**
keeps its one-line command, because it manages its own settings file and
has a proper command for this. **VS Code** still needs pasting, because
where its config lives depends on which extension you use, and writing
to the wrong place would look like success and do nothing.

### Read this before setting up your team

**Every tool is configured separately.** They don't share settings.
Setting up Claude Code does not set up Claude Desktop, and the previous
version's screen implied otherwise — you could follow it exactly and end
up with a tool that saw nothing. That's fixed, and the screen now says
so plainly.

**A Claude running in the cloud can't see your meetings.** claude.ai in a
browser, Claude on your phone, and cloud-hosted sessions all run on
someone else's computers. This connection is a program on *your* disk
talking to the app on *your* machine — that's what keeps your
transcripts off the internet, and it's also why a browser tab can't
reach them. Use a tool running on the same computer as the app.

### If something was already set up

Your existing config is kept. The app merges its entry into whatever's
already there — other servers, other settings, all untouched — and backs
the file up before writing. If it finds a config file it can't read, it
stops and tells you rather than overwriting something it didn't
understand.

## Documentation

`docs/ai-integrations.md` now opens with which tool reads which file,
and its troubleshooting is ordered by what actually goes wrong: did you
set up *that* tool, is the session running on this computer, did the
tool fully restart, is Meeting Recorder open.
