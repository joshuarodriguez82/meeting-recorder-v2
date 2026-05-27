# Meeting Recorder v2.9.2

Two deferred-from-v2.8 features land in one small, focused release: clickable Insights topics + manual fields for engagements that the auto-roll can't see. No co-pilot changes, no design overhaul — that's v3.0's job.

(Why v2.9.2 and not v2.9.1: v2.9.1 is reserved for any smoke-test bug-fix that comes out of v2.9.0 install testing. This release is pure feature work, so it skips ahead.)

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged. Here's how to get past it.
>
> We ship the Mac app as a **ditto-zipped `.app`** (`Meeting.Recorder_2.9.2_universal.zip`), not a `.dmg`. Tauri's `bundle_dmg.sh` is chronically broken on the macOS GitHub Actions runner — its AppleScript-against-Finder layout step gives up silently in headless CI. The workflow builds with `--bundles app` and `ditto`-zips the bundle. Apple recommends `ditto` for un-notarized distribution because it preserves extended attributes and (future) code signatures.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.9.2_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
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

## Insights — recurring topics are clickable

Click any recurring-topic bubble in the Insights tab. A modal opens listing every session whose summary mentions that phrase, scoped to the topic's client bucket. Each row is itself clickable — opens the session detail dialog.

**Why this matters:** previously the topic cloud was a wall of words with counts and nothing to do with them. Now the click answers the obvious next question — *which* meetings was this raised in?

Implementation notes:
- No new backend endpoint. Filtering happens client-side from the existing sessions list. The full list caps at a few hundred entries for even heavy users; the filter runs in <10ms.
- Topic matching is case-insensitive substring against the summary text. Click "Salesforce screen-pop" → finds every session whose summary mentions Salesforce screen-pop.
- Lazy load — sessions only fetched on first modal open, not on Insights mount.

---

## Engagements — manual status, sponsor, notes

The auto-roll handles "what does the system know" — last meeting date, open commitments rolled up across calls, decisions made. v2.9.2 adds the other half: **what does the SA know** that no meeting ever explicitly stated?

Each engagement gets an "Engagement details" card with:

- **Status** — dropdown of canonical values (`active`, `on-hold`, `at-risk`, `won`, `lost`, `archived`) with color-coded badges. Free text accepted too, so a status like "renewal-discussion" works fine and just renders with the neutral outline variant.
- **Exec sponsor** — free text. Customer-side decision-maker, person to call when things move.
- **Next milestone** — free text. "SOW signature target 2026-06-15" or "Pilot kickoff Tuesday".
- **Notes** — free-form. Anything the auto-rolled register can't see: exec asks, political dynamics, commercial context, redlines just received.

Read mode shows whatever's set; click **Edit** (or **Add details** when empty) to open the inline form. All four fields persist to `recordings_dir/engagement_overlays.json` keyed by `client__project`. Editing here cannot touch any meeting record — the overlay is stored separately.

New endpoints:
- `PUT /engagements/{client}/overlay` — sets the overlay (project optional in body)
- `GET /engagements/known-statuses` — canonical status list, keeps frontend dropdown in sync
- Existing `GET /engagements/{client}/register` now merges the overlay into the response

---

## What didn't change

- Live Co-Pilot, modes, meeting types, cadence — all v2.9.0 behavior carries forward unchanged
- All four split-pane views (Follow-ups, Commitments, Decisions, plus the auto-rolled engagement register sections) — unchanged
- Settings, recording flow, calendar integration — all unchanged
- No design overhaul yet. The v2.8.0-era look is intact. The v3.0 visual rework is queued as its own release.

---

## What's queued

- **v2.9.1** (reactive) — bug fixes from v2.9.0 install testing if any surface
- **v2.9.3** — co-pilot prompt tuning based on real-world usage
- **v2.9.4** — design tokens scaffold (silent prep for v3.0 — defines the CSS variables in globals.css but doesn't apply them yet)
- **v3.0** — the visual moment. Color, typography, motion, Cmd+K command palette. The spec is locked in `design/v3.0-system.md`.

---

## Upgrade notes

- **macOS** — see the install block at the top.
- **Windows** — `Meeting Recorder_2.9.2_x64-setup.exe` (or `.msi`). Installs over your existing version; `%LOCALAPPDATA%\MeetingRecorder\` data (recordings, config, settings, engagement overlays) is preserved.
- **No new dependencies.** The bootstrap step from v2.9 already covered everything; first launch after upgrade should be fast.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
