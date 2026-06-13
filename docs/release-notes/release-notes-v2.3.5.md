# v2.3.5 — Hotfix: macOS calendar permission persists across updates

The macOS calendar integration shipped in v2.3.0 has been silently
broken on every unsigned build because of how macOS Tahoe's TCC tracks
permission grants. v2.3.5 fixes it via ad-hoc code signing in the CI
pipeline with a custom Designated Requirement that matches by bundle
identifier instead of cdhash.

## What was actually broken

macOS records each TCC grant against **two** pieces of identity: the
binary's **cdhash** (a hash of the binary's contents) and its **Designated
Requirement** (a code-signing requirement string). When a process queries
its permission state later, TCC matches against either.

For unsigned apps, the default Designated Requirement is
`designated => cdhash H"..."` — strictly tied to the binary's contents.
Every rebuild produces a different binary = different cdhash = no DR
match. TCC keeps the bundle-ID-keyed entry visible in System Settings
(toggle stays "on"), but `authorizationStatusForEntityType` returns
`NotDetermined` because no recorded DR matches the running binary.

Net effect on v2.3.0 / v2.3.1 / v2.3.2 / v2.3.3 / v2.3.4 on macOS:
- User installs the app, sees Calendar prompt, clicks Allow.
- App appears in System Settings → Privacy & Security → Calendars with
  toggle on. Looks granted.
- App's `authorizationStatusForEntityType` returns 0 (NotDetermined).
- Both Rust and Python pyobjc paths report `auth_status=0` and
  `calendars_visible=0`. Calendar shows empty in the UI.

The v2.3.0 architecture (Rust reads EventKit inside the .app bundle,
writes JSON cache that Python reads) was the right idea for the
"Python venv is outside the bundle" half of the problem, but did
nothing for the cdhash drift half. Both halves had to be fixed.

## Fix

`.github/workflows/release.yml` now has a `Codesign with stable
identifier-based DR` step that runs after `npm run tauri-prebuild` and
the Info.plist patching, before the ditto-zip:

```sh
codesign --force --deep --sign - \
    --identifier com.joshuarodriguez.meeting-recorder \
    --requirements '=designated => identifier "com.joshuarodriguez.meeting-recorder"' \
    "Meeting Recorder.app"
```

This is still ad-hoc signing — no Apple Developer cert, no notarization.
First-launch Gatekeeper bypass instructions in the install path remain
exactly the same. Only the embedded Designated Requirement changes: from
the default cdhash-based one to a bundle-identifier-based one.

After v2.3.5:
- First-time users: install, see prompt, click Allow. TCC records grant
  with the new identifier-based DR.
- Future v2.3.6+ / v2.4 / v3.x installs: same DR (because we use a
  stable identifier). TCC matches by bundle ID, grant persists, calendar
  keeps working without re-prompting.

## If you were already on v2.3.4 with calendar broken

You need to reset the stale TCC grant once so v2.3.5 can record a fresh
one against the new DR:

```sh
# Quit the app
osascript -e 'tell application "Meeting Recorder" to quit' 2>/dev/null

# Wipe the stale Calendar grant for our bundle
tccutil reset Calendar com.joshuarodriguez.meeting-recorder

# Install v2.3.5 (drag .app to /Applications, replacing the old one)
# xattr -cr "/Applications/Meeting Recorder.app"

# Launch
open "/Applications/Meeting Recorder.app"

# Permission prompt appears — click Allow ONCE. Wait ~10 seconds.

# Verify
cat ~/Library/Application\ Support/MeetingRecorder/calendar_cache.json
```

You want to see `"auth_status": 3` and `"events": [...]` populated.

## Windows install

> v2.3.5 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.3.5_universal.zip`.
>
> The build is **unsigned for Gatekeeper purposes** (ad-hoc signature
> doesn't satisfy Gatekeeper's notarization requirement). First launch
> still needs the Gatekeeper bypass.
>
> **Path A — Finder:** double-click the `.zip` in Finder (Archive
> Utility auto-extracts to `Meeting Recorder.app`). Drag the `.app`
> to `/Applications`. Double-click, dismiss the "damaged" warning,
> then **System Settings → Privacy & Security → Open Anyway**,
> double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_*_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users** — Windows uses Outlook COM for calendar (entirely
> different code path) and is unaffected by this change. v2.3.5's
> Windows installer is functionally identical to v2.3.4's; no need to
> reinstall on Windows if you don't want to.

## What's in the release (unchanged from v2.3.0)

- **Conference room mode** — toggle in the Record view; forces
  mic-only capture and replaces `SPEAKER_YOU` with generic labels.
- **Offline AEC validator** — `python -m backend.scripts.measure_aec`
  plus the `KEEP_AUDIO_TEMPS=1` env var to preserve per-session WAVs.

## When we eventually buy an Apple Developer cert

Replace the `codesign --sign -` line with
`codesign --sign "Developer ID Application: ..." --options runtime`,
add a notarization step (`notarytool submit ... --wait`), and the app
also bypasses Gatekeeper cleanly (no more `xattr -cr` dance). The DR
stays the same — TCC grants persist across the unsigned→signed
transition because the identifier-based DR matches both.
