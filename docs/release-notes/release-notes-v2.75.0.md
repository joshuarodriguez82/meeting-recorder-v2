# v2.75.0 — commitments you can read, and proof the setup worked

## Install (macOS)

> v2.75.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.75.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.75.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.75.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## Asking an assistant what you owe now returns readable answers

If you asked your AI assistant for your open commitments, every item
came back with an owner, a due date and a link to the meeting — and no
text saying what the commitment actually was. Eighty-nine rows of
nothing.

The list was reading the wrong field. It's fixed: you get the
commitment, who owns it, when it's due, the client and project, and the
meeting to cite. Items the recorder captured without a description now
say so explicitly instead of appearing as a blank line.

## The app tells you when an assistant last used it

**Settings → Templates & Integrations → AI assistant access.**

Setting up an AI tool used to end with "quit it and reopen" and no way
to tell whether that worked. If it didn't, you got silence — the same
silence as a tool that was never set up at all, so the only option was
restarting and hoping.

The card now reports when an AI assistant last called the app. Set your
tool up, restart it, ask it a question, and that line updates. If it
still says nothing has used it, the tool isn't launching the connection
and the restart is the first thing to check.

### Restarting properly, which is where this goes wrong

Closing the window is not quitting. Claude Desktop runs a dozen
processes and keeps one in the system tray, so closing the window leaves
it running with the settings it loaded when it started — including the
absence of any settings you just added.

**Right-click the tray icon and choose Quit**, or end it in Task
Manager. Then reopen it. The card and the setup screen both say this now,
and say why.

## Documentation

`docs/ai-integrations.md` carries the same guidance: what quitting
actually means per tool, and how to confirm a connection instead of
guessing at it.
