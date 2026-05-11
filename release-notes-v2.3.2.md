# v2.3.2 — Hotfix: ship Mac as ZIP instead of DMG

Same features as v2.3.0 and v2.3.1 (conference room mode + offline AEC
validator). Both prior releases shipped Windows successfully, but
neither produced a Mac installer:

- **v2.3.0** — three Rust compile errors in `calendar_macos.rs` against
  `objc2-event-kit 0.3.2`.
- **v2.3.1** — fixed those, but the build then died inside Tauri's
  `bundle_dmg.sh`: the AppleScript-against-Finder window-layout step
  gives up silently on the macos-14 / Apple Silicon GitHub Actions
  runner, and Tauri swallows its stderr.

v2.3.2 sidesteps that by passing `--bundles app` to `tauri build` on
macOS and `ditto`-zipping the resulting `.app` ourselves. Apple
recommends `ditto` for un-notarized distribution anyway — it preserves
the bundle's extended attributes and any future code signature, which
plain `zip` does not. The Mac artifact name changes:
`Meeting.Recorder_2.3.2_universal.dmg` → `Meeting.Recorder_2.3.2_universal.zip`.

> ## ⚠️ macOS install — READ THIS FIRST
>
> v2.3.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.3.2_universal.zip`.
>
> The build is **unsigned** — first launch needs the Gatekeeper bypass.
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
> **Windows users** — none of this Gatekeeper stuff applies. The
> Windows installer is unchanged across v2.3.0 / v2.3.1 / v2.3.2
> (every difference between them lives behind `#[cfg(target_os = "macos")]`
> or in the CI workflow's macOS branch), so any of the three works.

## What changed since v2.3.1

- `.github/workflows/release.yml` — Mac matrix passes `--bundles app`;
  "Collect installer paths (macOS)" step now runs `ditto -c -k
  --sequesterRsrc --keepParent` against the built `.app` instead of
  copying a `.dmg`.
- `AGENTS.md` — updated the Gatekeeper-bypass template for future
  release notes so the `.zip` format is reflected.
- `src-tauri/src/calendar_macos.rs` — removed three `unused_unsafe`
  warnings the v2.3.1 build emitted (`NSDate::timeIntervalSince1970`
  and `NSURL::absoluteString` are safe in `objc2-foundation 0.3.2`).

## What's in the release (unchanged from v2.3.0)

- **Conference room mode** — new toggle in the Record view; forces
  mic-only capture and replaces `SPEAKER_YOU` with generic labels.
- **Offline AEC validator** — `python -m backend.scripts.measure_aec`
  plus the `KEEP_AUDIO_TEMPS=1` env var to preserve per-session WAVs.
