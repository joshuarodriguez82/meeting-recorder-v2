# v2.82.3 — Fixes the Speakers tab taking the whole window down

## Install (macOS)

> v2.82.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## Opening Speakers killed the window

Since v2.82.1, opening a meeting's **Speakers** tab replaced the entire
app with:

> This page couldn't load — Reload to try again, or go back.

Not the tab. The whole window.

The speaker-merge feature added a list of speakers already merged away,
so the rows would disappear immediately rather than waiting for a
refresh. The line that filters them out ended up **one line above** the
line that creates the list it reads. In JavaScript that is not a
warning, it is a hard error the moment it runs — and it ran on every
single render of that tab, which is why nothing was left standing.

One line moved. Anyone on v2.82.1 or v2.82.2 should update.

### Why the checks missed it

The frontend gate is a full production build, and it passed. The
typechecker does not flag this: the reference sits inside a small
function handed to a filter, and a function *could* be called later —
nothing tells the typechecker that a filter calls it immediately.

The linter does flag it, but the build has never run the linter,
deliberately: the project carries around forty pre-existing style
complaints, and a check that is red on every change is a check people
learn to ignore.

So the build now runs a **very short** list of lint rules — only the
ones whose violations are crashes rather than preferences — against a
recorded list of the places that already trip them. It is red only for
a crash that the change being tested introduced. Putting the old
mistake back turns it red; nothing else does.

That list turned up five other spots. Each was read individually; none
of them crashes, because in every case the value is read inside a
function that runs after it exists. They are recorded rather than
hidden, and they stay visible until they are cleaned up.
