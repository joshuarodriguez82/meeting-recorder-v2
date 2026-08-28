# v2.73.0 — the calendar tells you the truth

## Install (macOS)

> v2.73.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.73.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.73.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.73.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## "Calendar: Not connected" on a machine with a working calendar

Reported three times, always the same shape: the calendar works on a
Mac and not on Windows, same account, same version.

The app reads your calendar from **two** places — the calendar on your
computer, and whatever the Chrome extension picks up from Outlook Web —
and shows you meetings from both. But the status line on the Record tab
only ever checked the first one. On a Mac that check passes. On Windows
it often doesn't, so the same account was told "Not connected" while
meetings from the extension were sitting right there on the screen.

Windows is also where that check fails most often now, for a reason
nobody could be expected to guess: **the new Outlook has no way for
other apps to read your calendar at all.** If Microsoft has moved you to
it, there is nothing for the app to read — and the old advice, "connect
a calendar in Settings", was useless, because you already had.

Now the status line reports on every source the app actually uses, names
the one answering — you'll see **"Connected (Chrome extension)"** when
Outlook has quietly stopped working — and when nothing answers it tells
you why, in terms that fit your machine. A Mac user gets pointed at the
calendar permission in System Settings; a Windows user gets told about
classic Outlook and the extension.

**Still being worked on:** meetings that arrive without their join link
or attendee list. That's a separate problem in how much detail the
extension can collect in one pass, and it needs a diagnostics export
from a machine that shows it. If you see a meeting missing its Join
button, export diagnostics and send it over.

## Connecting an AI assistant on Windows

The setup command on the **AI assistant access** card was written for a
Mac terminal and split across two lines. Pasted into a Windows command
prompt it registered a broken entry and then refused to replace it. It's
one line now, and it works the same way in every terminal.

If you hit this on 2.72.0, run `claude mcp remove meeting-recorder
--scope user` once, then copy the command again from the card.

## The app now reports its own version correctly

Anything asking the app which version it is — an AI assistant, a
connection check, any tool reading the API — was told **2.0.0**,
regardless of what was actually installed. That answer had been
hardcoded since the endpoint was written and went unnoticed until an AI
assistant started printing it. It now reports the real build.

## Under the hood

The frontend had no automated tests. That's why a broken setup command
could ship — the app's Python side has 1,433 tests, and the text you
copy off the screen had none. There's now a test suite covering it, run
on every change.
