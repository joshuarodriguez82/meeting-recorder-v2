# Meeting Recorder v2.9.0

The Live Co-Pilot release. Five layered changes that turn coaching from "vanilla bullets" into a real in-call assistant: editable persona libraries, meeting-type modifiers, actionable suggestions, a faster hot-tick tier, and a live cost estimator so you can dial the call rate against what it actually costs.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged. Here's how to get past it.
>
> We ship the Mac app as a **ditto-zipped `.app`** (`Meeting.Recorder_2.9.0_universal.zip`), not a `.dmg`. Tauri's `bundle_dmg.sh` is chronically broken on the macOS GitHub Actions runner — its AppleScript-against-Finder layout step gives up silently in headless CI. The workflow builds with `--bundles app` and `ditto`-zips the bundle. Apple recommends `ditto` for un-notarized distribution because it preserves extended attributes and (future) code signatures.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.9.0_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
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

## The headline change — modes + meeting types

The co-pilot is no longer one fixed prompt. It now composes three editable layers at runtime:

**Mode (persona)** — 3 defaults, fully editable from Settings, each is a pure role/care-about framing:
- **SA** — Amazon Connect / CCaaS migration lens, IVR + integration risks, vendor-lock-in flags
- **Sales** — BANT/MEDDIC gaps, deal-velocity actions, objection signals
- **Executive** — outcomes over mechanics, strategic risk, exec-altitude follow-ups

**Meeting type (modifier)** — 7 defaults, also editable:
- General, Discovery, SOW Review, Status Sync, Technical Deep-Dive, Customer Demo, Internal Working Session

Pick one of each from the dropdowns in the co-pilot panel header at recording start (or mid-meeting). Sales + SOW Review composes deal-velocity persona with redline-catching guidance, automatically. **3 × 7 = 21 combinations from 10 editable prompts.** Add your own modes (Engineering Lead, Customer Success) or types (Post-Mortem, QBR) from Settings; reset any default back to the shipped prompt with one click.

The JSON output schema is appended by the system after your prompt — editing modes or meeting types can't break the wire format.

---

## Actionable tick bullets

Every co-pilot suggestion now has a hover-revealed **Save** menu with three targets:

- **As follow-up** → appends to the session's `action_items` markdown (shows up in Follow-ups tab post-process)
- **As decision** → appends to `decisions` (shows up in Decisions tab)
- **To my notes** → appends to session notes

Section-defaulted (clarifying questions default to follow-up, risks to decision) with three-way override per bullet. Idempotent on exact text — clicking save twice doesn't duplicate. New `POST /recording/copilot/save` endpoint handles the append.

Plus the Copy buttons that landed in v2.8 still work — three levels: whole tick / one section / one bullet.

---

## Sliding-window cadence — hot ticks for time-sensitive coaching

The 45-second wide tick has been the only cadence. v2.9 adds a second tier:

- **Wide tick** — full ~10 minute context window every N seconds (default 45). Same coaching as before.
- **Hot tick** — only the last ~90 seconds, with a prompt explicitly biased toward EMPTY responses. Fires only when something time-sensitive is happening RIGHT NOW. Default off (interval = 0).

`POST /recording/copilot/hot-tick` uses the same mode + meeting-type composition + cross-tick memory, just with a tighter prompt + cheaper budget (max_tokens=256, timeout=10s vs 512/20s for wide).

When both tiers run, the panel polls both endpoints in parallel. Hot results appear in the same history list.

---

## Adjustable intervals + live cost estimator

New Settings card — **Co-Pilot Cadence**:

- Wide tick interval input (15-300s, default 45)
- Hot tick interval input (0-60s, default 0 = disabled)
- **Live cost estimate** that recalculates as you touch either input or switch provider/model
- Comparison rows for Anthropic Haiku/Sonnet/Opus, OpenAI GPT-4o/mini, Ollama (local = $0), OpenRouter free tier (with cap caveat)

So if you crank the hot tick to 15s on Haiku, you see "$0.31/hr" inline before committing. Flip to Ollama and it's "$0/hr". The maths is in `src/lib/copilot-cost.ts` — token estimates × per-1k-token rates × calls-per-hour.

---

## Tick memory — no more echo-chamber coaching

Each tick now sees the previous tick's outputs and is explicitly told **don't repeat — build on these**. Kills the pattern where every 45 seconds produced the same three bullets reworded. Notes what's evolved, what's been answered, what's gone unaddressed.

---

## Persistence + active-mode setter

- New `live_copilot_mode` and `live_copilot_meeting_type` settings persist to config.env
- New `live_copilot_wide_interval_sec` and `live_copilot_hot_interval_sec` settings persist
- New `POST /settings/copilot-active` lightweight endpoint flips just mode/type without rebuilding RecordingService — works mid-recording without orphaning capture threads

---

## What didn't change

- Existing co-pilot panel layout, refresh button, pause/resume, tick scroll history — same UX, same keyboard behavior
- Existing `POST /recording/copilot/tick` endpoint — unchanged shape, just composes prompts differently internally
- Existing post-meeting summary integration (ticks feed the summarizer) — unchanged
- Custom coaching context (per-engagement free-text from v2.8) — unchanged, now layers on top of the mode + meeting-type composition

---

## Coming in v3.0

Design work in flight (not in this release): full visual overhaul, Cmd+K command palette, refined typography and color system. Functionality stays untouched; what changes is how it *feels*.

---

## Upgrade notes

- **macOS** — see the install block at the top.
- **Windows** — `Meeting Recorder_2.9.0_x64-setup.exe` (or `.msi`). Installs over your existing version; `%LOCALAPPDATA%\MeetingRecorder\` data (recordings, config, settings) is preserved.
- **First launch after upgrade** should be fast — v2.8 already shipped the `--only-binary` bootstrap fix, so no source-build dependency surprises this time.
- **First time on v2.9?** Open Settings → Live Co-Pilot section to see the new Modes card, Meeting Types card, and Cadence card. Defaults are SA + General + 45s wide / hot off.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
