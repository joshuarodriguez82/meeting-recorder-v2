# v2.69.0 — ready for the delivery team

## Install (macOS)

> v2.69.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.69.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.69.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.69.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## Six delivery-phase templates

The built-in template library grew up in pre-sales, and it showed:
Requirements Gathering, Design Review, Stakeholder Update. If you're on
the delivery side — building, testing, and cutting over what pre-sales
scoped — your meetings have different fields that matter, and a
generic summary buries exactly the ones you need.

Six new built-ins, in the order the meetings happen in a real
engagement:

| Template | Built to surface |
| --- | --- |
| **Delivery Kickoff** | scope as confirmed vs the SOW, explicit out-of-scope items, assumptions to validate with owners, access requests, RACI, committed dates |
| **Technical Working Session** | integration points, exact agreed config values, data mappings, blockers with owners and dates, parked spikes |
| **UAT & Defect Triage** | per-defect ID / severity / owner / retest date, scope disputes and how each resolved, exit-criteria impact |
| **Go-Live Readiness** | go/no-go criteria and the decision itself, cutover runbook items, rollback triggers and authority, freeze windows, sign-offs |
| **Hypercare Review** | issues by severity with trends, exit-criteria progress, ops-handoff items, escalations |
| **Change Request Scoping** | the requested change, the in/out-of-SOW case from each side, effort sizing, schedule impact, what was actually agreed |

They appear in your template picker automatically on first launch after
updating — no reinstall, no import, and your own templates and edits
are untouched. Like every built-in, they can be edited freely and
restored to the original with **Reset to default**.

## The auth token no longer appears in backend.log

Requests that can't set headers (the live-transcript stream, audio
players, thumbnails) pass the backend auth token in the URL, and the
access log printed those URLs — token included — into `backend.log`,
whose tail ships inside diagnostics bundles. Since the token survives
restarts, sharing a diagnostics zip meant sharing a live credential.

Access-log lines are now scrubbed before they're written: the log
still records every request for debugging, with `token=REDACTED` in
place of the value. If you've shared a diagnostics bundle before this
release, quit the app, delete the token file next to the app's config,
and relaunch — a fresh token is generated and the extension will ask
to be re-paired.

## For the engineers about to read the source

Two new documents at the repo root:

- **`SECURITY.md`** — the full security model with a file path for
  every claim: the token lifecycle, why the extension holds the
  `debugger` permission and exactly what it does with it, what's in
  the OS keychain, the CI security gates, and an honest
  known-limitations section.
- **`CONTRIBUTING.md`** — layout, run/test commands, the house rules,
  and the release procedure.

## Also in this release

- Opening the detail dialog for a session that's **still recording**
  no longer shows a broken player and a "audio file is missing"
  error implying the recording is lost. The dialog now says it's
  recording; processing is blocked with "stop the recording first"
  instead of a misleading failure. (Merged from #187 — found live,
  mid-meeting, 35 minutes in.)
