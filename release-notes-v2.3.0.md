# v2.3.0 — Conference room mode

A new recording mode for the case where you're in a physical meeting room
with everyone sharing speakers and a single mic. The previous default —
record mic *and* system loopback — doubles up the room audio (you hear
the speakers through the mic, then again through loopback) and produces
a transcript with phantom "SPEAKER_YOU" labels for things other people
in the room said.

Flip the new **Conference room mode** toggle in the Record view and the
recorder captures mic only, dropping system loopback entirely. Diarization
runs against the single channel, so participants get generic
SPEAKER_00 / SPEAKER_01 / … labels you can rename after the fact, instead
of having half the room mislabelled as you.

> ## ⚠️ macOS install — READ THIS FIRST
>
> v2.3.0 ships **a single universal `.dmg`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.3.0_universal.dmg`.
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

## Conference room mode in detail

A new toggle near the device selectors:

- **Off (default)** — unchanged from v2.2.5. Mic + system loopback are
  both captured; far-end participants are tagged from the loopback
  stream and the user from the mic stream.
- **On** — system loopback is forced off for the entire recording
  (the output-device selector is ignored). Live and final transcripts
  use generic `SPEAKER_XX` labels with no "you said" attribution. Best
  fit for in-person meetings where everyone is in one room sharing a
  speaker.

The toggle's last position is remembered in `localStorage` so a recurring
conference room user doesn't have to re-flip it every session.

## Bonus: offline AEC validator (dev-only)

Ships alongside the feature for anyone curious about pursuing real-time
echo cancellation in a future release:

```sh
# Synthetic math sanity check
python -m backend.scripts.measure_aec --self-test

# Run against an existing recording
KEEP_AUDIO_TEMPS=1 npm run tauri dev   # preserves the per-session WAVs
# ...record a meeting, then:
python -m backend.scripts.measure_aec --session <session-id>
```

Loads mic + loopback WAVs, runs an offline NLMS adaptive filter, and
prints **ERLE** (Echo Return Loss Enhancement) numbers + an optional
cleaned mic WAV. The point: empirically measure whether shipping a real-
time AEC integration would actually improve the recording, before
investing in the cross-platform packaging work that would entail. Not
exposed in the UI — purely a measurement tool.

`KEEP_AUDIO_TEMPS=1` is a new opt-in env var that skips the post-finalize
cleanup of `_recording_<id>.wav` and `_loopback_<id>.wav`. Default
behaviour is unchanged (cleanup still runs).

## Nothing else changed

All v2.2.5 features (calendar permission fix, pre-meeting briefs,
commitments tracker, trackable follow-ups, decision lifecycle, auto-stop
watchdog, semantic index auto-build) are unchanged. v2.3.0 adds the
conference mode toggle and the validator script, and that's it.
