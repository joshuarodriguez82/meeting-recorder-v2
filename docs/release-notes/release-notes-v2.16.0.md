# v2.16.0 — Chrome extension is the new Today briefing path

> **What this release adds:**
>
> 1. **Daily briefing now ships from a Chrome extension** running in
>    YOUR real signed-in Chrome — replacing the v2.15.x Playwright
>    sync that fought (and lost to) Microsoft's enterprise-tenant
>    automation detection for the entire dot-release saga. Because
>    the extension runs in the user's real browser, Microsoft's
>    detection doesn't fire and the scrape authenticates with the
>    same cookies you use to read mail.
> 2. **Four sources, not two.** The extension pulls Outlook day
>    calendar, Outlook focused inbox, Teams Activity, **and Teams
>    Chat** every capture, so the briefing surfaces unread chat
>    messages alongside calendar / inbox items.
> 3. **Background auto-capture.** The extension's service worker
>    fires `chrome.alarms` at up to three configurable local times
>    each day (defaults 08:00 / 12:00 / 17:00) and re-runs the
>    capture automatically — no clicking required as long as Chrome
>    is running.
> 4. **Backend port stays at 17645 across recorder restarts.** Token
>    is persisted at `USER_DATA_DIR/extension-token` so you only
>    paste the URL + token into the extension's Settings page once
>    and it survives every recorder restart.
> 5. **Today tab cleanup.** The dead **Sync now** and **Sign in to
>    Microsoft** buttons are gone (they only worked against the
>    Playwright path that's been removed). The manual **Import
>    briefing** button stays as a fallback for users who can't
>    install the extension.

## Install (macOS)

> v2.16.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.16.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.16.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.16.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Install the Chrome extension

The extension ships as `chrome-extension.zip` alongside the desktop
installers on this release's GitHub assets list.

1. Download `chrome-extension.zip` and extract it to a folder you'll
   keep around (Chrome reloads the extension from this folder on every
   browser start, so don't delete it after install).
2. Open `chrome://extensions` and flip on **Developer mode** (top-right
   toggle).
3. Click **Load unpacked** and pick the extracted folder. You'll see
   *Meeting Recorder — Capture OWA + Teams* in the list.
4. Pin the extension icon to the Chrome toolbar.
5. In Meeting Recorder, go to **Settings → Chrome Extension**. Click
   *Show* next to the auth token, then *Copy* on both the **Backend
   URL** and **Auth token** fields.
6. Click the extension icon → **Settings** (the gear). Paste the
   backend URL and token, click **Save**.

That's it. Click **Capture & Send** in the extension popup to push a
fresh briefing to Today right now, or flip on **Auto-capture** to let
the alarms do it.

## How it works

### The data path

The extension runs in your real Chrome — same browser, same cookies,
same M365 SSO session you use day-to-day. When a capture fires (manual
or alarm), the service worker opens four background tabs in sequence:

| URL | Source |
|---|---|
| `outlook.office.com/calendar/view/day` | Today's calendar |
| `teams.microsoft.com/v2/?clientType=desktop#/activity` | Mentions, replies, missed calls |
| `outlook.cloud.microsoft/mail/?folder=focusedinbox` | Focused inbox |
| `teams.microsoft.com/v2/?clientType=desktop#/chat` | Chat list with unread |

For each tab the worker polls `[role="main"]` / `main` / `[role="grid"]`
/ `[role="feed"]` / `[data-app-section]` / `#mainPaneContainer` /
`#app` and picks the largest result — Microsoft's cloud.microsoft
shell doesn't wrap real content in `[role="main"]` consistently, so
multi-selector extraction picks up the right subtree. The tab closes
once content settles (text stops growing for 3 polls) or the
per-source max wait fires (25-40s depending on source). All four
texts are POSTed to `/briefing/extension-import` with bearer auth.
Claude reshapes the four labeled blocks into the Today view's
sections.

### Teams Chat needs an extra nav step

`teams.microsoft.com/v2/?clientType=desktop#/chat` looks like it
should drop you in chat directly, but Microsoft's redirect from
`teams.microsoft.com` to `teams.cloud.microsoft` strips the hash
route, so the tab lands on Teams' default view (Activity). v2.16.0
works around this by re-setting `window.location.hash = '#/chat'` in
the loaded tab (with `#/conversations` as a second attempt), and
falls back to clicking the left-sidebar nav button via a wide
selector list (`data-tid`, `aria-label`, `role=treeitem`, `href`).
If both fail, the service worker logs the visible `data-tid` and
`aria-label` values to the worker console so the selector list can
be extended next round.

### Auto-capture

Flip **Auto-capture** on in the extension popup, pick up to three
times of day, and the service worker registers
`chrome.alarms` with `periodInMinutes: 1440` (daily). On each fire:

- If the recorder isn't reachable, skip (no toast spam).
- If a capture ran within the last hour, skip (dedupe — manual
  capture from the popup bypasses this).
- Otherwise, run the four-source capture, POST, store
  `lastCaptureAt` + `lastResult` in `chrome.storage.local`.

Alarms re-arm on `chrome.runtime.onStartup` and on
`chrome.storage.onChanged` (so flipping the toggle or editing times
takes effect immediately, not on next browser restart).

### Port stability and persisted token

Earlier builds picked a random free port on every recorder launch
and minted a fresh auth token — meaning you had to re-paste both
values into the extension every time the recorder restarted.
v2.16.0 fixes both:

- **Port:** the Rust sidecar binder tries `PREFERRED_PORT=17645`
  first; only on `EADDRINUSE` does it fall back to a random free
  port. In normal use the recorder always lands on 17645.
- **Token:** persisted at
  `%LOCALAPPDATA%\MeetingRecorder\extension-token` (Windows) /
  `~/Library/Application Support/MeetingRecorder/extension-token`
  (macOS). Read on startup; only minted if the file is missing.
  Delete the file to rotate the token.

## What v2.16.0 removes

- The **Sync now** and **Sign in to Microsoft** buttons on the Today
  tab. They drove the Playwright path that v2.15.x was built around,
  and that whole path is gone. The **Import briefing** button stays
  as a manual fallback (paste M365 Copilot output → parse).
- The Playwright-based `outlook_web_scraper` service and its
  `/briefing/sync` + `/briefing/signin` endpoints are still present
  but no longer driven by any UI. They'll be deleted in v2.17 once
  we're sure nobody's hooking them externally.

## Frontend additions

- New **Chrome Extension** card on the Settings page with Copy
  buttons for backend URL + auth token, and a 6-step first-time
  install walkthrough.
- New endpoint `POST /briefing/extension-import` accepts
  `owa_text` / `teams_text` / `inbox_text` / `chat_text`, stitches
  them with labeled section headers, and runs the same parser the
  manual paste flow uses.
- Usage Guide → Today section rewritten end-to-end for the
  extension flow (the M365 Copilot path stays as a fallback at the
  bottom). New troubleshooting entries for short captures and
  unreachable backends.

## chrome-extension/ in the repo

The whole extension lives at the repo root in `chrome-extension/`
(manifest v3, service worker, popup, options page). It's bundled into
`chrome-extension.zip` as a release asset by the Windows build job in
`.github/workflows/release.yml`. Anyone with read access to the repo
can also load it directly as a developer extension.

## Bundle changes

None for the desktop app. The new release asset is
`chrome-extension.zip` (~7 KB).

## Known not yet patched

- **Extension auto-install / sideload signing.** You still load
  unpacked from `chrome://extensions`. Web Store listing would
  require a publisher account and review; we'll do that once the
  extension is stable.
- **Teams Chat selector list.** The hash-nav path covers most
  builds; if the click fallback fails too, look in the service
  worker console for the dumped `data-tid` / `aria-label` list and
  open an issue with that output so the selector list can be
  extended.
- **Outlook COM Calendar fetch error in Record view.** Still relies
  on Classic Outlook + COM on Windows. Plan is to switch the
  Upcoming Meetings panel to use the same extension-driven 7-day
  calendar pull; not in this release.
- **Subprocess-isolated transcribe + diarize** — deferred from
  v2.12.0.
- **RecordingService decomposition** — 1,300-line god-object
  remains.
