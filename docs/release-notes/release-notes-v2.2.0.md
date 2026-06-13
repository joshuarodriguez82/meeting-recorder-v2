# v2.2.0 — Better live transcription + in-call search

> ## ⚠️ macOS install — READ THIS FIRST
>
> ### Step 1: download the right file
>
> On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab the `.dmg` that matches your CPU:
>
> - **Apple Silicon** (M1, M2, M3, M4) → `Meeting.Recorder_2.2.0_aarch64.dmg`
> - **Intel Mac** → `Meeting.Recorder_2.2.0_x64.dmg`
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
> **Windows users** — none of this Gatekeeper stuff applies. Download
> the `.msi` or `.exe` from the Releases page and double-click.

## Summary

This release rebuilds the live transcription pipeline so the *other*
participants on a call actually show up in the live preview, adds a
search panel beside the live transcript so you can recall facts mid-
meeting, and fixes three crash bugs that could turn a healthy
recording into a "Recovered Session" with no transcript.

It also adds two free AI provider presets so the app no longer requires
an Anthropic key to do summaries / Q&A.

## What's new

### Live transcription

- **Dual-stream split.** The live preview now transcribes your mic
  and the system-audio loopback as two independent streams, tagged
  **You** and **Them**. The previous design mixed them into one mono
  channel before handing it to Whisper, which buried the far-end
  participants under your mic level — the live preview was effectively
  mic-only. Each segment now displays a colored speaker badge.

- **Mic duck gate.** When far-end audio is loud (other participants
  talking through your speakers), the mic copy that goes into live
  transcription is attenuated so the live "You" stream doesn't get
  polluted with garbled speaker bleed. This applies to live preview
  only — the full-fidelity mic WAV on disk is preserved untouched
  for the canonical post-stop transcript and speaker diarization.

### In-call search

A new panel sits beside the live transcript while you're recording,
with three scopes via toggle:

- **This call** *(default, instant, free)* — text find through the
  live transcript above. Type a phrase, hit Find — matching segments
  highlight with timestamps and speaker tags. No AI call, no API
  cost. Best for mid-meeting recall ("did anyone mention pricing
  yet?", "what was that company name?").

- **This call (AI)** — Claude answers a question grounded in the
  live transcript. The most recent ~8 KB of transcript is sent as
  context. Use for synthesis questions text find can't answer:
  "what number did she give for Q3?", "summarize what we've
  discussed so far".

- **Past meetings** — semantic search across your indexed meeting
  history, answered with citations. Same pipeline as the standalone
  Q&A tab; just exposed during a recording so you don't navigate
  away mid-call. Click-through to source sessions is disabled
  during recording — the cited session IDs are shown for follow-up
  after you stop.

### Free AI providers

Settings → AI Provider gains two new presets joining OpenRouter and
Ollama:

- **Groq** — generous free tier, fastest hosted inference available
  (~1 second for a meeting summary). Llama 3.3 70B, Mixtral, Gemma.
  Get a key at [console.groq.com](https://console.groq.com/keys).

- **Google Gemini** — free tier with daily request limits, fine for
  personal use. Gemini 2.0 Flash and 2.0 Flash-Lite.
  Get a key at [aistudio.google.com](https://aistudio.google.com/apikey).

Each preset auto-fills the base URL and a sensible default model so
you only paste the key. The existing OpenAI-compatible plumbing
handles the actual API calls — no provider-specific code paths.

## Bug fixes — three real crashes

These were all surfacing as "Failed to fetch" on Stop and recordings
showing up as **Recovered Session** with empty transcripts.

- **PyAudio shutdown race on Windows.** The loopback reader thread
  was joined *before* the WASAPI stream was stopped, so the read()
  call inside the thread was still in flight when the main thread
  closed the stream — segfaulting Python at native level. Reordered
  to: stop_stream → join thread → close → terminate.

- **`LiveTranscriber.stop()` blocking for 10 seconds.** The
  worker's tail-flush ran two Whisper calls inside the stop path,
  and on CPU those exceeded the join timeout. The frontend's POST
  to `/recording/stop` then exceeded Tauri's invoke timeout and
  surfaced as "Failed to fetch" — even though the file was saving.
  Stop is now non-blocking; the tail flush continues in background
  and exits on its own. Total stop flow dropped from 10.1s → 0.1s.

- **Header-only loopback WAV crashing libsndfile.** When the
  loopback endpoint never delivered any audio (wrong device picked,
  or system-audio routed elsewhere), the WAV file ended up as just
  a header — and feeding that to `soundfile.info()` segfaulted
  Python before any error could be caught. Added a 1 KB size
  guard before the call.

Plus per-step diagnostic logging through the entire stop flow, so
any future failure reports the specific substep instead of just
trailing off the log.

## Other fixes

- **`window.confirm()` works again.** The Tauri 2 default capability
  set didn't include `dialog:allow-confirm`, so delete-confirmation
  dialogs threw "dialog.confirm not allowed. Command not found" in
  dev builds. Added the missing capability.

- **Defensive guard against empty mic recordings** — finalize now
  detects an empty / silent mic WAV and degrades gracefully instead
  of attempting a merge that would have produced an unplayable file.

## Internal

- Per-step instrumentation in `recording_service.stop_recording` and
  `AudioCapture.stop` — every substep logs entry + elapsed so
  failure modes are diagnosable from the log alone, not by guessing.

- `LiveTranscriber` rewritten to a single-worker, two-buffer design
  that alternates between mic and loopback windows. Avoids
  `WhisperModel` thread-safety concerns vs running two parallel
  inferences while still preserving independent per-source timing
  and speaker tags.
