# v2.19.1 — the crash-loop fix: fast startup, recording never waits on AI

> **What this release fixes (root-caused from your logs):**
>
> The backend was **segfaulting** (`0xC0000005`) during AI model
> loading and crash-looping through watchdog respawns — that was the
> real cause of the "failed to fetch" storms, the endless morning
> startup, and the "models must load before anything works" feeling.
> Four fixes land together:
>
> 1. **Model loading is single-flight.** Five different triggers could
>    each spawn a load thread; at app-open two could pass the guard
>    together, and two concurrent native torch/ctranslate2 inits in
>    one process intermittently crash on Windows. Now an atomic lock
>    guarantees exactly one loader, ever.
> 2. **OpenMP double-runtime hardening.** faster-whisper and pyannote
>    each bundle their own OpenMP; `KMP_DUPLICATE_LIB_OK=TRUE` is set
>    before any ML import — the documented mitigation for this exact
>    native-crash class.
> 3. **Startup no longer imports the ML stack before the server can
>    answer.** The boot-time dependency check imported
>    sentence-transformers + speechbrain (which pull in torch) before
>    the HTTP port even bound — on AV-scanned corporate machines that
>    held "Starting backend…" hostage for 30s–3min. It now checks
>    package metadata only: milliseconds.
> 4. **Recording never requires AI models.** Opening the app no longer
>    auto-loads models at all. They load only when actually needed:
>    when a recording starts **with live transcription enabled**, or
>    when you hit Process. With live transcription off, you can record
>    all day without a single model in memory.
>
> Plus: when the backend does restart, the app now shows
> **"Reconnecting to backend…"** instead of raining raw
> "failed to fetch" errors.

## Install (macOS)

> v2.19.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.19.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.19.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.19.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The evidence this was built on

From the user's own machine logs (2026-07-21):

- `rust.log`: 11 × `Backend exited unexpectedly:
  ExitStatus(3221225477)` — a native access violation — including
  three crashes inside two minutes.
- `backend.log`, same morning: `Loading transcription engine…` with
  **no** `Models loaded`, followed 23 seconds later by a fresh
  `Backend started` (the process died mid-load and was respawned),
  twice in a row, before the third attempt survived.

Every "failed to fetch" was a window where the process was dead or
rebooting. The fixes above target that chain directly, and a new
regression test hammers `ensure_models_loaded` from 8 threads and
asserts the load body runs exactly once with zero concurrent entries.

## Also worth knowing

- **Gemini free tier is 20 requests per day** (`generate_content_free_
  tier_requests: 20`). At the live co-pilot's 45-second cadence that's
  exhausted ~15 minutes into your first meeting, after which every
  tick is rate-limited (429) — the co-pilot goes quiet for the rest of
  the day. If you want Gemini for live ticks, enable billing on the
  key; otherwise point the co-pilot at Anthropic Haiku.

## Under the hood

- `Services.ensure_models_loaded` guard is atomic under a
  `threading.Lock` (single-flight).
- `KMP_DUPLICATE_LIB_OK=TRUE` set at the top of `server.py` before any
  ML import path.
- `_verify_and_repair_dependencies` uses `importlib.util.find_spec`
  (metadata) instead of `importlib.import_module` (execution).
- Record-view no longer calls `/models/load` on mount; the two
  record-start pre-warms are gated on `live_transcription_enabled`.
- Status-poll failures (2+ consecutive) render a "Reconnecting to
  backend…" banner that clears on the next successful tick.
- 3 new tests (single-flight under 8-way concurrency, idempotent
  re-call, failure releases the flag + surfaces the error). Full
  backend suite: 111 passing.
