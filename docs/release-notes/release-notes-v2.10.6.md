# v2.10.6 — Session log no longer fights the cloud-sync driver during capture, audio-format mismatch warning, bootstrap survives Python 3.13 + tokenizers wheel gap

Four fixes (two were rolled into this release after the first
v2.10.6 ZIP shipped — re-download to get them):

1. **Critical, real-time** — the session log file was still being
   written into `recordings_dir` during active capture even after the
   v2.10.5 WAV-temp fix. When `recordings_dir` is on OneDrive /
   Google Drive Stream / iCloud, the cloud sync filter driver stalls
   every log write for hundreds of ms, and the audio capture ring
   buffer overflows during those stalls. Field repro on 2.10.5: a
   55-min "Demo Discussion Emily" session recorded to
   `G:\My Drive\Recordings\` showed **1215 s of mic↔loopback drift**
   (~22 s per minute) because the WAVs went local but the session
   log was still on Google Drive Stream.
2. **Preventative** — the Record view now detects when the mic and
   System Audio loopback are configured at different default formats
   (sample rate or bit depth) and surfaces an amber banner above the
   device pickers with the exact mismatch + an **Open Sound Control
   Panel** button. Catches the v2.10.5 follow-on bug that needed two
   round-trips of field repro to diagnose.
3. **Critical, install-blocker** — the pinned `huggingface_hub==0.23.0`
   forced pip's resolver to backtrack into `transformers 4.41.x` →
   `tokenizers 0.19.1`, which has no Python 3.13 wheel and whose sdist
   fails to build (`pyo3 0.21.2` references
   `PyUnicode_FromKindAndData` / `PyUnicode_4BYTE_KIND`, both removed
   from the Python C API in 3.13). Field repro: the first v2.10.6 ZIP
   wouldn't start on a clean Windows 11 install — the bootstrap died
   at "no matching distribution found for tokenizers". The pin is now
   `>=0.23,<1.0`; the `<1.0` upper bound preserves pyannote.audio's
   `use_auth_token=` requirement.
4. **Defense-in-depth** — bootstrap's `pip install -r` pass is now
   best-effort on the upgrade path. If pip trips on an existing venv
   but the critical modules (`fastapi`, `sounddevice`,
   `faster_whisper`, `pyannote.audio`, `torch`, `huggingface_hub`)
   import cleanly, the backend now starts with the existing wheel set
   and logs the pip warning instead of refusing to launch. Marker
   stays unwritten so the next launch retries. Fresh venvs still fail
   hard — nothing to fall back to.

## Install (macOS)

> v2.10.6 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.10.6_universal.zip`.
>
> Still unsigned for Gatekeeper purposes. First launch needs the
> Gatekeeper bypass — pick whichever path you prefer:
>
> **Path A — System Settings (no Terminal):** double-click the `.zip`
> in Finder (Archive Utility auto-extracts to `Meeting Recorder.app`),
> drag the `.app` to `/Applications`, double-click, dismiss the
> "damaged" warning, then **System Settings → Privacy & Security →
> Open Anyway**, double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_2.10.6_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.10.6_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's fixed

### 1. Session log moved to local temp during capture

`_start_session_log` attached a `logging.FileHandler` to the **root**
logger at **DEBUG** level, writing to
`recordings_dir/session_<id>.log`. Root-logger attachment means every
`logger.info` / `logger.debug` from any thread — *including the
sounddevice audio-capture callback* — wrote synchronously through
that handler.

When `recordings_dir` is on a cloud-sync mount, each blocking write
takes 50–500 ms vs ~50 μs to local NTFS. The OS audio ring buffer
overflows during those stalls; samples get dropped on whichever
capture thread logs. Over 55 minutes, accumulated drops totalled
~20 minutes of lost samples on one stream.

The v2.10.5 fix moved the WAV streaming temps to
`%TEMP%\meeting_recorder_capture\` but missed this file — same
architectural issue, different filename.

Fix mirrors the WAV temp pattern: the session log writes to
`%TEMP%\meeting_recorder_capture\session_<id>.log` during the live
capture (zero cloud-filter contact in the audio thread's hot path).
On stop, `_stop_session_log` copies the finished log to
`recordings_dir/session_<id>.log` as a one-shot write that the cloud
sync absorbs cleanly. Copy failure is non-fatal — the local temp log
remains for debugging.

After upgrading, you can keep `RECORDINGS_DIR` on OneDrive / Google
Drive / iCloud and capture cleanly — neither the WAV streams nor the
session log fight the sync driver any more.

### 2. Audio-format mismatch warning on the Record view

New endpoint `GET /audio/sync-risk` compares each selected device's
WASAPI shared-mode mix format via pycaw
(`IAudioClient::GetMixFormat`). When the mic and the System Audio
loopback don't match on sample rate or bit depth, the Record view
shows an amber banner above the device pickers explaining the
mismatch and offering an **Open Sound Control Panel** button that
deep-links to `mmsys.cpl` (Windows) / Sound preferences (macOS).

Why this matters: the Windows audio engine resamples each side
independently. When the two endpoints' default formats differ — e.g.
mic at 16-bit and speakers at 24-bit — the resamplers drift relative
to each other, and the inter-stream offset accumulates linearly. A
v2.10.5 field session at 16-bit + 24-bit produced ~31 s of drift on
49 min before the user noticed. The new banner catches this
*before* the recording instead of after.

Banner only shows on Windows (where pycaw resolves the mix format)
and is suppressed during active recording / conference-room mode.
On macOS / Linux the endpoint returns `level="unknown"` and the UI
stays out of the way.

The Usage Guide gains a new **Warn** block under "Audio routing"
documenting the matching requirement, in case the runtime detection
ever fails to fire.

### 3. `huggingface_hub` pin loosened across all three requirements files

`backend/requirements.txt`, `backend/requirements-cpu.txt`, and
`backend/requirements-mac.txt` now carry:

```
huggingface_hub>=0.23,<1.0
```

instead of `==0.23.0`. The upper bound still satisfies pyannote.audio
3.3.2's `use_auth_token=` requirement (that keyword was removed in
hf_hub 1.0). The lower bound matches what was already tested. The pip
resolver is no longer cornered into an unbuildable transformers /
tokenizers combination on Python 3.13.

Why the strict pin existed in the first place: an earlier release wanted
to keep the test matrix narrow when pyannote's hf_hub compatibility
window was unclear. Now that we know the lower bound for
`use_auth_token=` and the upper bound for hf_hub 1.0 specifically, the
range pin is both more permissive AND tighter on the actual breakage.

### 4. Bootstrap doesn't fail closed when the existing venv is healthy

`src-tauri/src/lib.rs::bootstrap_app_venv` previously returned an error
the moment `pip install -r` exited non-zero. For a fresh venv that's
correct — there are no Python modules installed yet, so a pip failure
genuinely means the backend can't start. For an UPGRADE from a working
prior version, however, the existing venv often has every module the
backend needs at startup, and a pip reverify failure (because the new
requirements file ran into a resolver quirk on the current pip / Python
combination) shouldn't be fatal.

The new behavior:

- **Fresh venv + pip install fails** → fatal, as before.
- **Existing venv + pip install fails + critical modules import** →
  logged warning, backend starts using the existing wheel set. The
  marker file is NOT updated, so the install retries on the next
  launch.
- **Existing venv + pip install fails + critical modules missing** →
  fatal, as before. The user gets the same actionable error they'd
  have gotten on the first v2.10.6 build.

The probe runs `python -c "import fastapi, pydantic, sounddevice,
soundfile, faster_whisper, pyannote.audio, torch, huggingface_hub"`
and considers the venv runnable iff every import succeeds.

## New backend dependency

`pycaw>=20240210; sys_platform == "win32"` — pure-Python WASAPI
binding via comtypes. Read-only mix-format query; no native build,
no driver install. The first-launch bootstrap pulls it automatically.

## Tauri shell

New allowlist-only command `open_system_settings(panel)`. Currently
the only supported panel is `"sound"` (maps to `mmsys.cpl` on
Windows, `x-apple.systempreferences:com.apple.preference.sound` on
macOS, `pavucontrol` / `gnome-control-center` best-effort on Linux).
Adds an explicit allowlist tag — never accepts a free-form path —
so the WebView can't `shellexec` arbitrary applets.

## Known not yet patched

- **Audio-format detection on macOS / Linux** — `IAudioClient` is
  Windows-only. The Mac CoreAudio equivalent (HAL property scope
  queries) is a separate integration; tracked for a follow-up. Until
  then non-Windows users see the Usage Guide entry rather than a
  runtime banner.
- **Sync-integrity historical sessions** — the warning is written
  into each session's JSON at capture time and isn't recomputed when
  the JSON is later loaded. After upgrading to 2.10.6, NEW recordings
  on a cloud-mounted `recordings_dir` should report `<5 s` drift.
  Older sessions retain the warnings they were captured with.
