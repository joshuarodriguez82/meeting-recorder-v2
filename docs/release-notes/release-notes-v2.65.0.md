# v2.65.0 — the census actually reaches you now (rig-proven first)

## Install (macOS)

> v2.65.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.65.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.65.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.65.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.21.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.21.0.

## Why v2.64.0's census never appeared

The 16:13 diagnostics had the census absent despite 190 responses seen
and 22 matched. The end-to-end rig reproduced that exact state and
found two wiring faults:

1. **The post-click parser call had no diagnostics channel.** The
   field's bodies arrive *with the clicks* (`postClickBodies: 13`) —
   Outlook fetches an event's full detail when it opens — and that one
   call site dropped the diag, so the census, the key-group flags and
   the body-href gains all died on the richest payloads in the
   pipeline.
2. **Responses the recorder's vocabulary gate declined vanished
   without a trace.** A response naming its subject `cardTitle`
   matches no hint, was never recorded, and the silence was
   indistinguishable from no traffic.

Both fixed: the post-click call carries the diag; declined responses
contribute their key names (regex over the text head, no parse, names
only) to the same census; and the census accumulates across harvests
instead of overwriting.

## Rig-proven before shipping, per the field instruction

The rig now runs the exact field scenario — a calendar response in an
unrecognized shape, plus click-provoked detail responses that pass the
recorder's gate but not the parser — through the real extension, real
backend, real store. 16/16 checks, including:

- recorder-declined key names stored (`cardtitle`, `timewindow`)
- post-click unparseable key names stored (`detailenvelope`, `opensat`)
- `responsesContainJoinShapedUrl: true` from the post-click bodies
- and the pane-button markup scan genuinely recovering a Teams URL —
  a path that had never actually been exercised before (mechanism 1
  was masking it), now proven against the anti-misattribution rule

Also in this release, held from earlier: **portal bindings roam
between machines** (the bindings file travels with the recordings
folder; each machine's keychain keeps its own token, and a token-less
machine shows a paste-once amber note instead of poisoning the shared
binding as broken), and the sync toast reports what was sent.

## Tests

1328 backend tests, 156 extension tests. Fail-first throughout; full
E2E rig 16/16.
