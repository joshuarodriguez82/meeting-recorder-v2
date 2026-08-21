# v2.51.0 — attendees are data, never guesses about what text looks like

## Install (macOS)

> v2.51.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.51.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.51.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.51.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.13.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.13.0.

## Attendees (24), of which 22 were buttons

A meeting expanded to an attendee list reading *Skip to main content,
App launcher, My Day, Org Explorer, Ribbon tabs, Chat with Copilot…* —
Outlook's interface, itemised as invitees, alongside the user's **own
account button** listed as a person, and another meeting's organiser
address pulled in from across the page.

Three defects, one root: **treating the shape of text as evidence
about what the text means.**

## The name-shape scanner is deleted, not repaired

Extensions 1.11–1.12 identified attendees by scanning the page for
labels that *look like* names — two to four title-case words, minus a
vocabulary blacklist. A blacklist can never enumerate a product's
entire interface, in every language Outlook ships in, across every
redesign; the moment it misses one label, that label is a person.

So the scanner is gone, and attendees now come only from sources where
they are **data**:

- **Outlook's own event-detail responses** — the JSON captured when an
  event is opened carries the real invitee list. Field diagnostics
  showed this filling 19 of 23 meetings.
- **Email addresses in the text the clicked pane itself added.**

A meeting whose sources yield nothing shows no attendees. An honest
zero — which "Attendees (24)" was not.

## Nothing is scanned page-wide any more

The address from a *different* meeting got attributed because the
email scan searched the whole page, and that meeting's organiser is
rendered in its own grid tile. The same logic would hand one meeting's
pasted join link to another.

Both scans — addresses and URLs — are now held to the text the clicked
pane added; the join link additionally comes from anchors that appeared
with the pane, which is already click-scoped. Mis-attributing is worse
than missing: a wrong attendee propagates into speaker identification
and follow-up recipients, and a wrong Join button puts you in someone
else's call.

## Cleaning up what 1.11–1.12 already stored

The v2.50.0 enrichment ratchet — doing exactly its job — would have
preserved the stored junk forever, since every bare capture inherits
stored detail.

A **one-time scrub** runs on the first capture after this update: junk
and genuine display names are indistinguishable in the store (both are
strings without an @), so it keeps address-shaped entries and drops the
rest. Real names refill on the same capture from Outlook's own detail
responses; the junk, its producer deleted, would never have been
replaced by anything. The scrub is flag-gated so names stored after it
— which can only come from response data — are never touched.

## The guard caught this release's own fixture

The personal-data scanner flagged one line of this release before it
shipped: a test fixture reproducing the field screenshot had copied the
user's real name into the repo as the "own account button" case. It is
now the fictional placeholder the repo's rules require. Noted because
that is the second time the guard has caught the incident being written
into the code that fixes it.

## Tests

1286 backend tests, up from 1284, and 132 extension tests — four of
them rewritten, since their premise (read names from labels) is the
thing deleted. The new contract is pinned directly: no page label can
ever become an attendee; pane-text addresses are collected; an address
visible elsewhere on the page — the exact field case — is not
attributed; a still-open pane's invitees cannot bleed into the next
meeting. The store tests pin the scrub removing the field screenshot's
junk while keeping the real address, and sparing names stored after
the flag.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 3 / 0 new, personal-data 0 (after fixing the
one it caught).
