# v2.61.0 — the app stops needing a diagnostics zip to answer "why no link?"

## Install (macOS)

> v2.61.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.61.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.61.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.61.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Extension

Still **1.17.0**. App-only release; everything here applies to what is
already in your store.

## "What the last capture found", in the app

Under Upcoming Meetings there is now a collapsible row showing what the
last capture actually did:

| | |
| --- | --- |
| Meetings queued for detail | how many were worth opening |
| Panes opened | how many actually opened |
| Tile not found / skipped | the two ways a meeting gets missed |
| Join links from invite text | Webex/Zoom paste theirs into the body |
| Join links from a button/anchor | Teams renders a button |
| Join links found in markup | the shape-agnostic fallback (1.17.0) |
| Invite bodies gained | |
| Attendee lists gained | |
| Outlook responses captured | |

**These counters have existed since v2.47.0.** They were reachable only
by generating a diagnostics zip and sending it to someone who could
read it — which is exactly why every round of "the join link still
isn't there" cost a file transfer and a day. The app knew how many
panes it opened and how many join-shaped URLs it found in them. It had
no way to say so.

The distinction that matters is now one glance:

- **Panes opened > 0 and every join-link count 0** — the meeting's join
  URL is not in the page when the capture reads it. No amount of
  DOM-scanning fixes that; the URL has to come from Outlook's own event
  data instead.
- **Panes opened 0, tile-not-found high** — the capture never reached
  those meetings, and the join link was never the problem.

Those are different bugs. Until now they looked identical.

## Two more lines of Outlook's UI stop reaching the agenda

From the field screenshot: the card echoes the meeting's own subject as
its heading, so the captured agenda opened by repeating the title
rendered directly above it — and ended with the `Email organizer`
button. Both are dropped, along with `Reply`, `Reply all`, `Decline`,
`Propose new time`, `Join online` and friends.

Only the **leading** subject echo is treated as a heading: an invite
that names the meeting inside its own text is writing, not chrome, and
keeps it.

## Tests

1323 backend tests, up from 1321.
