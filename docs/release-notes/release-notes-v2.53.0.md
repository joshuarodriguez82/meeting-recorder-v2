# v2.53.0 — the invite body was being thrown away at the backend's door

## Install (macOS)

> v2.53.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.53.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.53.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.53.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

**The Chrome extension is unchanged at 1.13.0 — no extension update.**

## "(No description on this invite.)" was guaranteed, six releases deep

The extension has captured invite bodies since 1.7. The store has
serialized them since v2.43.0. The screen has rendered them since the
same release. And every body ever captured was discarded anyway —
because `events_from_structured`, the validator every extension POST
passes through on its way to the store, was never taught the field. It
copies subject, start, end, location, organizer, attendees, duration
and join link… and dropped everything else, including the body.

Six releases of fixing the extension's capture could not have put a
description on screen. The pipe's front door deleted the field before
storage every time.

It is carried through now, and the new test drives the real import
path end to end — the exact POST shape the extension sends in, the
stored event with its body out — and fails against every previous
build.

This is the drop-list defect for the third time, in its third costume:
`replace_all` rebuilt the store from a whitelist and destroyed the
diagnostics; the render check whitelisted signals and missed the pane;
now a whitelist-shaped constructor deleted a field added after it was
written. Each looked like the feature "not working" when the feature
was working and a list in the middle was silently eating its output.

## What this means on screen

After the next extension capture, descriptions appear — the capture
finally has somewhere to put them. Attendees were already passing
through this door; the real invitee lists arrive from the same capture
via Outlook's own event-detail responses, and a fresh capture that
carries them replaces the one leftover mis-attributed address the
junk scrub could not distinguish from a real entry.

## The readiness checklist collapses to its verdict

The Audio Devices pre-flight (the request from this morning, done
properly this time): when everything passes, the card shows the single
green **Ready to record** line with a chevron — four rows of
"Connected" are confirmation, not information, and they pushed
Upcoming Meetings below the fold on every visit.

**A failing checklist does not collapse.** When something is wrong, the
rows auto-expand and stay open, because the line that says *which* item
failed and what to do about it must be on screen while it is true. A
folded-away failure is the exact "unreadable result renders as fine"
defect this app keeps paying for.

## Tests

1288 backend tests, up from 1287. The new one pins the invite body
surviving the import door end to end, verified to fail against every
shipped build back to the field's first "(No description)" report.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 6 / 0 new, personal-data 0.
