# v2.38.0 — follow-up drafts tell you where they went and who still needs an address

## Install (macOS)

> v2.38.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.38.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.38.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.38.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## "10 drafts created" was true and useless

A real run reported **10 of 10 drafts created**, with no error anywhere.
The drafts weren't in the Drafts folder the user was looking at, and
nine of the ten had no recipient. Everything the app said was
technically accurate and none of it was actionable.

Three separate problems sat behind one confident message.

## It now tells you where the drafts went

Drafts are saved, never displayed — ten windows opening at once would be
worse. But the app never checked *where* it saved them, so it couldn't
tell you.

After each draft is saved it now reads back the folder it actually
landed in and the account that owns it, and says so:

> **10 drafts saved to Drafts (you@example.com) — 9 need an email
> address before you can send them.**

If the read-back isn't available it says the folder couldn't be
confirmed, rather than naming one nobody verified.

**Why drafts can appear to vanish:** they're created through classic
desktop Outlook, which is a different application from the Outlook web
app. If your default profile there points at another account or a local
store, the drafts are real but somewhere you weren't looking. The app
can't choose a mailbox for you — nothing in a meeting identifies one,
and in the reported case guessing by domain would have picked wrong —
so instead of guessing it now tells you exactly where each one went.

## It stops counting drafts it can't confirm

A saved item gets an identifier from Outlook. One that never persisted
doesn't. Drafts without one are no longer counted as created; they're
reported separately, so "10 created" can't include one that silently
failed.

## It tries harder to find an email address

The lookup was being handed whatever name appeared in the action item —
usually a first name, which rarely resolves against a corporate address
book. One unusually distinctive first name worked; eight didn't.

It now tries the richest form of that person's name the app knows,
falling back through shorter ones. A full name resolves far more often
than a first name.

Fuller forms are only used when identity is actually established —
either a merge you confirmed yourself in speaker grouping, or an
attendee whose name properly contains the label's words. "Alex" will
never silently become "Alexandra".

**And ambiguity is refused rather than guessed.** If two attendees could
both extend one label, neither is used. Addressing one person's
commitments to their colleague is worse than leaving the field blank.

Unaddressed drafts are still created — the body is useful and you can
fill in the recipient. The count just stops implying they're ready to
send.

## Diagnostics now report the app version

Exported bundles recorded `app_version: null` while every other field
was populated — so a bug report couldn't say which build produced it.

Two independent causes, both fixed. The desktop shell never passed the
version to the backend at all. And the fallback looked for project files
that don't exist in a packaged install, because the backend runs from an
extracted runtime folder. The version is now passed directly and also
stamped into the bundle at build time, so either source alone is enough.

## macOS

The same gaps existed there and are closed the same way — the draft's
identifier is read back for verification, the location is named
specifically, and the account is reported where it can be determined.

## Tests

1086 backend tests, up from 1054. The 32 new ones cover the retry
against a fuller name, the fall-back through shorter forms, the
confirmed-identity guard, the two-attendee ambiguity refusal, addressed
versus unaddressed counting, the message naming both location and
unaddressed count, exclusion of drafts with no identifier, and the app
version resolving from each of its sources.
