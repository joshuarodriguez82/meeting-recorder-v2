# v2.2.2 — Semantic search auto-indexes everywhere

A small patch release: the semantic-search index now stays current
without you ever clicking a button. Plus the universal2 Mac DMG +
dependency self-heal that were supposed to ship in v2.2.1 (the v2.2.1
tag pointed at the wrong commit and never got a clean build).

> ## ⚠️ macOS install — READ THIS FIRST
>
> ### Step 1: download the .dmg
>
> v2.2.2 ships **a single universal `.dmg`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab:
>
> - `Meeting.Recorder_2.2.2_universal.dmg`
>
> ### Step 2: bypass Gatekeeper
>
> The build is **unsigned** (no Apple Developer cert yet), so macOS will
> say *"damaged and can't be opened"* when you double-click the DMG.
> It is **not** damaged — it's the quarantine attribute your browser
> added on download. Pick whichever path is easier; both work, both are
> one-time per install.
>
> **Path A — System Settings (no Terminal):**
>
> 1. Double-click the DMG, drag the app to **Applications**.
> 2. Double-click `Meeting Recorder` in Applications. macOS refuses
>    with the "damaged" warning. Click Done / Cancel.
> 3. Open **System Settings → Privacy & Security**. Scroll to the
>    Security section. Click **Open Anyway** next to the Meeting
>    Recorder blocked-app message.
> 4. Re-double-click the app. macOS asks once more — click Open. Done.
>
> **Path B — Terminal:**
>
> ```sh
> xattr -cr ~/Downloads/Meeting*.dmg
> open ~/Downloads/Meeting*.dmg
> # In Finder: drag the app icon to Applications.
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> macOS treats the app as trusted on every subsequent launch — you only
> do this once. **Right-click → Open does not work** on macOS Sequoia /
> Sonoma.
>
> **Windows users** — none of this Gatekeeper stuff applies. Download
> the `.msi` or `.exe` from the Releases page and double-click.

## What changed since v2.2.0

### Semantic index now self-maintains

Previously you had to click **Settings → Semantic Index → Index N
sessions** for any session that wasn't auto-indexed. v2.2.2 makes the
index a no-touch feature:

- **`/process` and `/process_full` both auto-index** when they finish.
  Previously only the standalone `/process` path indexed; the
  auto-process-after-stop flow (which goes through `/process_full`)
  silently skipped indexing, leaving sessions invisible to semantic
  search until you went looking for them.

- **Backend startup runs a background backfill pass.** On every boot,
  the backend scans for processed sessions whose embedding sidecar is
  missing, then walks them in sequence and indexes each. Cheap (~0.1-1s
  per session on CPU MiniLM); silent (only logs start/end of the run);
  no UI needed.

- **The Settings → Semantic Index card now reflects the new reality.**
  Description says auto-index covers everything; the manual
  "Index N sessions" button is still there but now reads "Index N
  sessions now" and is styled as outline/secondary — it's only useful
  if you want to skip the background pass and force results immediately.

You should never need to think about the index again. It just has
everything.

### Universal2 macOS DMG (was supposed to ship in v2.2.1)

v2.2.0 only published an Apple Silicon DMG even though the workflow
declared a `macos-13` (Intel) matrix entry alongside `macos-14`. Cause:
GitHub deprecated free-tier `macos-13` runners; the Intel job either
failed to allocate or errored at queue time, while the other entries
published anyway. v2.2.1 was meant to fix this with a universal2 build,
but the v2.2.1 tag landed on the wrong commit and never built cleanly.

v2.2.2 builds the universal2 (fat) `.dmg` properly: one file, runs on
both Apple Silicon and Intel. Install instructions consolidate to one
filename instead of "pick the one matching your CPU."

### Backend self-heals when a venv package is missing

Some users hit a "sentence-transformers isn't installed" warning in
Settings → Semantic Index after upgrading from a pre-v2.2.0 venv —
the bootstrap detected the existing venv as populated and skipped
re-installing the new requirements that v2.2.0 introduced. v2.2.2
adds a runtime self-heal: on backend boot, three feature-critical
packages get import-tested. If any are missing, the backend re-runs
`pip install -r requirements-{cpu,mac}.txt` against the running
interpreter and continues. ~10ms when everything is already
installed; one-time ~30s pause when an actual repair is needed.

### Install docs

README + MAC_SETUP.md + this release-notes file all use the same
two-path Gatekeeper workaround now (System Settings or `xattr -cr`),
since the pre-v2.1.0 right-click → Open trick stopped working on
macOS Sequoia / Sonoma.

## No feature changes

Everything in v2.2.0 — dual-stream live transcription, in-call search,
Groq + Gemini provider presets, the three crash fixes — is unchanged.
v2.2.2 is purely about delivery (universal2 DMG), reliability
(dependency self-heal), and removing the manual semantic-index step
from your workflow.
