# v2.15.0 — Outlook + Teams Web sync on personal machines, daily brief that actually works

> **What this release adds:**
>
> 1. **Today tab's daily briefing auto-fills from your real Outlook calendar
>    and Teams Activity feed.** No more pasting Copilot's scheduled-prompt
>    output. Works on personal machines even when IT has blocked Graph
>    / calendar API access — the recorder uses your existing
>    `outlook.office.com` / `teams.microsoft.com` browser session as the
>    data path.
> 2. **Two new buttons on the Today tab:** **Sign in to Microsoft** (one-time
>    interactive sign-in covering both OWA and Teams in a single Chrome
>    window) and **Sync now** (publishes the brief).
> 3. **"Signed in" toast no longer sticks on screen.** Bug from initial
>    cut of this feature where the success toast inherited an Infinity
>    duration. Fixed.

This is **the rebuilt v2.15.0** — the original release shipped 2026-06-29
along with v2.15.1 and v2.15.2 dot-releases all had subtle scraper
bugs that made the brief either silently empty or hit auth errors.
Those three releases are deleted; this one consolidates the fixes
and ships a path that actually produces a populated brief against
real M365 accounts on personal machines.

## Install (macOS)

> v2.15.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.15.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.15.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.15.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## How it works

### The data path

When you click **Sign in to Microsoft**, the recorder launches **your
installed Chrome** (`channel='chrome'`, no bundled Chromium —
installer stays lean) with a persistent profile at
`%LOCALAPPDATA%\MeetingRecorder\web-session\` (Windows) /
`~/Library/Application Support/MeetingRecorder/web-session/` (Mac).
Two tabs open: `outlook.office.com/calendar/view/day` and
`teams.microsoft.com/v2/?clientType=desktop#/activity`. Authenticate
in both tabs (your normal M365 sign-in + MFA tap), close the window.
Cookies and tokens persist in the profile.

When you click **Sync now**, the recorder reopens that same Chrome
profile in the background (off-screen, minimized — see below), navigates
to OWA's day view + Teams' Activity feed, extracts the visible text,
joins it with your recorder's open action items, sends the blob to the
LLM via the same parser the manual Import flow uses, and stores the
result in `DailyBriefingService`. The Today tab renders calendar
events, action items needing response, and FYI sections.

### Why a hidden headed browser (and not headless)

Microsoft **actively detects** headless Chromium on `outlook.office.com`
and `teams.microsoft.com` — they serve a stripped/empty UI even when
your session cookies are perfectly valid. The first cut of this
feature used `headless=True` and that's why those releases shipped
empty briefs.

v2.15.0 launches the SAME Chrome you use, with no `--headless` flag at
all. Real Chrome → Microsoft renders the full app → we extract real
content. To keep the UX clean, the Chrome window is launched with:

- `--window-position=-32000,-32000` — Windows considers this off-screen
  on every standard monitor setup.
- `--window-size=1280,1024` — ensure OWA's responsive layout renders
  the desktop view, not the mobile view.
- `--start-minimized` — belt and suspenders; worst case, the user sees
  a taskbar icon briefly.

For the ~10-15 seconds a Sync takes, you may see a taskbar icon flash.
That's the price of bypassing Microsoft's bot detection. The window
itself is never visible on your monitor.

### Profile location matters

The persistent profile **must live on a local-only path**. v2.14.0
(predecessor of this work) put it under `recordings_dir/web-session/`
— which for users with `recordings_dir` on Google Drive Stream /
OneDrive caused cloud-sync filter drivers to corrupt Chrome's cookie
store between the headed sign-in and the subsequent scrape. v2.15.0
pins the profile to `USER_DATA_DIR` (same per-machine local path
speaker profiles and the auto-record blocklist use).

### Content extraction is content-aware

The scrape doesn't rely on a fixed timeout or `wait_for_load_state`
to know when OWA's React tree has finished mounting. Instead it polls
`[role="main"]`'s `inner_text` every second with three exit conditions:

1. Text crosses 1500 chars (rich content present → return immediately).
2. Text stops growing for 3 polls in a row (page has settled → return).
3. 30-second hard cap (give up, return whatever we have, log a warning).

For empty calendar days, the inner-text settles around 700-1000 chars
(the time-of-day axis without events) and we publish a brief noting
no events. For a populated day, the text reaches the target in 5-10
seconds and we extract the actual event details.

If the extracted text is below 500 chars after the full 30-second
wait, Sync returns a distinct error rather than silently publishing
an empty brief — that floor catches "Microsoft fingerprinted us as a
bot and served a stub" cases so you see a meaningful error toast,
not an empty Today tab.

### Teams Activity scrape is non-fatal

Teams Web has its own OAuth dance on top of M365 SSO (even with OWA
fully authenticated, the first Teams visit bounces through
`login.microsoftonline.com` to mint a Teams-specific access token).
If Teams' scrape fails for any reason — auth dance not completed in
the sign-in window, DOM change Microsoft hasn't documented, etc. —
the brief still publishes with just OWA + your action items. Teams
failure leaves a clear warning in `backend.log` for diagnosis.

### Diagnostics

Every scrape logs its exit path so future debugging is easy:

```
[INFO] OWA: content reached target (4823 chars) in 6.2s
[INFO] Teams: content stable at 942 chars (above useful floor) after 8.3s
[WARNING] OWA: content stable at 392 chars after 5.1s — below useful floor (700). SPA may not have finished mounting; brief may be incomplete.
[WARNING] Teams needs an interactive sign-in (OWA was fine); OWA-only brief.
```

## What you do after install

1. Click **Sign in to Microsoft** on the Today tab. A Chrome window opens
   with two tabs (OWA + Teams).
2. Sign in / re-MFA in tab 1 (OWA). Calendar should appear.
3. Switch to tab 2 (Teams). Complete any "Stay signed in?" / consent
   prompts. Activity feed should appear.
4. Close the Chrome window.
5. Click **Sync now**. Wait ~10-15 seconds. Today tab populates.

The sign-in profile typically holds for ~7 days; on the next
auth-expired event, the banner re-prompts and you do steps 1-4 again
(weekly cadence given typical M365 conditional-access policies).

## Backend additions

- New service `services/outlook_web_scraper.py` — Playwright wrapper
  using your installed Chrome, hidden via off-screen + minimized window.
  Persistent profile at `USER_DATA_DIR/web-session`. Lazy playwright
  import so the rest of the backend keeps working if it's not installed
  yet.
- New endpoints `POST /briefing/signin` and `POST /briefing/sync`.
  Serialized via an asyncio.Lock so simultaneous clicks don't race on
  the Chrome profile-dir lock.
- New constant set: `OWA_TARGET_CHARS=1500`, `OWA_MIN_USEFUL_CHARS=700`,
  `TEAMS_TARGET_CHARS=1500`, `TEAMS_MIN_USEFUL_CHARS=500`,
  `MIN_SCRAPE_FLOOR=500`, `CONTENT_SETTLE_MAX_WAIT_SEC=30`,
  `TEAMS_CONTENT_SETTLE_MAX_WAIT_SEC=35`, `STABILITY_POLLS=3`.
- New dependency: `playwright>=1.40` (no bundled Chromium —
  `channel='chrome'` uses your installed Chrome).

## Frontend additions

- Two new buttons on Today tab: **Sync now** and **Sign in to Microsoft**.
- Auth-expired amber banner with retry, surfaced on 423 LOCKED responses.
- "Signed in" success toast now dismisses after 5 seconds (was inheriting
  `Infinity` duration from the loading toast in early cuts).

## Tests

**72 backend tests** (was 45 in v2.14.0). New coverage:

- `test_outlook_web_scraper.py` — profile-dir contract, login-URL
  detection, formatter for OWA + Teams + action-items integration,
  lazy-import contract, and the wait-for-text-to-settle helper across
  all three exit conditions plus the v2.15.0 bug case
  (below-useful-floor stable text).

Live verification — does the scrape actually work against a real M365
tenant — happens on the user's machine after install. Browser-driven
tests aren't possible in CI.

## Bundle changes

None. Playwright pulls itself in via the first-launch venv bootstrap.
Adds ~10-30 seconds to first launch after upgrading.

## What this consolidates

This release supersedes (and the team has deleted) the
previously-shipped `v2.15.0`, `v2.15.1`, and `v2.15.2` releases. Each
of those had a real bug in the scrape path:

- **Original v2.15.0** (deleted): persistent profile lived under
  `recordings_dir` which broke on cloud-synced volumes; Teams scrape
  was fatal on its OAuth dance.
- **v2.15.1** (deleted): made Teams non-fatal, added two-tab sign-in
  window — but kept `headless=True` so OWA scrape silently returned
  392 chars of menu chrome instead of the calendar.
- **v2.15.2** (deleted): better content-settle wait + clearer error
  message — but still `headless=True` so the underlying bot detection
  still served the stripped UI.

This v2.15.0 retains the good parts from all three (profile location
fix, content-settle wait, two-tab sign-in, non-fatal Teams, fixed
toast) AND adds the actual fix: headed Chrome with the window
positioned off-screen.

## Known not yet patched

- **Teams Chat tab** — Activity-only for now.
- **Auto-sync on first launch of day** — Sync is still manual.
- **Subprocess-isolated transcribe + diarize** — deferred from v2.12.0.
- **RecordingService decomposition** — 1,300-line god-object remains.
- **Subprocess timeout on finalize child** — small fix tracked.
- **macOS Bluetooth audio-format-mismatch banner** — Windows-only today.
