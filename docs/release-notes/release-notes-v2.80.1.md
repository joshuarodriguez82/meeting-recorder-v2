# v2.80.1 — the activity panel fits, and Clear works

## Install (macOS)

> v2.80.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.80.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.80.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.80.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## The activity panel

Three faults, one of which caused another.

**It overflowed.** With more than a few entries the list ran past the
bottom of the panel, out of the window, and over the page behind it.
The panel was told to cap the list's height, but the way that cap was
written never took effect, so the list rendered at whatever height it
wanted.

**Clear didn't work.** Not because the button was broken — because the
overflowing list was painted on top of it. Clicks were landing on the
list, not the button. Fixing the overflow fixes the button.

**The same message appeared twice.** "Processing complete." showed up
as two entries, one blue and one green, a minute apart. It was logged
once while the run was still finishing and once when it reported done,
and the two were treated as separate events because their colour
differed. One thing happened; it now takes one line, and ends on the
colour that matters — a step that looked fine and then failed ends red.

Also: Clear now resets the "N new" counter with the list, rows have a
hairline between them so a long list can be scanned, and the timestamp
column stays straight as "now" becomes "12m".
