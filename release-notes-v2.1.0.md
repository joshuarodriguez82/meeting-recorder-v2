# v2.1.0 — macOS support

First-class macOS build. Same feature set as the Windows version, just
running on Mac. No fork, no second app — both platforms ship from the
same codebase.

> ## ⚠️ macOS install — READ THIS FIRST
>
> ### Step 1: download the .dmg
>
> Grab the `.dmg` from the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases).
> v2.1.0 was released as two separate architecture-specific files
> (`_aarch64.dmg` for Apple Silicon, `_x64.dmg` for Intel) — pick the
> one that matches your Mac's CPU. From v2.1.1 onward there's a single
> `_universal.dmg` that runs on either.
>
> (The filename writes spaces as dots — that's just the GitHub Actions
> artifact naming. The app's display name is still "Meeting Recorder".)
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
> # 1. Strip the quarantine flag from whichever Meeting Recorder DMG
> #    is in Downloads. The Meeting* glob handles either dot or space
> #    in the filename.
> xattr -cr ~/Downloads/Meeting*.dmg
> open ~/Downloads/Meeting*.dmg
>
> # 2. In Finder: drag the app icon to Applications.
>
> # 3. Confirm the installed app's exact filename — could have a
> #    space or a dot depending on the build.
> ls /Applications/ | grep -i meeting
>
> # 4. Strip quarantine on the installed app and launch. Quote the
> #    path if it contains a space.
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> macOS treats the app as trusted on every subsequent launch — you only
> do this once. **Right-click → Open does not work** on macOS Sequoia /
> Sonoma; don't waste time trying it. Proper signing + notarization is
> on the roadmap; until then, the steps above are the install path.
>
> **If you copy commands from chat / Slack:** some clients auto-link
> `Recorder.app` (treats `.app` as a TLD) and turn it into
> `[Recorder.app](http://Recorder.app)` in the paste. Either type the
> commands by hand, or copy from the GitHub-rendered README directly.
> If you see `^[[200~` echoed in your terminal that's a stray
> bracketed-paste sequence — Ctrl+C and re-type.
>
> ### Step 3: first-run setup (~5 minutes)
>
> First launch bootstraps a Python venv and downloads ML models — the
> window opens immediately but features will say "backend not ready"
> for ~5 minutes while pip installs. Tail
> `~/Library/Application\ Support/MeetingRecorder/bootstrap.log` to
> watch progress. After bootstrap finishes:
>
> 1. Open **Settings**, paste your Anthropic API key + HuggingFace
>    token, click Save.
> 2. (Optional) For system-audio capture: `brew install blackhole-2ch`
>    + reboot, then route system audio through it via Audio MIDI Setup.
>    See [MAC_SETUP.md](./MAC_SETUP.md) for the full walkthrough.
> 3. Quit and relaunch — pyannote downloads its models on first
>    Process (~200 MB, one-time).
>
> **Windows users** — none of this Gatekeeper stuff applies. Download
> the `.msi` or `.exe` from the same Releases page and double-click.

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
notes. Until Apple Developer signing + notarization is in place,
first-launch on Mac requires either System Settings → Privacy &
Security → Open Anyway, or one `xattr -cr` Terminal command. Windows
is unaffected — `.msi` / `.exe` install normally.

## Internal: cross-platform CI

New `.github/workflows/release.yml` builds Windows + Apple Silicon Mac +
Intel Mac on every `v*` tag push and uploads the installers to a
GitHub Release.
