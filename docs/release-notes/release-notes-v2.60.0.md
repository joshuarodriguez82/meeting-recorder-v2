# v2.60.0 — the app cleans what it already has, and tells you which extension captured it

## Install (macOS)

> v2.60.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.60.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.60.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.60.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Extension

Still **1.17.0** — unchanged from v2.59.0. Both fixes in this release
are in the app, and both apply to meetings **already in your store**,
with no re-capture required.

## The app now cleans invite text it already has

v2.59.0 fixed the invite body at capture time, structurally: the text
is read out of the invite's own frame, where Outlook's UI is
definitionally not present. That fix is correct and it stays.

It also does nothing for a meeting captured before it. Every body
already stored kept its RSVP control, its attendee tally, its Copilot
prompt suggestions and its toolbar-icon characters until something
re-captured that meeting — and a screenshot of the panel right after
installing v2.59.0 looked exactly like a screenshot from before it.

That is the lesson v2.52.0 already paid for with the attendee scrub: a
cleanup of data the **app owns** must not be held hostage by whether a
browser is running, or by which extension version got reloaded. Invite
bodies are now cleaned on the way out of the store, on every read:

- Toolbar-icon characters removed. Those `□` boxes are a private-use
  font — each icon is a Unicode Private Use Area character that
  `innerText` returns and every font draws as a hollow box.
- Outlook's own card lines dropped: `Join`, `Chat`, `Accepted`,
  `Change`, `No location added`, `Prepare for this meeting`, the
  `Accepted 1, Didn't respond 5` tally, `<organizer> invited you.`, the
  avatar monogram, the card's date line, and Copilot's three suggested
  prompts.

**Drop-only by construction.** It removes characters and whole lines
and never adds or rewrites, and every match is against the *whole* line
— so an invite that discusses "the change process" or "acceptance
criteria" keeps its text. The worst case on an unfamiliar tenant is
text that is still there.

## The panel tells you which extension captured these meetings

A release whose entire point was a capture-side fix was installed, the
panel looked identical, and there was no way — for you *or* for
diagnosis — to tell whether the extension had been reloaded or the fix
had simply not worked. Two completely different problems, rendering
identically. That is this project's oldest defect wearing yet another
hat.

The backend has always known both numbers. They were only shown in
Settings, nowhere near the meetings they explain. Upcoming Meetings now
carries a banner when the extension that last captured is older than
the one the app ships, naming both versions and what to do about it.

## Tests

1319 backend tests, up from 1314. The body cleanup is pinned by the
exact text from the field screenshot — it must keep the one real
sentence and drop everything around it — plus a genuine invite that
must pass through byte-identical, a chrome-only body that must become
empty so the UI can say "no description" honestly, and a line that
merely *contains* a keyword and must survive.

## "0 synced" now says which zero it is

Pressing **Sync to portal** after reprocessing reported `0 synced`,
which covers three unrelated situations in one sentence:

- the register for this project is **empty** — nothing was sent;
- the portal **already held exactly this** and correctly did nothing
  (ingest is idempotent by contract, so re-pushing unchanged content
  *should* report 0/0);
- the bound scope is **not the project** the sessions are tagged to, so
  a different register was pushed.

Only the first two are healthy, and none of them are actionable without
knowing which one happened.

Sync already rebuilt the register from current session data before
pushing — that part was working, so reprocessed content *was* being
sent. What was missing was any way to see it. The response now carries
the register's own shape next to the portal's answer, and the toast
reads one of:

- *Nothing to sync — "<project>" has no register content yet (0
  processed sessions in this project). If the meeting you expected is
  under a different project, re-tag it and sync again.*
- *Sent 47 items from 3 sessions — portal: 12 added, 5 updated, 275 on
  record.*
- *…0 added, 0 updated (already had this content).*

An empty register is a statement now, not a silence.
