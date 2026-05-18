# Meeting Recorder — Android companion (v3.0.0)

On-the-go mic recording for Android that drops `session_<id>.m4a` +
`session_<id>.json` into the **same OneDrive folder the desktop app
reads**, so you record on your phone and Process on your PC later. No
backend, no login — pure file sync, exactly like the desktop's
cross-device flow.

## What it does — and the one thing it can't

- **Personal / in-person recording**: device mic, foreground service, so
  it keeps recording with the screen off or the phone in a pocket.
- **Teams / Zoom calls in the car**: put the call on **speakerphone or
  car speakers**. The phone mic then captures both you *and* the far-end
  voice coming out of the speaker. This is the only thing that works —
  **Android forbids any app from tapping another app's call audio**, and
  Teams/Zoom additionally opt out of screen/audio capture. Held to your
  ear or on a Bluetooth headset, only your side is recorded.
- **Tagging**: name, client, project (auto-completed from
  `client_configs.json` + existing synced sessions), and notes that feed
  the AI summary on the PC.
- **Reliable sync**: a finished recording goes into a durable local
  queue and only leaves once both files are confirmed in the OneDrive
  folder. Failed/interrupted writes (no space, revoked folder grant, app
  killed) retry with backoff instead of losing audio. Same-named files
  are overwritten on retry, so there are no `... (1).m4a` duplicates.

Not in v3.0: **calendar-driven auto-record on the phone**. The desktop
already auto-records from the calendar; the phone toggle is present but
disabled until the next build.

## Architecture

```
mobile/
  src/                    # React + Vite UI (Record / Recordings / Settings)
    native/recorder.ts    # typed bridge to the Kotlin plugin (+ browser stub)
    lib/session.ts        # builds the EXACT desktop Session.to_dict() JSON
    lib/store.ts          # folder grant, defaults, durable sync queue
    lib/sync.ts           # queue drain w/ backoff, synced-folder reads
  android-overlay/        # custom native code (source of truth)
    java/.../RecorderPlugin.kt     # SAF + recording control + folder reads
    java/.../RecordingService.kt   # foreground service owning MediaRecorder
    java/.../MainActivity.kt       # registers the app-local plugin
  scripts/apply-android-overlay.mjs# injects the above into cap-generated android/
```

`android/` is **build output** (gitignored). It's regenerated from
`android-overlay/` by `apply-android-overlay.mjs` so there's a single
source of truth and `cap sync` can never clobber the native code.

## Build the APK

CI does this automatically: pushing a `v*.*.*` tag runs
`.github/workflows/android.yml`, which builds `assembleDebug` and
attaches `Meeting.Recorder_<version>_android.apk` to the GitHub Release.
`workflow_dispatch` produces the same APK as a downloadable workflow
artifact for dry-runs.

Locally (needs Node 20+, JDK 21, Android SDK):

```sh
cd mobile
npm install
npm run android:apk
# → android/app/build/outputs/apk/debug/app-debug.apk
```

`npm run dev` runs the UI in a desktop browser using a `MediaRecorder`
fallback (recording works; "sync" triggers downloads). Good for
iterating on the UI without a device; it does **not** exercise the
foreground service or SAF.

## Install on the phone (sideload)

The APK is debug-signed (installable, no Play Store, no Apple-style
Gatekeeper). On the phone:

1. Download the `.apk` (or transfer it over).
2. Tap it. Android asks to allow your browser/file manager to "install
   unknown apps" — allow it, then install.
3. Open the app → **Settings → Pick OneDrive folder**. In the system
   picker, browse into the **OneDrive** provider and select the *same*
   folder the desktop's Recordings Folder points at (e.g.
   `OneDrive/MeetingRecorder`). Grant mic + notification permissions.
4. Record. Recordings sync via the OneDrive app; the PC picks them up
   and you run **Process** there as usual.

## How the desktop picks these up

The desktop scans `RECORDINGS_DIR` for `session_*.json`. A phone
recording's `audio_path` is a phone path that doesn't exist on the PC,
so v3.0 adds `SessionService.resolve_audio_path()`: when the stored path
is absent, it falls back to `session_<id>.<ext>` sitting next to the
JSON — exactly what the phone synced. This also retroactively fixes
PC↔Mac audio playback/processing, which had the same latent gap.
