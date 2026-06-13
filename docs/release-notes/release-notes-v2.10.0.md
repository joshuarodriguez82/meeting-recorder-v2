# Meeting Recorder v2.10.0

New "Today" tab — your morning briefing as a structured dashboard, populated by your existing Microsoft 365 Copilot scheduled prompt.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.10.0_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
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

## Why this release

You already run a Microsoft 365 Copilot scheduled prompt every morning that gives you a great daily briefing — top priority, today's agenda, items needing a response, FYI. The problem: it lives inside Copilot. There's no API surface to pull scheduled-prompt output from outside the Copilot UI, so it stays trapped on the M365 side of your day.

v2.10 brings it across with a one-step copy-paste, then renders it as an interactive Today view you can act on. Action items become checkboxes you tick off as the day moves. Re-importing later (e.g. after Copilot re-runs the prompt) preserves what you've already checked.

---

## What's new

### Today tab — default landing view

Opens automatically when you launch the app. Sits at the top of the sidebar above Record.

Layout:

- **Greeting header** — date, time, "Briefing imported at 7:14 AM" status when present.
- **Top priority** — gradient hero card with the single most important thing today, why it matters, what to do.
- **Right Now** — live recording status when you're mid-meeting (with a jump-to-Record button) or the next agenda item when you're idle.
- **Needs your response** — checkbox cards for action items the briefing flagged (Mike Gooch's reply, Guardian ACGR follow-up, etc). Tick them as you go; they're persisted across reloads.
- **Today's agenda** — timeline cards with time / duration / meeting-type color tag (discovery / SOW / status / technical / demo / internal) and role badge (host / attendee / optional). Cancelled meetings stay visible with strike-through so you see the schedule change.
- **Schedule notes** + **FYI** — two-column footer for soft context: schedule heads-ups (conflicts, packed afternoons) and the market/client/internal/personal items that don't need action but you'd want to glance at.

### Import flow

Manual but cheap:

1. Run your scheduled prompt in M365 Copilot, copy the output.
2. Click **Import briefing** on the Today tab.
3. The paste dialog opens and auto-pulls your clipboard. If the right text is already there, just hit **Parse and import**.
4. Claude reshapes the free-form briefing into the structured Today view.
5. Check off action items as you complete them. Re-importing the same day merges by title so your check-state survives.

One file per calendar date stored at `<recordings_dir>/briefings/YYYY-MM-DD.json`. History is preserved.

### What didn't change

- All v2.9.x defense layers — orphan-kill, parent-PID deadman switch, watchdog timer, capture-stall detector, WAV integrity check, ghost-session audit. Still active.
- Recording capture, transcript, summarization, live co-pilot — unchanged.
- Sessions / Follow-Ups / Commitments / Decisions / Search / Ask / Clients / Engagements / Insights / Prep Brief / Settings — unchanged.

---

## Upgrade notes

- **Windows** — `Meeting Recorder_2.10.0_x64-setup.exe`. Installs over your existing version; sessions / settings / config preserved.
- **macOS** — see the install block at the top.
- **No new dependencies**, no bootstrap changes.
- **First launch after upgrade**: app opens on the Today tab. Empty state until you import your first briefing — click Import briefing to wire it up.
- **API cost**: the import calls Claude once per parse (one Haiku/Sonnet message, ~2k token max). Trivial — pennies a month at daily import cadence.

---

## Under the hood

- **Backend** — new `DailyBriefingService` (atomic per-date JSON writes), `parse_daily_briefing()` on `Summarizer` (structured JSON output via Claude), three new endpoints (`POST /briefing/import`, `GET /briefing/today`, `PATCH /briefing/{date}/actions/{id}`).
- **Frontend** — new `TodayView` component, `Today` nav item, default-landing wiring, briefing types in `api.ts`.
- **Persistence** — done-state lives inside the briefing JSON (per-action `done_at` ISO timestamp). Re-import preserves it by matching action titles case-insensitively.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
