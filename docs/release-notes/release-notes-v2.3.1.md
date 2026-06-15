# v2.3.1 — Hotfix: ship the macOS build

Same features as v2.3.0 (conference room mode + offline AEC validator).
v2.3.0's Mac build failed in CI before the `.dmg` could be uploaded, so
the GitHub Release for v2.3.0 only carried Windows installers.

This release is just v2.3.0 + the three Rust fixes needed to make
`src-tauri/src/calendar_macos.rs` compile against `objc2-event-kit 0.3.2`:

1. `EKEventStore::new()` is now `unsafe` — wrapped accordingly.
2. `requestFullAccessToEventsWithCompletion` wants a `*mut Block<...>`,
   not `&Block<...>` — we cast explicitly instead of relying on Rust to
   auto-coerce (it won't).
3. `NSURL::absoluteString()` returns `Option<Retained<NSString>>`, not
   `Retained<NSString>` — added the `.map()` wrap so the `p.name()`
   fallback is preserved when the URL has no absoluteString.

No behaviour change for users vs v2.3.0; this just lets the Mac DMG
actually build and ship.

> ## ⚠️ macOS install — READ THIS FIRST
>
> v2.3.1 ships **a single universal `.dmg`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.3.1_universal.dmg`.
>
> The build is **unsigned** — first launch needs the Gatekeeper bypass.
>
> **Path A — System Settings:** double-click the app, dismiss the
> "damaged" warning, then **System Settings → Privacy & Security →
> Open Anyway**, double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> xattr -cr ~/Downloads/Meeting*.dmg
> open ~/Downloads/Meeting*.dmg
> # drag to Applications, then:
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users** — none of this Gatekeeper stuff applies. The
> Windows installer in v2.3.0 is identical to v2.3.1's (only Rust
> code that's gated behind `#[cfg(target_os = "macos")]` changed), so
> you can stay on v2.3.0 or upgrade — either works.

## What's in the release

See `release-notes-v2.3.0.md` — feature set is unchanged:

- **Conference room mode** — new toggle in the Record view; forces
  mic-only capture and replaces `SPEAKER_YOU` with generic labels.
- **Offline AEC validator** — `python -m backend.scripts.measure_aec`
  plus the `KEEP_AUDIO_TEMPS=1` env var to preserve per-session WAVs.
