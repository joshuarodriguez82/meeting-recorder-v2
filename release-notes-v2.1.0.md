# v2.1.0 — macOS support

First-class macOS build. Same feature set as the Windows version, just
running on Mac. No fork, no second app — both platforms ship from the
same codebase.

## What's new

- **Native macOS app.** Tauri produces a `.dmg` installer (Apple Silicon
  and Intel). System integration uses the native APIs, not shims:
  - **Calendar** via EventKit — reads whatever's synced into Calendar.app
    (iCloud, Exchange / Outlook for Mac, Google).
  - **Follow-up email drafts** via AppleScript to Mail.app, with Outlook
    for Mac as a secondary path and `.eml` files in `~/Downloads` as a
    last-resort fallback.
  - **Auto-launch on login** via a LaunchAgent plist in
    `~/Library/LaunchAgents/`, replacing the Windows Startup-folder shortcut.
  - **GPU acceleration** via Apple's Metal Performance Shaders (MPS)
    backend — works out of the box on M1/M2/M3/M4 Macs with no install.
    The Settings → Transcription Acceleration card now shows MPS as
    active on Apple Silicon and hides the irrelevant CUDA / DirectML
    cards.

- **System-audio loopback via BlackHole** (free, `brew install blackhole-2ch`).
  macOS has no first-party loopback API for general apps, so the user
  installs BlackHole once and routes their system output through it via
  Audio MIDI Setup. Then BlackHole appears in the System Audio dropdown
  next to any other input device.

- **Per-platform settings paths.** Mac data lives under
  `~/Library/Application Support/MeetingRecorder/` — config, recordings,
  logs, and the bootstrap venv all in the canonical Apple-blessed spot.

## Bug fixes (apply to Windows too — same shared frontend)

- **AI Provider dropdown was stuck on "anthropic".** A stale-closure bug
  in the settings view made `applyPreset()` clobber its own writes — the
  model id would update but the provider name wouldn't. Fixed by
  switching to a functional state updater.
- **API Keys section now hides irrelevant fields.** Picking OpenRouter /
  Ollama / Custom hides the Anthropic API Key field; HuggingFace stays
  visible (always required for speaker diarization).
- **Launch-on-startup toggle wasn't doing anything.** The setting
  persisted but no code applied it to the OS. `/settings` now installs /
  removes the LaunchAgent (Mac) or Startup-folder shortcut (Windows) on
  actual transitions.
- **Transcription Acceleration card no longer shows misleading "Use This"
  on the NVIDIA / DirectML cards on Mac.** Replaced with an Apple Silicon
  (MPS) card showing it's already active, plus CPU as a force-fallback
  for debugging.

## Documentation

- **`MAC_SETUP.md`** — full Mac install walkthrough: Homebrew, Python
  3.13, Node, Rust, BlackHole, audio routing, first-launch permission
  prompts (mic, calendar, AppleScript automation), and notarization
  instructions for anyone who later picks up an Apple Developer account.

## Distribution caveat — Mac install requires four Terminal commands

These builds are **not signed or notarized** — that requires a paid
Apple Developer account ($99/yr) which the project doesn't have yet.
On macOS Sequoia / Sonoma the DMG opens with **"damaged and can't be
opened"**. This isn't actual damage — it's the quarantine attribute
your browser added when downloading the file. The older
*"right-click → Open"* workaround stopped working on recent macOS
versions; you have to strip the quarantine attribute first.

Open Terminal and run:

```sh
xattr -cr ~/Downloads/Meeting*Recorder*.dmg
open ~/Downloads/Meeting*Recorder*.dmg
# drag the app from the DMG to /Applications, then:
xattr -cr "/Applications/Meeting Recorder.app"
open "/Applications/Meeting Recorder.app"
```

One-time per install. macOS trusts the app on every subsequent launch.

If you'd rather have a clean drag-to-install experience without
Terminal, see `MAC_SETUP.md` → "Code signing and notarization" — that
path requires the $99/yr Apple Developer enrollment.

## Internal: cross-platform CI

New `.github/workflows/release.yml` builds Windows + Apple Silicon Mac +
Intel Mac on every `v*` tag push and uploads the installers to a
GitHub Release.
