# v2.18.0 — Cloud Mirror, tabbed Settings, and the end of Drive-folder freezes

> **What this release adds:**
>
> 1. **Cloud Mirror — the safe way to publish sessions to Google Drive
>    / a NAS.** Point your recordings folder at a **local** disk (fast,
>    reliable) and set a new **Cloud Mirror** folder for a network
>    location. After each session finishes recording or processing,
>    every artifact (audio, transcript, summary, action items,
>    decisions, requirements) is copied to
>    `<mirror>/<client>/` (or `Unfiled/` for untagged sessions) by a
>    background worker with retries. The record → finalize → process
>    path never touches the network folder — a slow or briefly-offline
>    drive delays the copy, never the recording.
> 2. **Settings is now tabbed.** The one-endless-scroll page is grouped
>    into **Setup · Templates & Integrations · Recording & Co-Pilot ·
>    Data & Diagnostics**. Every existing card is unchanged, just
>    grouped; the Save bar saves from any tab.
> 3. **The end of "failed to fetch" storms on Google Drive.** Writing
>    recordings directly into `G:\My Drive\...` was behind the
>    mid-recording freezes, ghost sessions, evicted audio, and
>    disappearing Sessions list. The Recordings Folder help now says
>    **local disk only**, with Cloud Mirror as the supported route to
>    cloud.

## Install (macOS)

> v2.18.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.18.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.18.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.18.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Cross-device workflow this enables

Laptop records → **local disk** (safe, fast, never stalls) → background
worker mirrors each finished session to `G:\My Drive\MRv2\<client>\` →
dev PC / Mac reads the same Drive folder. Audio, transcript, summary,
and all extractions land per-client. If Drive is briefly offline the
worker retries; the recording is never affected.

## How Cloud Mirror works

- **Setup:** Settings → Recordings Folder card:
  - **Recordings directory** — must be a local disk path (e.g.
    `C:\Users\you\MeetingRecordings`).
  - **Cloud Mirror** — the network / cloud root. Empty = off.
- **Folder resolution per session:**
  - If the client has an explicit *Designated Folder* set (Clients
    view), that wins.
  - Otherwise `<mirror>/<sanitized-client-name>/`.
  - No client tag → `<mirror>/Unfiled/`.
- **The invariant (why it can't freeze the app):** the record, finalize,
  and process paths never write to a network folder. Every network
  copy is enqueued to a background worker with **5s / 30s / 120s**
  retry backoffs. Retries reschedule via a timer — a dead mount can
  never head-of-line-block another session's mirror. The manual
  Export button follows the same rule (text sync, audio queued).

## Cloud Mirror — the review pass that caught real bugs before ship

An 8-angle code review of this feature caught seven real defects
before release, all fixed in the shipped code:

- **Data-loss race** — the worker would save a stale session snapshot
  back over the JSON, clobbering a transcript/summary the main thread
  had written mid-copy. Fixed: the worker no longer writes the session
  file; retention finds mirror WAVs by enumerating the mirror
  subfolders.
- **Retry was dead for the WAV** — `export_all` was swallowing the
  audio-copy OSError, so the worker's retry never fired for the one
  artifact it existed to protect. Fixed.
- **Cross-thread folder race** — the shared `ExportService`'s target
  dir got swapped for the duration of a slow network copy; a
  concurrent request-path export could land in the wrong client's
  folder. Fixed: the worker gets its own instance.
- **Lost exports** — a re-enqueue during a running job was silently
  coalesced away, so newer artifacts (e.g. a summary) never mirrored.
  Fixed: coalescing clears at dequeue, not at finish.
- **Head-of-line blocking** — retry backoffs slept on the sole worker
  thread. One dead mount stalled every other session's export for
  minutes. Fixed: retries reschedule via a `Timer`, worker keeps
  draining.
- **Re-introduced freeze on manual Export** — the manual Export
  endpoint copied audio synchronously with a mirror configured.
  Fixed: audio goes through the worker.
- **Windows reserved names** — a client named `CON`/`NUL`/`COM1`
  would silently never mirror. Fixed: sanitizer suffixes reserved
  device names.

Full backend suite: 105 passing.

## Tabbed Settings

The ~20 cards are now grouped behind a sticky tab bar:

- **Setup** — API keys, recordings folder + Cloud Mirror, app updates,
  AI models
- **Templates & Integrations** — summary templates, email, calendar,
  auto-record skip patterns, Chrome extension
- **Recording & Co-Pilot** — workflow toggles, live co-pilot config,
  auto-stop, GPU
- **Data & Diagnostics** — diagnostics, terminology, known speakers,
  semantic index, retention

Pure layout change — every existing card is untouched; the Save bar
sits outside the tabs and saves everything regardless of which tab
is active.

## Known / recommended

- **Move your recordings folder to a local path** if it's currently on
  Google Drive / OneDrive / iCloud. Settings → Recordings Folder →
  local path → Save → restart. Then set Cloud Mirror to the Drive
  folder for cross-device access.
- **Editable AI prompts (all features)** and **PR-B: server.py router
  split** are planned follow-ups.
