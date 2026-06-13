# v2.5.0 — Calendar-driven auto-record + Insights dashboard

Two big additions in this release: a **persistent auto-record toggle**
on the Record view that watches your calendar and starts recordings
automatically at each meeting's scheduled start time, and the
**Insights** tab — a cross-meeting trend dashboard for time
allocation, recurring topics, and stale open loops.

## Install (macOS)

> v2.5.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.5.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.5.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.5.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Auto-record from calendar

A new **Auto-record** switch lives in the Upcoming Meetings card
header on the Record view. Flip it on once and forget it.

- **Trigger:** a 30-second backend loop watches your connected
  calendar. At each event's scheduled start time, recording begins
  with the meeting's name, attendees, and scheduled end already
  filled in — so the existing silence + overrun watchdog handles
  the stop side for free.
- **Filters:** skips all-day events and only fires for events that
  include a conference link (Teams, Zoom, Google Meet, Webex,
  GoToMeeting, BlueJeans, Whereby). The link is detected anywhere
  in the event's location or subject.
- **Manual wins:** if you're already recording (manually or auto)
  when a scheduled meeting opens, the auto-start is suppressed and
  the event is marked handled so it won't pounce the moment you hit
  Stop.
- **Persistent across restarts:** state is saved to `config.env`
  alongside the other settings. If the toggle was on when you quit,
  it's on the next time you launch.
- **Status hint:** when the toggle is on, the card shows
  "Auto-record on — next: \<subject\> at \<time\>" so you can
  confirm at a glance which meeting will fire next.

The auto-stop side intentionally piggybacks on the watchdog rather
than stopping exactly at the scheduled end — meetings that run long
keep recording until silence settles in. Tune **Settings → Watchdog**
if you want a stricter cutoff.

## Insights dashboard

The new **Insights** tab is a live trend rollup over your entire
session library. Pick a date window (or leave it open) and optionally
scope to one client; three panels render in under a second:

- **Time allocation** — recorded hours bucketed by client and by
  summary template, so you can see where the meeting time actually
  goes.
- **Recurring topics** — phrases that keep coming up in summaries,
  grouped per client. Useful for spotting threads that aren't
  closing out.
- **Open loops** — follow-ups and decisions still unchecked past a
  staleness threshold (30 days by default). Pulled from the same
  per-item state used in the Follow-Ups / Decisions tabs, so
  checking an item there removes it from Insights.

Everything is computed on demand from session JSONs already on disk
— no separate index, no separate database. Open the tab and it
runs.

## Bug fixes

- **Insights window comparison crash.** When the date filter sent
  a UTC `Z`-suffixed ISO string from the frontend, the backend
  parsed it as tz-aware and then crashed comparing against the
  naive `started_at` stored in session JSONs (`TypeError: can't
  compare offset-naive and offset-aware datetimes`, surfacing as a
  500 + CORS error in the browser). Both sides are now normalized
  to naive local time before comparison.

## Backend additions for integrators

- New `GET /recording/auto-status` — returns
  `{enabled, running, next_event}` for the toggle UI.
- `SettingsDTO` gains `auto_record_enabled: bool` (persisted as
  `AUTO_RECORD_ENABLED` in `config.env`).
- New `services/auto_record_service.py` — single asyncio loop, no
  cross-cutting deps; can be unit-tested by mocking the four
  constructor callbacks (`get_upcoming_meetings`, `is_recording`,
  `start_recording`, `is_enabled`).

## Upgrade notes

No migration steps. Existing `config.env` files load unchanged;
`AUTO_RECORD_ENABLED` defaults to `false` so the toggle starts off
until you opt in.
