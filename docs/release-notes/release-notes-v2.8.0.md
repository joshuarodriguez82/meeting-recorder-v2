# Meeting Recorder v2.8.0

A polish + features release across the entire app. Most of this comes from a focused round of dogfooding — the things that were quietly annoying every day are now fixed, and the Live Co-Pilot got a real prompt rewrite so its suggestions are SA-flavored instead of generic.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged. Here's how to get past it.
>
> We ship the Mac app as a **ditto-zipped `.app`** (`Meeting.Recorder_2.8.0_universal.zip`), not a `.dmg`. Tauri's `bundle_dmg.sh` is chronically broken on the macOS GitHub Actions runner — its AppleScript-against-Finder layout step gives up silently in headless CI. The workflow builds with `--bundles app` and `ditto`-zips the bundle. Apple recommends `ditto` for un-notarized distribution because it preserves extended attributes and (future) code signatures.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.8.0_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
> 2. Drag `Meeting Recorder.app` into `/Applications`.
> 3. Double-click it. Dismiss the "damaged" warning.
> 4. Open **System Settings → Privacy & Security**, scroll to the bottom, click **Open Anyway** next to the Meeting Recorder entry.
> 5. Double-click the app again, then click **Open**.
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
> Windows users can ignore this whole block — just download the `.msi` or `.exe` and install normally.

---

## The headline change — Live Co-Pilot actually feels SA-built

Before: generic "what to ask / what to flag" template that produced vanilla coaching, especially on Ollama.

Now:
- **Default prompt rewritten** to frame as a TTEC Digital SA on Amazon Connect / CCaaS migration work. Risk categories are explicit (scope creep, integration boundary, vendor lock-in, IVR/flow gotchas, IAM trust chains, hidden licensing) instead of "general risks." Should noticeably tighten Haiku and dramatically lift Ollama, which previously had no role grounding.
- **Settings → Coaching context** — paste anything the model can't infer ("currently focused on Genesys → Connect migration for a healthcare client, PHI compliance is everything, watch Salesforce screen-pop scope creep"). Appended to every co-pilot tick as authoritative framing. Persists across recordings. Empty by default.
- **Tick memory** — each tick sees the previous tick's output and is explicitly told not to repeat. No more echo-chamber where every 45 seconds produces the same three bullets reworded.
- **Copy buttons at three levels** — whole tick (header), one section, or a single bullet. Bullet-level appears on hover so the panel stays clean.
- **Refresh-button feedback** — manual Refresh now surfaces "No new coaching content since the last tick" instead of silently doing nothing. 429 rate-limit errors are called out explicitly.
- **Ticks feed the post-meeting summary** — when you process the session, the deduplicated co-pilot observations are passed alongside the transcript as a second pass. The summarizer is told to corroborate against the transcript, prefer the co-pilot's phrasing when clearer, and never invent details.

---

## Auto-stop silence — actually fires now

Two stacked bugs:
1. **Loopback threshold was too sensitive.** Post-meeting system audio (Teams keepalive hiss, codec noise) was tripping the "someone is talking" check, so the silence timer never accumulated and auto-stop never fired. Loopback floor raised to `DUCK_LEVEL_THRESHOLD` (−34 dBFS); mic floor unchanged so whispered speech still counts.
2. **Watchdog logged nothing.** No way to diagnose without guessing. Now emits `watchdog: silence_s=… last_speech_age=… loopback_ema=… should_stop=…` once a minute while recording. Greppable in `backend.log`. The diagnostic-log rate limiter resets on every `start_recording` so the first tick of every new recording always emits.

Caveat for users with `silence_stop_min > 0`: on calls where the far-end participants are very quiet AND you're muted, the higher loopback threshold could trigger an early auto-stop. Bump the value if it bites.

---

## Three lists now feel like one app

Follow-ups, Commitments, and Decisions all use the same split-pane pattern: row list on the left, full detail + status dropdown on the right. Previously Commitments and Follow-ups had their own UI shapes; now they match Decisions.

Plus:
- **Sticky detail panel** — when you click a row halfway down the list, the detail stays on screen as you scroll. Previously you had to scroll back up to read what you just selected.
- **Follow-ups speaker resolution** — items that the LLM tagged as `SPEAKER_03` now resolve to the renamed display name (`Charles`) when the session has known speakers. Both lists and detail.

---

## Copy buttons across the session detail dialog

Every text-bearing tab in the session detail now has a Copy button:

- Summary, Actions, Decisions, Requirements — Copy button in the corner of the markdown block
- Transcript — Copy button at the top, copies a plain-text version with resolved speaker names and `[MM:SS]` timestamps so it pastes cleanly into notes

Plain `navigator.clipboard` with a textarea+execCommand fallback for any WebView that blocks the primary path.

---

## Prep Brief & Meeting Brief — feed extra context

Both prep-brief flows (manual subject + calendar-tile) now have an **"Additional context for the AI"** textarea. Paste exec asks, recent emails, customer mood, redlines just received, anything the meeting history can't see. Treated as authoritative — the model weaves it into the brief instead of paraphrasing it.

The meeting-brief modal generates the first brief one-click as before, then exposes the textarea + a "Regenerate with context" button below the brief so iteration is a second click.

Prep brief also gets a **meeting picker** at the top: pick an upcoming calendar event and the Subject + Client + Project auto-fill from any matching prior session.

---

## Auto-record blocklist patterns

Old: block specific recurring meetings by exact title.
New: **add substring patterns** that skip any meeting whose title contains them. Most useful for Outlook's "Canceled: …" prefix — one pattern catches every cancelled meeting from any organizer.

New Settings card **"Auto-record skip patterns"** under Calendar. Per-meeting exact blocks still live on the Record view's *No auto* toggle (unchanged).

---

## Engagement auto-roll

The engagement register now surfaces three things the per-session view can't:

- **Last meeting** — when this account was last touched
- **Since** — first meeting on record
- **Outstanding commitments** — count of awaiting/overdue items, rolled up from the commitments tracker

Auto-rolled from your existing recordings — no manual entry. Per the v2.8 plan, file-import-driven engagement context is deferred.

---

## Search vs Ask — explainer banners

Both views now open with a one-line callout explaining the difference: Search returns chunks, Ask synthesizes an answer with citations. Same underlying retrieval; different output.

---

## Live transcript persists across tab switches

Previously, leaving the Record tab mid-meeting wiped the live transcript when you came back. Now the panel rehydrates from a backend snapshot of every segment captured during the current recording — appending continues from there.

---

## Bootstrap UX — no more scary terminal windows

`--only-binary=:all:` on pip means it can never source-build a package. The original "maturin window pops up for 30 minutes" scenario happened when pip fell back to source compilation for a missing wheel; that path is now forbidden. If a wheel is genuinely missing for some future dependency, pip fails fast with a readable "no matching distribution found" message instead of silently invoking a compiler.

The bootstrap also now re-runs on every launch (not just first install) so dependency upgrades in new releases actually land on existing installs. Combined with the no-source-build flag, this is fast (~5 sec when everything is satisfied).

---

## Security hygiene

`.gitignore` hardened to catch `*.env` and `config.env` at the repo root after a near-miss with a real Anthropic key in user appdata. The runtime config.env at `%LOCALAPPDATA%\MeetingRecorder\` already lives outside this repo — this is defense in depth against a misplaced copy.

---

## Usage guide updated

Reflects the Live Co-Pilot, the Ollama walkthrough, the post-meeting vs live AI model separation, and the new behaviors above.

---

## Known issues / deferred

- **Engagement status editing** — manual fields beyond auto-roll. Going to v2.8.x.
- **Insights drill-down** — clickable recurring topics. Going to v2.8.x.
- **Splash screen during bootstrap** — wasn't necessary once `--only-binary` made bootstrap fast.
- **Engagement-grounded co-pilot ticks** — feeding the open-commitments register into every tick context. Designed but deferred to v2.9 along with a fuller co-pilot rework.

---

## Upgrade notes

- **macOS** — see the install block at the top.
- **Windows** — `Meeting Recorder_2.8.0_x64-setup.exe` (or `.msi`). Installs over your existing version; `%LOCALAPPDATA%\MeetingRecorder\` data (recordings, config, settings) is preserved.
- **First launch after upgrade** may take an extra ~30 sec while the bootstrap pip walks the dep graph. Subsequent launches are fast again.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
