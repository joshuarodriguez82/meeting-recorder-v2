# v2.82.1 — A session copy the sync client has open is not a missing session

## Install (macOS)

> v2.82.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## Merging speakers looked like it failed when it had worked

Merging two speakers on a machine whose archive folder lives on a
Google Drive `G:\` mount produced an error, and then a second, stranger
one:

> Confirm failed: Not Found: Speaker not on this session

The merge had actually **succeeded**. Three things went wrong in a row,
and only the last one was visible.

**The merge worked.** 153 segments were rewritten and the session saved.

**The refresh that followed it failed.** Merging queues an export, and
the export's archive step rewrites the session's copy in the archive
folder. That made the archive copy the newest one — and the app always
opens the newest copy. Ninety milliseconds later the app tried to
re-read the session, landed on the copy the sync client still had open,
and got a permission error. The whole request failed.

An identical, perfectly readable copy was on the local disk the entire
time.

**So the speaker list never updated.** It still showed the speaker the
merge had removed, complete with its buttons. Clicking one asked the
app to confirm a speaker that no longer existed — which is where "Not
found" came from, three steps downstream of anything that was actually
wrong.

### What changed

**A copy that will not open is no longer treated as a session that does
not exist.** The app now waits briefly for a locked file (a sync client
usually releases it in well under a second) and, failing that, falls
back to another copy instead of giving up. A half-written or corrupt
copy on the synced side no longer costs you a session that is intact
locally. Only when *no* copy can be read does it report an error — and
it says which ones it tried.

A file being held open by a sync client also used to be reported as a
file that had not finished downloading, which sent you looking in the
wrong place. Those are now told apart.

**A merge and the refresh after it are reported separately.** A merge
that worked says so, even if the view behind it could not reload. The
merged-away speakers disappear from the list immediately rather than
waiting for a refresh that might not arrive — so there is no stale row
left to click.

**And if you do reach a speaker that has gone**, the message now says
they were most likely merged and to reopen the meeting, instead of
reporting a bare "not on this session".

### Tests

Ten tests covering the exact sequence — a locked newest copy with a
readable fallback, both error codes Windows uses for a file-in-use, a
corrupt newest copy, no readable copy at all, and a genuinely absent
session still reporting as absent. Seven of them fail against v2.82.0.
