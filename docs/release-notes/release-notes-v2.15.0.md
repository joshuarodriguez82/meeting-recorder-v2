# v2.15.0 — Outlook Web sync actually works, Teams Activity, sticky-toast fix

> **What this release fixes / adds:**
>
> 1. **Outlook Web Sync now works on Google Drive / OneDrive setups.**
>    v2.14.0's persistent Chrome profile lived under your
>    `recordings_dir`. If that path was on Google Drive Stream or
>    OneDrive — which mine is — cloud-sync filter drivers locked /
>    corrupted Chrome's cookie store between the headed sign-in window
>    and the subsequent headless scrape. Symptom: empty calendar
>    extraction right after a successful sign-in. **Fixed by moving the
>    profile to `USER_DATA_DIR/web-session/`** (Windows: `%LOCALAPPDATA%`,
>    macOS: `~/Library/Application Support/MeetingRecorder`) — the same
>    LOCAL-only path speaker profiles and the auto-record blocklist
>    use. Per-machine, never roams.
> 2. **Teams Activity is now in the brief.** The Today tab's sync now
>    visits `teams.microsoft.com/v2/?clientType=desktop#/activity`
>    headlessly against the same persistent profile and joins
>    @mentions, replies, missed calls, and meeting reminders into the
>    same LLM-parsed briefing as OWA. The LLM lifts those into the
>    Needs Response section so they show up alongside email replies
>    needed.
> 3. **"Signed in" toast no longer sticks on screen.** v2.14.0's
>    sign-in success toast inherited `duration: Infinity` from the
>    loading toast it was replacing. The Sonner toast library merges
>    options when you pass `{ id }`, so the Infinity persisted.
>    Dismiss-then-show-fresh-toast pattern now used in success + error
>    paths.

This is a quality-of-life patch on top of v2.14.0's Outlook Web sync
feature. No data-pipeline or recording changes.

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

## What's new

### 1. The cloud-sync profile bug (the headline)

Field repro 2026-06-29: signed in via the headed Chrome window, OWA
calendar visible in the window, closed it cleanly. Clicked **Sync now**
2 seconds later — got `HTTP 502: OWA returned an empty calendar`.
Tried again 2.5 minutes later — got HTTP 200 but the parsed briefing
had `agenda=0, needs_response=0, fyi=0`. The scraped text was only
~390 characters (just OWA menu chrome, no calendar grid).

`backend.log` showed:

```
[INFO] Scraping OWA day view (profile=G:\My Drive\MRv2\web-session)
```

**Root cause.** v2.14.0 wired the persistent profile directory to
whatever `recordings_dir` the user had configured. For users with
`recordings_dir` on Google Drive Stream / OneDrive — common for
laptops that need recordings to follow them between devices — that
puts Chrome's profile (cookies, IndexedDB, SingletonLock, Local
Storage) on a cloud-sync filter driver. Same class of bug as the
v2.12.0 read-side ML pipeline crash, just on the auth side now:

- Headed sign-in writes cookies to disk on close. The cloud-sync
  driver intercepts the writes and queues them for upload.
- Headless scrape starts ~2 seconds later. The profile dir on disk
  is partially written — some cookies present, some still in the
  driver's queue.
- Chrome opens the partial profile, finds insufficient auth state,
  navigates to outlook.office.com and gets the unauthed/redirect
  landing page.
- Our scraper extracts `[role="main"]` from that — which on the
  unauthed page is the menu shell (~390 chars), not the calendar.
- LLM correctly extracts nothing from menu chrome. Briefing
  stores as empty.

The fix is the same fix we made for the recordings dir's audio reads
in v2.12.0 — move the contested data off the cloud volume:

```python
# Before (v2.14.0):
profile = recordings_dir / "web-session"   # often on Google Drive

# After (v2.15.0+):
profile = USER_DATA_DIR / "web-session"    # %LOCALAPPDATA%, etc.
```

`USER_DATA_DIR` is already used for speaker profiles and the
auto-record blocklist for the exact same reason — those need to be
per-machine and must never sync. The web session is identity-bound to
your work account; cross-device cookie sync is a security problem
even when the sync works.

**Migration impact for upgraded users:** the new profile dir is empty
on first launch of v2.15.0. The auth-expired banner will surface on
the next Sync, you click Sign in once, MFA tap, done. The OLD profile
under your `recordings_dir/web-session/` is left in place (we don't
auto-delete) so you can clean it up at your leisure — it doesn't
affect anything.

### 2. Teams Activity in the brief

v2.14.0 release notes promised Teams was a follow-up; v2.15.0 ships
it. The same persistent Chrome profile that authenticates OWA now
also authenticates Teams Web — Microsoft uses one M365 session
across both surfaces, so signing into the recorder's profile once
covers both.

The scraper navigates to:

```
https://teams.microsoft.com/v2/?clientType=desktop#/activity
```

after OWA's day view, extracts the `[role="main"]` inner text from
the Activity feed (which holds @mentions, replies, missed calls,
meeting reminders), and joins it into the same LLM-parsed briefing.
The summarizer's `parse_daily_briefing` prompt picks @mentions up as
**needs_response** items rather than agenda items — different
section in the Today view from your calendar events. That's the
right shape for "who's waiting on me to reply right now."

**Failure handling.** Teams Web is heavier than OWA and known to be
more finicky in headless Chromium. If the Teams scrape fails for any
reason that ISN'T auth-expired, v2.15.0 omits the Teams section and
publishes an OWA-only brief rather than failing the whole sync. The
backend.log will say "Teams scrape failed; OWA-only brief" with the
reason. Auth-expired errors during the Teams scrape DO propagate to
the UI's banner — since the cookies are stale for both surfaces,
fixing it once fixes both.

**Known not-yet-patched on the Teams side:** the Chat tab (active
1:1 conversations) is noisier than Activity and harder to scope to
"today" without DOM-specific filtering. Activity covers ~80% of the
"what needs my attention" value; Chat may follow in a later release
if it turns out Activity misses important things.

### 3. Sticky "Signed in" toast

v2.14.0 had a tiny but annoying bug: clicking **Sign in to Microsoft**
showed a loading toast with `duration: Infinity` (intentional —
sign-in can take minutes if you're chasing the Authenticator app).
When sign-in completed, my code called `toast.success(msg, { id })`
to UPDATE the loading toast in place. But [Sonner](https://sonner.emilkowal.ski/)
merges options on update, so the `Infinity` duration persisted and
the success toast never dismissed itself.

Fix: dismiss the loading toast first, then show a fresh success
toast with a normal 5-second duration. Same pattern applied to the
error path. Net change: 6 lines.

## Backend additions

- `services/outlook_web_scraper.py`:
  - `profile_dir_for()` semantics changed: docstring now says "LOCAL-only"
    explicitly. Function signature unchanged.
  - New `scrape_today_teams_text()` — mirrors `scrape_today_briefing_text`
    but navigates Teams Activity. Returns "" instead of raising on
    non-auth failures so Teams flakiness doesn't kill the brief.
  - `format_for_briefing_parser` gains optional `teams_text=` kwarg.
- `server.py`:
  - `/briefing/signin` + `/briefing/sync` import + pass `USER_DATA_DIR`
    instead of `recordings_dir`.
  - `/briefing/sync` calls both scrapers in sequence; Teams failures
    are caught and skipped, OWA failures fail the whole sync.

No requirement changes; Playwright is still all we need.

## Tests

Total backend test count: **67** (was 64 in v2.14.0). 3 new in
`test_outlook_web_scraper.py` covering the Teams formatter
integration:

- `test_format_blob_joins_teams_text_with_owa` — Teams section appears,
  labeled, between OWA and action items.
- `test_format_blob_omits_teams_section_when_empty` — None / "" /
  whitespace teams_text yields no Teams header (silent omission so
  the brief still looks clean when Teams fails).
- `test_format_blob_full_combination` — order assertion (OWA → Teams →
  Actions) so future refactors don't accidentally reshuffle the LLM
  prompt's expected layout.

Browser-driven tests still aren't possible in CI (Chrome + Playwright
aren't reachable from the runner) — the live verification happens on
your machine after install.

## Bundle changes

None. The new Teams scraping is pure Python; persistent profile dir
just moves a folder name.

## Known not yet patched

- **Teams Chat tab** — Activity-only for now (see Teams section above).
- **Auto-sync on first launch of day** — Sync is still manual.
- **Subprocess-isolated transcribe + diarize** — deferred from v2.12.0.
- **RecordingService decomposition** — 1,300-line god-object remains.
- **Subprocess timeout on finalize child** — small fix tracked.
- **macOS Bluetooth audio-format-mismatch banner** — Windows-only.
