# Meeting Recorder — macOS Setup

This document is the complete checklist for getting Meeting Recorder
running on a fresh Mac. It mirrors the Windows install path in `README.md`
but covers the Mac-specific pieces (BlackHole for system audio, EventKit
permissions, code signing, etc.).

Tested on macOS 13 Ventura and 14 Sonoma, both Apple Silicon and Intel.
Should work on macOS 12 Monterey too (the Tauri bundle's
`minimumSystemVersion` is set to `12.0`), but EventKit's permission UX is
older there.

## What you need to install on this MacBook

In install order:

1. **Xcode Command Line Tools** — for the C compiler that some Python
   wheels (numpy, scipy, soundfile) need to fall back to if a precompiled
   wheel isn't published for your exact CPU+OS combo.
   ```sh
   xcode-select --install
   ```

2. **Homebrew** — package manager. If you've never installed it:
   ```sh
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   Apple Silicon installs into `/opt/homebrew`; Intel into `/usr/local`. The
   Rust shell looks in both.

3. **Python 3.13**:
   ```sh
   brew install python@3.13
   ```
   The Rust shell will find this via `python3.13` on PATH or at the
   standard Homebrew location.

4. **Node.js 20+** and **Rust** — for building the app from source:
   ```sh
   brew install node
   brew install rustup-init && rustup-init -y
   ```
   (Skip these two if you're going to install a prebuilt `.dmg` instead of
   building from source.)

5. **BlackHole 2ch** — virtual audio loopback driver. Without this you can
   only record your microphone, not the audio coming from the other
   meeting participants (Zoom/Teams/Meet output).
   ```sh
   brew install blackhole-2ch
   ```
   Then **reboot** so the kernel extension activates. (One-time only.)

6. **(Optional) ffmpeg** — only needed if you want to import non-WAV audio
   files via the Sessions → Import button. soundfile handles WAV natively
   without it.
   ```sh
   brew install ffmpeg
   ```

After step 5 the toolchain is complete. Steps 7+ are about the app itself.

## Build the app

```sh
git clone https://github.com/joshuarodriguez82/meeting-recorder-v2.git
cd meeting-recorder-v2

# 1. Backend venv + Python deps (5–10 min, mostly torch).
#    NOTE: use `python3.13`, NOT `python3`. The stock macOS `python3` is
#    Apple's 3.9, which can't install modern numpy or torch. setup.py
#    itself will bail with an explanation if you get this wrong.
python3.13 setup.py

# 2. Frontend deps + Rust toolchain initial compile
npm install

# 3. Build the .app + .dmg installer (3–5 min first time)
npm run tauri build

# 4. Patch privacy-usage strings into the bundled Info.plist so macOS will
#    actually grant mic / calendar / Apple Events access at runtime.
#    (Tauri 2 doesn't have a first-class field for these, so we inject
#    them post-build via plutil.)
./scripts/macos-postbuild.sh
```

After this you have:
- `src-tauri/target/release/bundle/macos/Meeting Recorder.app` — the app
- `src-tauri/target/release/bundle/dmg/Meeting Recorder_2.0.19_x64.dmg` —
  installer disk image (or `_aarch64.dmg` on Apple Silicon)

Drag the `.app` into `/Applications` (or run it from where it is — Mac
doesn't care).

## First launch

1. **Right-click → Open** the first time (NOT a regular double-click). The
   app isn't notarized yet, so Gatekeeper will refuse a normal launch with
   "can't be opened because Apple cannot check it for malicious software".
   Right-click → Open shows an Open button that bypasses that warning. You
   only have to do this once per install.

2. **Wait for the venv bootstrap.** First launch on a clean Mac without a
   `backend/.venv` will trigger the embedded `bootstrap_app_venv` flow,
   which does `python3.13 -m venv` + `pip install -r requirements-mac.txt`.
   Takes 3–5 minutes. The window opens immediately but API calls will fail
   with "backend not ready" until pip finishes. Tail
   `~/Library/Application Support/MeetingRecorder/bootstrap.log` to watch.

3. **Grant permissions when prompted:**
   - **Microphone** — required for any recording. macOS prompts on the
     first `sd.InputStream.start()`.
   - **Calendar (EventKit)** — needed for the Upcoming Meetings panel.
     macOS prompts on the first `/calendar/upcoming` request.
   - **Automation → Mail / Outlook** — needed for follow-up email drafts.
     macOS prompts on the first AppleScript send.

   If you accidentally deny any of these, re-enable them in
   **System Settings → Privacy & Security** → Microphone / Calendars /
   Automation. Permissions are bound to the bundle ID
   `com.joshuarodriguez.meeting-recorder`.

4. **Paste API keys.** Open Settings inside the app and add:
   - Anthropic API key (`sk-ant-api03-…`)
   - HuggingFace token (`hf_…`)
   then click Save Settings. Same instructions as the Windows README.

## Audio routing for system-audio capture

To capture what the other meeting participants are saying (not just your
voice), you need to route system audio through BlackHole. Two ways:

### Option A — Multi-Output Device (recommended, you can still hear audio)

1. Open **Audio MIDI Setup** (already installed; in Applications →
   Utilities).
2. Click the `+` button bottom-left → **Create Multi-Output Device**.
3. In the right pane, tick BOTH:
   - your normal output (Built-in Speakers, AirPods, etc. — first)
   - **BlackHole 2ch**
4. Right-click the new "Multi-Output Device" in the list → **Use This
   Device For Sound Output**.
5. In Meeting Recorder's Record view → System Audio dropdown, pick
   **BlackHole 2ch**.

Audio still plays through your speakers/headphones AND gets piped to
BlackHole, which Meeting Recorder records.

### Option B — BlackHole only (silent monitoring)

Set BlackHole 2ch as your default output directly. You won't hear anything
from your speakers, but recording works. Useful if you're only listening
through a separate headset on a different output.

## Auto-launch on login (optional)

Toggle "Launch on Windows startup" in Settings — the label says Windows
but the backend code is platform-aware and will install a LaunchAgent at
`~/Library/LaunchAgents/com.joshuarodriguez.meeting-recorder.plist` on
Mac. (Renaming the UI label is a frontend cleanup we can do later.)

## Troubleshooting

| Symptom | Fix |
|---|---|
| App launches then immediately quits | Check `~/Library/Application Support/MeetingRecorder/rust.log` and `backend.log`. Most likely: the bootstrap couldn't find Python 3.13 — install it via `brew install python@3.13`. |
| Mic recording is silent | System Settings → Privacy & Security → Microphone → toggle Meeting Recorder on. Then quit and relaunch. |
| Upcoming Meetings panel always empty | Same screen → Calendars → toggle on. Confirm in Calendar.app that you actually have meetings scheduled (the app reads whatever Calendar.app sees). |
| BlackHole doesn't appear in System Audio dropdown | After `brew install blackhole-2ch`, you must reboot. Driver loads at boot. |
| Follow-up drafts didn't appear in Mail | First time: System Settings → Privacy & Security → Automation → Meeting Recorder → toggle Mail (and/or Microsoft Outlook) on. Re-run the action. |
| "App is damaged and can't be opened" | Gatekeeper quarantine bit. Run `xattr -dr com.apple.quarantine "/Applications/Meeting Recorder.app"` and try again. |
| `npm run tauri dev` opens window but mic permissions never prompt | Dev-mode binary doesn't have the patched Info.plist. Build the release `.app` once with the steps above and run that — once permissions are granted to the bundle ID, dev-mode runs inherit them. |
| pip install fails on `lightning==2.6.1` (Apple Silicon) | Make sure `xcode-select --install` finished. If you have multiple Xcode versions, run `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer` to point at the full Xcode. |

## Code signing and notarization (when you want to distribute)

Right now the build is unsigned, which is why first-launch needs the
right-click-Open trick. To distribute to other Macs without that step:

1. Get an Apple Developer ID Application certificate from
   developer.apple.com (paid Apple Developer account, $99/yr).
2. Set in `tauri.conf.json` → `bundle.macOS`:
   ```json
   "signingIdentity": "Developer ID Application: Your Name (TEAMID)"
   ```
3. Build, then notarize:
   ```sh
   xcrun notarytool submit \
     "src-tauri/target/release/bundle/dmg/Meeting Recorder_2.0.19_aarch64.dmg" \
     --apple-id YOUR@EMAIL --team-id TEAMID --password APP_SPECIFIC_PASSWORD \
     --wait
   xcrun stapler staple "src-tauri/target/release/bundle/dmg/Meeting Recorder_2.0.19_aarch64.dmg"
   ```
4. Distribute the stapled DMG. First-launch on any Mac becomes a regular
   double-click.

That step isn't required for personal use on this MacBook.
