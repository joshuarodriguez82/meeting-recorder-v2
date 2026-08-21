# v2.52.0 — the cleanup runs when the app opens, not when the browser feels like it

## Install (macOS)

> v2.52.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.52.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.52.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.52.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

**The Chrome extension is unchanged at 1.13.0 — no extension update
this time.** Every fix in this release is app-side, which is the shape
releases here are supposed to have.

## v2.51.0's cleanup shipped, and the junk was still on screen

The scrub that removes the "Attendees (24)" wall of Outlook buttons was
gated on the **next extension capture** — the 30-minute alarm, or a
manual Capture & Send. Install the fix, open the app, and the app reads
the same old store: identical junk, looking exactly like nothing was
fixed. With Chrome closed, it would have looked that way forever.

That was a design mistake, not a bug: a cleanup of data the app owns
must not be held hostage by whether a browser happens to be running.

The scrub now also runs **when the store is read** — the moment the
Record tab loads. No capture, no POST, no browser required. Same flag
as the capture-side scrub, so whichever side reaches the store first
does the work and the other finds it done. The new test reproduces the
exact complaint: a junk store, zero captures, one read — clean.

## Attendee lists collapse past six

Even a legitimate large invite rendered a wall of chips that pushed
every other meeting off screen. Six chips stay visible — the common
small-meeting case is unaffected — and anything more sits behind an
explicit **"+N more"** that names the hidden count, so nothing is
silently truncated. Expansion is per-meeting and doesn't disturb the
rest of the list.

## The Import briefing button is gone

The header's Import/Re-import briefing button opened a manual
paste-a-blob flow from before the extension existed. The extension's
automatic capture is the data path; the button was dead weight in the
most visible corner of the Today tab. Removed at the user's request.

The import dialog and its backend endpoint remain — a brand-new install
with no briefing at all still gets an entry point on the empty state,
and the flow can return as a Settings escape hatch if an extension
outage ever demands one.

## Tests

1287 backend tests, up from 1286. The new one pins the read-time scrub
— junk store, zero captures, one read, clean, persisted — and was
verified to **fail against v2.51.0**, which is precisely why the field
saw what it saw.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 6 / 0 new, personal-data 0.
