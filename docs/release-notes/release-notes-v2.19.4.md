# v2.19.4 — client folders actually get all your files, and created clients stop disappearing

> **What this release fixes:**
>
> 1. **Meetings you tagged to a client never reached that client's
>    Designated Folder.** Only three operations ever triggered a copy,
>    and none of them was tagging. If you filed a back-catalogue — or
>    set a folder after you'd already recorded — those meetings stayed
>    on local disk permanently, with nothing telling you.
> 2. **Clients you created vanished from the Clients list.** Any
>    transient failure loading your saved clients silently erased every
>    client that had no meetings yet, and showed "No clients yet"
>    instead.
> 3. **Meetings tagged with different capitalization went missing.** A
>    meeting tagged `AON` exported into the `Aon` folder but never
>    appeared under `Aon` in the app.

## Install (macOS)

> v2.19.4 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.19.4_universal.zip`.
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
> unzip -o Meeting.Recorder_2.19.4_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.19.4_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Your client folders now get everything

The copying itself was never broken. The problem was that almost
nothing told it to run.

A copy was queued from exactly three places: full processing, the
individual extractions, and re-summarizing. Every operation you'd
actually use to organize meetings queued nothing at all:

| What you did | What used to happen |
|---|---|
| Tagged an already-processed meeting to a client | nothing copied |
| Used the **Tag Meetings** button | nothing copied |
| Set a client's Designated Folder | only future meetings copied |
| Renamed a client | nothing re-copied |

So you could tag twenty meetings to a client and watch the folder stay
exactly as empty as before, with no error and no indication anything
was outstanding.

**The fix isn't "queue a copy in those four places too."** That's
correct right up until a fifth place is added, and it fails silently
when one is missed — which is the whole problem.

Instead, the app now **reconciles**: it compares what each meeting owes
its client folder against what's actually sitting in that folder, and
copies whatever's missing. That runs when you set a folder, when you
rename a client, about 20 seconds after the app starts, and whenever
you press the new **Sync now** button. Tagging still copies
immediately — that's the fast path — but if anything ever fails to fire,
reconciliation repairs it on the next pass. A missed copy is now a
short delay instead of a permanent hole.

**After updating:** open **Clients**, pick a client, and press **Sync
now** to pull everything missing into its folder right away. Or just
leave the app open — the startup sweep does every client on its own.

## You can finally see whether it worked

Each client's Designated Folder card now shows **"N of M meetings
copied"**, and tells you when the folder isn't reachable at all.

This mattered more than it sounds. When a copy failed, the app retried
three times and then gave up with nothing but a line in a log file. A
file that never arrived was undiscoverable unless you happened to open
the folder and count. Now a gap is visible where you'd look for it, and
**Sync now** fixes it.

## Created clients stop disappearing

Clients that don't have any meetings tagged yet exist only in your
saved client settings. If reading that file failed for any reason —
including the backend restarts fixed in v2.19.3 — the app quietly
dropped every one of them and fell through to the "No clients yet"
screen, exactly as if you'd never created them.

Nothing was ever deleted; it just couldn't be read, and the app treated
those two situations identically. It now says so plainly, keeps
retrying every 5 seconds, and gives you a Retry button. If some clients
load and others don't, it warns that the list may be incomplete rather
than showing a short list as though it were the whole thing.

This is the same mistake as the Today tab losing its briefing in
v2.19.3 — *a failed read rendered exactly like "there's nothing here."*
That fix only covered the Today tab. This one covers the Clients list.

## Capitalization no longer hides meetings

Copying resolved a client's folder case-insensitively, but the Clients
list matched exactly. A meeting tagged `AON` therefore copied into the
`Aon` folder and then never showed up under `Aon` — the files were on
disk the whole time, just invisible in the app.

Client matching is now case- and whitespace-insensitive everywhere, and
`AON` / `Aon` / `aon ` collapse into one client instead of splitting
your meetings across near-identical entries.

## Unchanged

Recording and processing still never write to a network folder. That
rule exists because a Google Drive copy once stalled the backend
mid-recording, tripped the watchdog, and cost recordings — copies
happen on a background thread, and audio stays on local disk. None of
that changed here.

## Under the hood

- New `export_reconcile` module: pure, no copying, no session mutation
  — it only predicts filenames and stats them, so it can never stall
  the event loop on a cloud mount.
- New `GET /clients/{client}/export-status` and
  `POST /clients/{client}/reconcile`.
- 17 new tests (142 passing), including a guard that locks the
  filename prediction to the exporter so the two can't drift, and one
  asserting an unreachable folder counts as "everything outstanding"
  rather than "everything present" — so an offline Drive can't report a
  client fully mirrored.
