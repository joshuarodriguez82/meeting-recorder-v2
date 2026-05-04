# v2.1.0 — macOS support

First-class macOS build. Same feature set as the Windows version, just
running on Mac. No fork, no second app — both platforms ship from the
same codebase.

> ## ⚠️ macOS install — READ THIS FIRST
>
> **The DMG will say "damaged and can't be opened" when you double-click it.**
> It is **not** damaged — the build is unsigned and macOS adds a
> quarantine flag to anything downloaded from the internet. The fix is
> four Terminal commands (one-time, per install):
>
> ```sh
> xattr -cr ~/Downloads/Meeting*Recorder*.dmg
> open ~/Downloads/Meeting*Recorder*.dmg
> # drag the app from the DMG to /Applications, then:
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> macOS treats the app as trusted on every subsequent launch — you only
> do this once. **Right-click → Open does not work** on macOS Sequoia /
> Sonoma; don't waste time trying it. Proper signing + notarization is
> on the roadmap; until then, the four commands above are the path.
>
> **Windows users** — none of this applies. Just download the `.msi`
> or `.exe` and double-click.

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

## Distribution caveat (Mac)

See the **macOS install — READ THIS FIRST** callout at the top of these
notes. Until Apple Developer signing + notarization is in place, the
four `xattr` / `open` commands are the install path on Mac. Windows is
unaffected.

## Internal: cross-platform CI

New `.github/workflows/release.yml` builds Windows + Apple Silicon Mac +
Intel Mac on every `v*` tag push and uploads the installers to a
GitHub Release.
