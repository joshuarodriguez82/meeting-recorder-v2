# v2.58.0 — the invite body was one frame deeper the whole time

## Install (macOS)

> v2.58.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.58.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.58.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.58.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.16.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.16.0.

## What v2.57.0 showed, and what it proved

v2.57.0 captured a real invite and rendered its body as this:

```
Join
Chat
Fri 8/21/2026 10:00 AM - 11:00 AM
No location added
GG
<organizer> invited you.
```

That is the detail pane's **chrome** — its buttons, its date line, its
footer. Not one word of the invite. No join link either. Both were on
screen the entire time.

Which was the most useful failure in this whole sequence, because it is
not a subtle one. The reader was seeing the frame and never the
picture.

## The cause

Outlook renders the invite body in its own **same-origin iframe** (the
message-body frame), and renders parts of the event card inside
**shadow roots**. `document.body.innerText` stops at both boundaries.
So does `document.querySelectorAll("a[href]")`.

The detail reader used exactly those two calls. So:

- **Webex / Zoom invites** — the pasted join URL and the agenda live in
  the body iframe. The reader collected the surrounding chrome instead
  and reported no link.
- **Teams meetings** — the Join anchor sits in a shadow root. Every
  field diagnostic ever collected reported `joinFromAnchor: 0`. Not
  because Teams hides its URL, but because the scan never entered the
  root holding it.
- **Attendee addresses** in the invite body were invisible for the same
  reason.

The calendar **grid** scanner has always walked iframes and shadow
roots — that is why the meeting list itself works. The detail reader
was the one DOM consumer that did not, and reading that pane is its
entire job.

Both boundaries are now crossed, with bounded depth and a root cap, and
cross-origin frames skipped rather than fatal.

## Proven against a running browser, not asserted

The end-to-end rig introduced in v2.57.0 was extended to render the
pane exactly as the field screenshot showed it: chrome-only text in the
pane, the real body in an iframe, a Teams anchor in a shadow root. It
reproduced the failure character-for-character — same `Join Chat Fri
8/21/2026 …` body, same empty join URL — then passed once the traversal
landed:

| Meeting | Result |
| --- | --- |
| Teams call | join URL from the shadow-root anchor, attendees, body |
| Webex call | `webex.com/meet/…` from the iframe, agenda, 2 addresses |
| Zoom call | `zoom.us/j/…`, body |
| Internal meeting | body, address |
| Ended yesterday | never opened |

The two unit regressions were verified to **fail against the shipped
1.15.0 build** before the fix was written.

## You can actually paste the connection block now

The bind form was a popover positioned inside the Projects card, so the
card's own bounds cropped it — the paste box was cut off mid-height and
the Bind button was off-screen. A bind form is a task, not a tooltip.
It is now a proper dialog on its own layer, with a full-size paste box
that no container can clip.

## Tests

1305 backend tests, 141 extension tests (up from 139). Both new
extension regressions verified failing first. bandit 0 new, semgrep 0
new, personal-data 0.
