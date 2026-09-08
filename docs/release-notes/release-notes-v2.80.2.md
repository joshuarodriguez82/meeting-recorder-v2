# v2.80.2 — the extension's Capture & Send stops reporting a false error

## Install (macOS)

> v2.80.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.80.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.80.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.80.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension too

**The extension goes to 1.24.0 and this release is only about the
extension** — download `chrome-extension.zip` from the release and
reload it at `chrome://extensions`. The app itself is unchanged.

## Capture & Send no longer shows an error for a capture that worked

Clicking **Capture & Send** could show:

> A listener indicated an asynchronous response by returning true, but
> the message channel closed before a response was received

That is Chrome's wording, not ours, and it was misleading twice over.
The capture had usually *started* fine, and in many cases finished and
sent — you were being shown a failure over work that succeeded.

A capture opens five pages in the background — Outlook, Teams Activity,
Inbox, Teams Chat, Calendar — and can take **up to three minutes**. The
popup used to hold a single connection open to the background worker
for that entire time. Chrome shuts an idle extension worker down after
about thirty seconds, and the popup itself closes the moment you click
anywhere else. Either one broke the connection, and Chrome reported the
break as an error.

Now the click is acknowledged straight away and the capture reports its
progress separately, which fixes three things at once:

- **No false error.** You are told what actually happened.
- **It names the surface it is on** — "Reading Teams Activity…" —
  instead of one spinner for three minutes.
- **You can close the popup.** The capture keeps going, and reopening
  shows where it got to. Nobody should have to babysit a window for
  three minutes.

A capture that genuinely dies mid-run — Chrome shutting the worker down
under memory pressure — now says so, rather than spinning forever.

## Known issue: calendar events

Some installs show **"0 events (no candidate elements found)"** after a
capture. That means the calendar page rendered in a shape the extension
does not recognise, and it is a separate problem this release does not
fix. It is reported honestly rather than silently, so it is visible.

If you see it, your Upcoming Meetings and auto-record still work from
the **local** calendar on macOS and Windows; only the extension's copy
is affected.
