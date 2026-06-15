# v2.2.5 — Calendar permission fix on macOS

A point fix on top of v2.2.4. Calendar integration silently failed on
macOS Sequoia / Sonoma / Tahoe because the bundle was missing the
`NSCalendarsFullAccessUsageDescription` Info.plist key — without that
string, EventKit never shows the "Meeting Recorder would like to access
your calendar" prompt and every fetch returns zero events.

> ## ⚠️ macOS install — READ THIS FIRST
>
> v2.2.5 ships **a single universal `.dmg`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.2.5_universal.dmg`.
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
> **Windows users** — none of this Gatekeeper stuff applies. Download
> the `.msi` or `.exe` and double-click.

## If you installed v2.2.4 first

macOS may have a stale (or missing) TCC entry from the broken build.
After installing v2.2.5, reset the calendar permission entry so the
prompt fires fresh on next launch:

```sh
tccutil reset Calendar com.joshuarodriguez.meeting-recorder
```

Then quit and re-open Meeting Recorder. The first time it polls the
calendar (Record tab → Upcoming meetings), macOS shows the EventKit
permission dialog. Click **OK / Allow**. Going forward the app will
appear in **System Settings → Privacy & Security → Calendars** and
stay granted across restarts.

## What changed

`src-tauri/Info.plist` (new file) declares the four usage strings the
hardened-runtime entitlements were already claiming:

- `NSCalendarsFullAccessUsageDescription` (macOS 14+) and
  `NSCalendarsUsageDescription` (legacy) — the calendar prompt this
  release is fixing.
- `NSMicrophoneUsageDescription` — was previously coming through some
  Tauri default path; now explicit and matched to the recorder.
- `NSAppleEventsUsageDescription` — the AppleScript prompt you see when
  the app drafts a follow-up email in Mail.app or Outlook for Mac.

Tauri 2's bundler auto-merges any `Info.plist` it finds next to
`tauri.conf.json`, so no config change was needed for it to take
effect.

## Nothing else changed

All v2.2.4 features (pre-meeting briefs, commitments tracker,
trackable follow-ups, decision lifecycle, auto-stop watchdog, semantic
index auto-build) are unchanged. v2.2.5 is purely about making the
calendar integration actually work on modern macOS.
