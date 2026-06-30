# v2.15.2 — Scraper waits for the SPA to actually mount, fails loudly when it doesn't

> **What this release fixes:**
>
> The OWA and Teams scrapers no longer extract a half-rendered React
> shell and silently publish an empty briefing. Both now poll the
> page's inner-text size until it either reaches a useful threshold
> or stops growing for several polls in a row — the right signal for
> "is the SPA done mounting." If after the timeout the extracted text
> is still tiny (< 500 chars), Sync now returns a clear error
> instead of silently storing an empty brief.

This release is the actual fix for the empty-brief bug field-reproduced
on v2.15.1. The v2.15.1 release made the auth dance non-fatal but left
the underlying scraper logic unchanged — and that scraper was
extracting 392 characters of menu chrome before the calendar grid
mounted, producing the "Briefing imported" success toast with zero
agenda / zero needs-response / zero FYI entries.

## Install (macOS)

> v2.15.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.15.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.15.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.15.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's new

### 1. New `_wait_for_text_to_settle` helper

v2.15.0 / v2.15.1 used `await page.wait_for_load_state("networkidle",
timeout=15s)` as the "is the page ready" signal. That returned in
~500ms because OWA's initial bundle loaded fast and the subsequent
telemetry pings hadn't started yet. We then extracted from a barely
mounted React tree — 392 chars of left-nav + top-bar text, no
calendar grid.

v2.15.2 replaces networkidle with a content-size poll. New helper
`_wait_for_text_to_settle(locator, target_chars, min_useful_chars,
max_wait_sec)` checks the locator's `inner_text` every second and
exits when ONE of:

1. **Text crosses `target_chars`** — page is rich enough, return
   immediately. (OWA target = 1500 chars; Teams target = 1500 chars.)
2. **Text stops growing for `STABILITY_POLLS` polls in a row** — the
   page has finished whatever it's going to load. If above the
   "useful floor" (700 for OWA, 500 for Teams), return that. If
   below, return it BUT log a warning so future repros are
   instantly diagnosable from `backend.log`.
3. **`max_wait_sec` elapses** (30s for OWA, 35s for Teams) — give
   up gracefully, return whatever we have, log a warning.

Field-repro outcome: a populated weekday calendar should now return
in 5-10 seconds with 3000-8000 chars of actual event text; an empty
day still returns in ~5s with the time axis (700-1000 chars); the
v2.15.1 bug case (grid never renders) returns in ~5s with the warning
"content stable at NNN chars after T.Ts — below useful floor".

### 2. Hard floor: scrapes returning < 500 chars now fail loudly

If the OWA scrape returns fewer than 500 characters even after the
full 30-second wait, `OutlookScraperError` is raised instead of
silently passing 392 chars of menu chrome to the LLM (which produced
the v2.15.1 "Briefing imported" toast with empty agenda). The user
sees a clear error toast:

> Sync failed: OWA's calendar grid didn't render in time
> (only 392 chars extracted; expected ≥500). Try Sync again,
> or click Sign in to Microsoft and confirm your calendar
> loads in the OWA tab before closing.

Teams stays non-fatal (returns "" on small/empty content) so a Teams
failure doesn't tank the brief — OWA-only briefings still publish.

### 3. Backend log is much more useful now

Every poll-settle exit logs the path it took:

```
[INFO] OWA: content reached target (4823 chars) in 6.2s
[INFO] OWA: content stable at 942 chars (below target 1500 but
       above useful floor 700) after 8.3s
[WARNING] OWA: content stable at 392 chars after 5.1s — below
          useful floor (700). SPA may not have finished mounting;
          brief may be incomplete.
[WARNING] OWA: content never settled within 30s; using last=812 chars.
```

If you ever hit empty-brief again, paste this section of
`backend.log` and the diagnostic is immediate.

## Backend changes

- `services/outlook_web_scraper.py`:
  - New `_wait_for_text_to_settle` helper.
  - `scrape_today_briefing_text` uses it (with OWA targets); raises
    `OutlookScraperError` below `MIN_SCRAPE_FLOOR` (500 chars).
  - `scrape_today_teams_text` uses it (with Teams targets, longer
    max-wait); returns "" on small/empty content (non-fatal).
- Constants: `OWA_TARGET_CHARS=1500`, `OWA_MIN_USEFUL_CHARS=700`,
  `TEAMS_TARGET_CHARS=1500`, `TEAMS_MIN_USEFUL_CHARS=500`,
  `MIN_SCRAPE_FLOOR=500`, `CONTENT_SETTLE_MAX_WAIT_SEC=30`,
  `TEAMS_CONTENT_SETTLE_MAX_WAIT_SEC=35`, `STABILITY_POLLS=3`.
- Removed: `wait_for_load_state("networkidle")` calls in both scrapes.

## Tests

Total backend test count: **72** (was 67 in v2.15.1). 5 new in
`test_outlook_web_scraper.py` covering the wait helper:

- `test_wait_returns_early_on_target_reached` — hot path.
- `test_wait_returns_on_stability_below_target` — empty-day case.
- `test_wait_returns_on_stability_even_below_floor` — v2.15.1 bug
  case + warning log assertion.
- `test_wait_returns_last_text_at_max_wait` — timeout fallback.
- `test_wait_handles_inner_text_exceptions` — Playwright transient
  exceptions don't abort the wait.

All use a fake locator + a patched `asyncio.sleep` so they run in
milliseconds even though the helper polls every second in
production.

## What you do after install

Same as v2.15.1 — but Sync should actually produce a populated brief
this time:

1. Click **Sync now**. (Or click **Sign in to Microsoft** first if
   the auth-expired banner is showing.)
2. Wait 5-15 seconds.
3. Today tab should show calendar events + (if Teams is signed in
   from the v2.15.1 two-tab window) Activity items in Needs Response.

If you still get an empty brief or a clear "didn't render in time"
error, the new `OWA: content stable...` lines in `backend.log` name
the cause precisely — paste them and the next iteration is a small
tuning change, not a flailing fix.

## Known not yet patched

- **Teams Chat tab** — Activity-only.
- **Auto-sync on first launch of day**.
- **Teams two-tab sign-in is one-and-done** — if you close the
  Chrome window before Teams' OAuth fully completes in tab 2, the
  next Sync's Teams scrape will hit the auth dance again. Workaround
  is to leave the Chrome window open until BOTH tabs show their
  content (calendar in tab 1, Activity feed in tab 2) before
  closing.
- Subprocess-isolated transcribe + diarize.
- RecordingService decomposition.
- Subprocess timeout on finalize child.
- macOS Bluetooth audio-format-mismatch banner.
