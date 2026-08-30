# v2.77.0 — your documents index themselves now

## Install (macOS)

> v2.77.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.77.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.77.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.77.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## Knowledge Folders stay indexed on their own

v2.76 told you when a client's documents weren't indexed. This version
does something about it.

Until now, documents were indexed once — when you first set a client's
Knowledge Folder — and never again. Every SOW, deck and questionnaire
you added afterwards stayed invisible to search and to your AI assistant
unless someone remembered to click Reindex. Most people never did.

The app now sweeps your Knowledge Folders in the background and picks up
anything new. **It's on by default, and there is nothing to configure.**

After you upgrade, leave the app running. Clients that have never been
indexed go first, one at a time, and within an hour or two your
documents will be searchable — and citable by your AI assistant —
without you doing anything. You can watch the counts change on the
Clients tab.

**It costs nothing.** The model that reads your documents runs on your
own machine, so there is no per-document charge and nothing is sent
anywhere. Documents that haven't changed since the last pass are skipped
entirely, so day-to-day this is invisible.

### It stays out of your way

- **Never while you're recording or processing.** Reading documents
  competes with transcription for your CPU, and a recording always
  wins — even if indexing is overdue.
- **One client at a time**, so it never turns into a long job that
  monopolises your machine.
- **A folder that's offline doesn't block the rest.** If your Drive
  isn't synced, that client is skipped and the others carry on.

### Settings → Knowledge Folder indexing

Turn it off, or change how often it checks. The minimum is five
minutes, and that isn't arbitrary: checking a folder means asking for
each file's timestamp, and on a Google Drive path every one of those is
a network request rather than a local read. Checking every ten seconds
would be constant traffic against Drive and would not find your
documents any sooner — a file you add in the morning is found on the
next pass either way.

## First run after upgrading

If your clients have never been indexed — which is likely — the first
pass through each folder does real work: it reads every document. On a
Google Drive folder that means downloading files that were previously
online-only, so expect some disk and network activity for the first hour
or so. After that, passes are near-instant.
