# v2.67.0 — read the traffic the page was never allowed to see

## Install (macOS)

> v2.67.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.67.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.67.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.67.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension — and expect a permission prompt

This release needs extension **1.23.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.23.0.

**1.23.0 adds the `debugger` permission**, so Chrome may show a
warning or disable the extension until you re-enable it. That
permission is the entire subject of this release; the reasoning is
below.

## Four probes closed every other door

All from field diagnostics on the new Outlook web stack:

| Probe | Result |
| --- | --- |
| DOM scan, every root, grid and pane | 0 join-shaped anchors, 0 in label text |
| Page-visible network (190 responses) | timezone config, group lists, mailbox metadata, license fields — **not one calendar field** |
| Browser storage | `msal.3\|…` entries are `{id, nonce, data}` — **encrypted**; IndexedDB holds PDF wasm, sticky notes, app acquisitions, **no calendar** |
| The one readable token | `LokiAuthToken`, scopes `Group.ReadWrite LLM.Read User.Read.All` — **401 from every calendar endpoint** |

And `navigator.serviceWorker.controller` is non-null. The calendar is
fetched by Outlook's **service worker**, which has its own global
scope — so patching `window.fetch` in the page, which is what the
passive recorder does, can never observe it. That one fact explains
every empty join link, every "Attendees (0)", and the whole of the
last week.

There is no page-level route left. There is no readable credential.
The app-signin route needs a managed device.

## So the capture reads below the page

`chrome.debugger` observes network traffic at the browser level,
beneath whichever context issued the request. During a capture the
extension attaches to **its own capture tab and the Outlook
service-worker target**, enables the Network domain only, reads JSON
response bodies, and detaches.

Deliberately narrow, and enforced:

- **Only** the capture's own tab and service-worker targets whose URL
  is an Outlook origin — matched by hostname **suffix**, so a
  lookalike host can never be attached to. Never another tab, never
  the browser at large.
- **Network domain only.** No Debugger domain, no script evaluation,
  no breakpoints. This reads responses; it does not control the page.
- **Detached in a `finally`**, so the bar cannot outlive the capture
  even if the run throws. Pinned by a test, because a browser left
  visibly in debug mode is the worst possible failure here.

**The cost, plainly:** Chrome shows a *"Meeting Recorder is debugging
this browser"* bar on the capture tab while it runs. Settings →
**Calendar detail** turns it off; subjects and times still work
without it, detail will not.

## Proven against the real shape before release

The end-to-end rig now serves the calendar **only** from a service
worker — the page never fetches it, exactly as your tenant behaves —
and includes a meeting whose pane is empty and which the cache does
not know: its detail exists nowhere but the worker's own response.

That meeting arrives with **its join URL, attendees and invite body**.
`debuggerWorkerTargets: 1`, `debuggerBodies: 3`. If the harvest were
broken, that meeting would be blank, and no other source could cover
for it. Full rig: **24/24**.

## The calendar stops opening every half hour

You were right: a background alarm refreshed the calendar every **30
minutes**, independent of your capture schedule, opening an Outlook
tab each time. Default is now **240 minutes**, and Settings →
**Background calendar refresh** accepts any value — **0 turns it off
entirely**, refreshing only when you press Capture & Send. Setting 0
also clears the old alarm, which would otherwise keep firing forever.

## Tests

1330 backend tests, 161 extension tests. The four new extension
regressions were verified to **fail against shipped 1.22.0**:
origin-suffix matching for attach candidates, every attached target
being detached, the detach-in-`finally` guarantee, and the
configurable refresh interval.
