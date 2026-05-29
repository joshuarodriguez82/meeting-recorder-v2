# Meeting Recorder v2.10.2

Reliability, accuracy, and onboarding release. Auto-processing now fires on every stop path, a retry queue + diagnostics panel make failures visible instead of silent, transcription is biased toward your domain vocabulary, briefs can auto-generate before meetings, and new users get a guided setup tour.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.10.2_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
> 2. Drag into `/Applications`.
> 3. Double-click. Dismiss the "damaged" warning.
> 4. **System Settings → Privacy & Security**, scroll to bottom, click **Open Anyway**.
> 5. Double-click again, click **Open**.
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
> Windows users — just install the `.msi` or `.exe`.

---

## What's new

### Auto-processing now works no matter how a recording stops
Auto-processing was only triggered by the Record view's Stop button. Any other stop path — the sidebar recording-pill Stop, or an automatic stop (silence / overrun / hard cap on an auto-recorded meeting) — finalized the audio but never processed it, so the session sat with no AI output even with auto-process on. It's now **backend-owned**: every stop path runs the full pipeline (transcribe → speakers → summary → actions → decisions → requirements → commitments).

### Processing retry queue + visible failures
Transient failures (Claude rate-limit, a brief model hiccup, a file still syncing) used to leave a session silently unprocessed forever. Failed auto-processing now **retries with backoff** (30s → 2min → 5min). If it still fails, the session shows a **red badge in the Sessions list with the reason** — "open the session and click Process to retry." A successful run clears it.

### Diagnostics panel (Settings → Diagnostics)
Live system health with red/amber/green dots: **Live Co-Pilot model reachability** (is Ollama running?), AI provider config, microphone + system-audio devices, recordings-folder writability, models loaded — plus an expandable **backend log tail with a Copy button**. No more digging through log files to find out why something didn't work.

### Domain terminology (Settings → Domain terminology)
Whisper mangles dense jargon — "Genesys" → "Genesis", "UCCX" → "you see ex", "CCaaS" → "see-cass" — and every mistranscription poisons the downstream summary. A seeded glossary (Solutions Architect / CCaaS / cloud / sales vocabulary) now biases the transcriber toward the right spelling and corrects known mis-hears after the fact. Fully editable.

### Auto pre-meeting brief (Settings → Auto pre-meeting brief)
Optionally generate a prep brief from your prior sessions a configurable number of minutes before each calendar meeting, with a "prep brief ready" notification. Runs on a backend timer (works even if the app isn't focused). Off by default.

### OneDrive cloud-file fix
When your recordings folder lives in OneDrive, older WAVs get evicted to cloud-only placeholders. Processing them failed with an opaque error mid-pipeline. The app now **hydrates a cloud placeholder to local disk before processing**, and shows a clear message if a file genuinely can't be read instead of a raw stack trace.

### First-run setup guide
New users now get a **guided tour** on first launch (no API key + no sessions): choose an AI provider, add your HuggingFace token, pick audio devices, record your first meeting, and discover the optional power features. Re-openable anytime from **Help → Launch setup guide**. Existing users never see it.

### Today / Brief polish (from earlier 2.10.x work, included)
- Today daily-briefing tab is opt-in (Settings) and persistent.
- Pre-meeting Brief modal has an "Add context" box at the top.
- Auto-record skip-pattern matches now show "Skipped (pattern)" on meeting tiles.
- Help guide includes the full M365 Copilot briefing prompt + scheduled-prompt setup steps.

---

## What didn't change
- All v2.9.x recording-safety layers remain active.
- Existing sessions / settings / config preserved on upgrade.
- Every new capability (Today, Live Co-Pilot, auto prep-brief, terminology editing) is opt-in or seeded with safe defaults.

---

## Upgrade notes
- **Windows** — `Meeting Recorder_2.10.2_x64-setup.exe`. Installs over your existing version.
- **macOS** — see the install block at the top.
- **No new dependencies**, no bootstrap changes.
- If you had the Today tab on in 2.10.0, it's been opt-in since 2.10.1 — re-enable it in Settings if you want it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
