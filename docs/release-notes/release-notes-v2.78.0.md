# v2.78.0 — automatic indexing actually works, and you can fix a client's name

## Install (macOS)

> v2.78.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.78.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.78.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.78.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## If you installed v2.77.0, please take this one

**Automatic indexing did not work in v2.77.0.** It was on, it looked
enabled in Settings, and it never indexed a single document — a mistake
in the code meant the background pass failed immediately every time and
said nothing. If you upgraded yesterday and left it running expecting
your documents to become searchable, they did not.

It works now. Install this, leave the app open, and your Knowledge
Folders will index themselves as originally described.

## Rename, merge or delete a client

New on the Clients tab: **Rename or remove this client**.

**Fixing a misspelling is the main use.** If the same customer exists
twice under slightly different spellings, half their history is filed
under a name you never look at. Rename one to the other and the app
**merges** them — every meeting and every indexed document moves across,
and the surviving client keeps its own folders. The button says "Merge"
when that is what will happen, so you always know which one you are
doing.

**Deleting a client never deletes a recording.** A client is a name and
its folder settings; your meetings stay in the archive either way. When
you delete one you can choose whether to also remove the name from its
meetings, or leave the tag in place so re-creating the client later
brings the history straight back. The confirmation tells you how many
meetings are affected before you agree.

You can rename and delete **projects** the same way. Projects are only
labels on meetings, so removing one clears the label and nothing else.

## Under the hood

The indexing bug shipped because the tests covered when the background
pass should run — all of which passed, because that part was right — and
nothing checked that it did any work when it ran. There are now tests
for the work itself, and they fail against yesterday's build.
