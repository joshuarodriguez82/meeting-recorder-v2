# v2.54.0 — the button you press when investigating now reports, and the diff stops caring where the pane renders

## Install (macOS)

> v2.54.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.54.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.54.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.54.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.14.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.14.0.

## What the latest diagnostics proved

The 2026-08-20 21:29 bundle settled two things.

**The response recorder works on this tenant.** The 20:12 capture saw
261 responses, 31 of them carrying meetings, and filled attendees and
bodies for 11 events from Outlook's own data. The pipeline's front half
is not the problem.

**Those 11 bodies were destroyed one more time** — that capture ran
minutes before the v2.53.0 install, so the import door dropped them
again. And the first capture after v2.53.0 arrived with its detail
empty, for reasons that were invisible, because of the next item.

## The manual capture path reported nothing — twice over

`capture_diag` — the counters that say which stage of a capture worked
— was attached only to the background alarm's POST. The popup's
**Capture & Send**, the button a person presses *precisely when they
are investigating a problem*, went through a different POST that never
attached it. Three diagnostic bundles in a row carried a stale
alarm-path report while the runs actually under investigation were
invisible.

Both paths now attach the same diagnostics through one shared builder,
and a source-level test asserts both do — the path a user reaches for
when things are broken must be the best-reported path, not the only
silent one.

## The text diff no longer assumes where the pane renders

Extraction of an opened event's text diffed "page after click" against
"page before" with a prefix match — which silently requires the pane's
text to land *after* the existing content in document order. Where
Outlook's event panel sits in the DOM relative to the grid is exactly
the kind of fact this project cannot observe, and if the panel renders
first, that diff returns **empty for every event** — no body, no
addresses, no pasted Webex link — indistinguishable from "the pane had
nothing."

The diff is now a line-level set difference: whatever lines exist after
the click that did not exist before, wherever they landed. Three new
tests render the pane appended, prepended, and mid-page; the old code
passes only the first.

## Tests

1288 backend tests, unchanged. 136 extension tests, up from 132: the
three pane-position cases and the both-paths-report guard.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 6 / 0 new, personal-data 0.
