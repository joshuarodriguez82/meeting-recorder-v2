# v2.20.2 — your clients and templates travel with your sessions

> v2.20.x made your **meetings** roam between machines through the
> shared Session Archive. Your **client list** didn't come with them.
> This fixes that.

## Install (macOS)

> v2.20.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.20.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.20.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.20.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Clients and templates now roam

Set up the shared archive on two machines and your meetings appeared on
both — but your **client list** didn't, and neither did your custom
summary templates.

Clients that have meetings tagged to them get rebuilt from those
meetings. Clients with **no** meetings yet exist only in a settings
file that lived inside each machine's own recordings folder and never
travelled. So did each client's Designated Folder and Knowledge Folder
settings, and every summary template you'd written.

The shared archive now carries those files too, in both directions.
Whichever machine changed a file most recently wins, and the change
applies immediately — no restart.

The Session Archive card in Settings says which way things are about
to move: **in sync**, **will pull from archive**, or **will push to
archive**.

**If a file can't be read, it is never used.** A half-downloaded or
truncated copy in the shared folder will not overwrite good settings on
your machine — it's skipped, and the card tells you why. These are
single shared files rather than per-meeting ones, so a bad read here
would cost real configuration; refusing is always the right answer.

> **After updating:** open the app on both machines and give it about
> 20 seconds. The machine with the newer client list pushes; the other
> pulls. You can force it immediately with **Sync now**.

## What still doesn't travel

Audio. Recordings stay on the machine that made them, deliberately — a
cloud copy stalling mid-write is what wedged the backend and cost
recordings in July. Transcripts, summaries, action items, decisions and
requirements all live in the session file and do travel, so the other
machine can show, search, and answer questions about a meeting it never
recorded.
