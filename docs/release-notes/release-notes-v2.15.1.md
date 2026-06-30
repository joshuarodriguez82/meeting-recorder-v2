# v2.15.1 — Teams auth dance is non-fatal, sign-in window covers both surfaces

> **What this release fixes:**
>
> 1. **Sync no longer returns 423 when Teams needs a separate sign-in.**
>    v2.15.0 propagated Teams' `OutlookAuthExpired` as a 423 response,
>    killing the whole sync even when OWA had succeeded. Teams' auth
>    state is now non-fatal — Sync publishes an OWA-only brief and
>    logs a warning so the cause is visible in `backend.log`.
> 2. **The Sign in to Microsoft window now opens BOTH OWA and Teams**
>    so the one-time Teams OAuth dance happens during interactive
>    sign-in, not silently during sync. After clicking Sign in, you'll
>    see two tabs in the Chrome window — finish auth on both, close
>    the window, and the next Sync covers both surfaces.

This is a tight follow-up to v2.15.0. No new features, no data
changes — just shipping the obvious "Teams shouldn't tank OWA" guard
that should have been in v2.15.0 and the sign-in-window UX to surface
Teams' separate auth requirement.

## Install (macOS)

> v2.15.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.15.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.15.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.15.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's new

### 1. Teams' auth-expired is no longer fatal (the headline)

Field repro 2026-06-29: signed in successfully via the v2.15.0 sign-in
window. Clicked **Sync now**. `backend.log` showed:

```
[INFO] Scraping OWA day view (profile=C:\\Users\\<you>\AppData\Local\MeetingRecorder\web-session)
[INFO] Scraping Teams activity (profile=...)
[access] POST /briefing/sync HTTP/1.1 423 Locked
```

OWA scraped fine; **Teams** is what raised `OutlookAuthExpired`.

**Why.** Teams Web has its own OAuth dance on top of M365 SSO. Even
when OWA's cookies are valid, the first visit to
`teams.microsoft.com/v2/?clientType=desktop#/activity` bounces through
`login.microsoftonline.com` to issue a Teams-specific access token,
and may render "Stay signed in?" / consent interstitials. Our
scraper saw `login.microsoftonline.com` in the URL and raised
`OutlookAuthExpired`. v2.15.0's server propagated it as 423 — wrong
assumption that "stale for Teams = stale for OWA."

**Fix in v2.15.0's `/briefing/sync` handler:**

```python
# Before (v2.15.0):
except OutlookAuthExpired as e:
    raise HTTPException(status_code=423, detail=str(e))   # kills brief

# After (v2.15.1):
except OutlookAuthExpired as e:
    logger.warning("Teams needs an interactive sign-in (OWA was fine); "
                   "OWA-only brief. ...")
    teams_text = ""    # silent, OWA brief still publishes
```

Net effect: when Teams' first-ever visit through the headless scrape
hits its OAuth dance, the brief still publishes with calendar + open
action items. The Teams section is just missing until you complete
the dance interactively (item 2 below).

### 2. Sign-in window now opens TWO tabs (OWA + Teams)

The cleanest way to handle Teams' separate auth dance is to surface
it at sign-in time, not silently at sync time. v2.15.1's
`open_signin_window` opens TWO tabs in the same Chrome window:

- **Tab 1** — `outlook.office.com/calendar/view/day` (your normal
  OWA sign-in flow; same as before).
- **Tab 2** — `teams.microsoft.com/v2/?clientType=desktop#/activity`
  (new; surfaces Teams' OAuth dance + any consent prompts).

The persistent profile shares cookies across tabs, so any token
Teams mints lands in the same profile the headless scrape will read
later. **After a one-time interactive sign-in covering both tabs,
headless Teams scrapes work and the daily brief shows both surfaces.**

If the Teams tab fails to open (very rare — maybe a transient Teams
outage), the OWA tab still works and sign-in still completes;
backend.log notes the failure so future diagnosis is easy.

## Backend changes

- `server.py:/briefing/sync` — Teams' `OutlookAuthExpired`,
  `OutlookScraperUnavailable`, and `OutlookScraperError` all caught
  at the same level → log a warning, `teams_text = ""`, OWA-only
  brief publishes.
- `services/outlook_web_scraper.py:open_signin_window` — opens a
  second tab at `TEAMS_ACTIVITY_URL` after the OWA tab; same
  persistent profile.

No new files, no requirement changes, no test additions (formatter
tests from v2.15.0 already cover the empty-`teams_text` path).

## Tests

67 backend tests still pass. TypeScript clean.

The behavior under test:
- `test_format_blob_omits_teams_section_when_empty` (from v2.15.0)
  already covers what happens when `teams_text = ""` — Teams section
  silently absent, OWA + actions still render. That's exactly the
  shape v2.15.1 emits when Teams auth-expired is non-fatal.

## What you do after install

1. Click **Sign in to Microsoft**. A Chrome window opens with **two
   tabs**.
2. Tab 1 (OWA): sign in / re-MFA as usual. Calendar should appear.
3. Tab 2 (Teams): may show another "Stay signed in?" or consent
   prompt. Complete it. Activity feed should appear.
4. Close the Chrome window.
5. Click **Sync now**. Both calendar AND Teams sections should
   populate.

If Sync still doesn't show Teams, `backend.log` will name the
remaining cause (DOM selector, timeout, etc.) and we'll iterate.

## Known not yet patched

Carried forward:

- Teams Chat tab — Activity-only.
- Auto-sync on first launch of day.
- Subprocess-isolated transcribe + diarize.
- RecordingService decomposition.
- Subprocess timeout on finalize child.
- macOS Bluetooth audio-format-mismatch banner.
