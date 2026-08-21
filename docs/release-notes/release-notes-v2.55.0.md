# v2.55.0 — engagement registers push to the SA Tools Portal

## Install (macOS)

> v2.55.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.55.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.55.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.55.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

**The Chrome extension is unchanged at 1.14.0.**

## What this is

A recorded engagement now flows to the SA Tools Portal on its own.
Bind a project to a portal opportunity once, and every regeneration of
that project's engagement register — commitments, decisions,
requirements, open questions — is pushed to the portal automatically,
where it becomes the opportunity's live register.

**Setup**: Settings → Templates & Integrations → *SA Tools Portal* —
paste the portal base URL (a setting, not a constant: the portal has
dev and prod hosts). Then Engagements → pick a client and project →
**Bind to portal…** — paste the opportunity's customer id and edit
token once. **Sync to portal** pushes on demand; everything after that
is automatic.

## The design decisions that matter

**The register is pushed verbatim.** The portal owns normalisation,
dedupe, merge and briefing; the recorder sends the file exactly as
written to disk. Two implementations of normalisation would disagree
within a month, so there is deliberately one. A test asserts the wire
body is byte-for-byte the file's contents under a single key.

**The edit token is a credential and is treated as one.** It is stored
in Windows Credential Manager / macOS Keychain alongside the API keys —
never in a config file, never in a log line, never in an error message.
A test feeds the service a hostile portal response that echoes the
token back and asserts it still appears nowhere.

**Failure semantics are typed, not guessed.** 503 (a partial write) and
network failures retry on the export worker's existing 5s/30s/120s
schedule. 400/422 never retry — identical bytes cannot produce a
different answer. **403 marks the binding broken and stops all pushes**
until you re-bind: retrying a revoked token forever is silent failure
wearing a retry schedule, and the Engagements view says exactly why it
stopped.

**Nothing touches the hot path.** Pushes run on a dedicated worker
thread with bounded timeouts — the same architecture that keeps flaky
exports away from record → finalize → process. The register-write hook
is wrapped so even an exploding worker cannot fail a register request,
and a test points the push at a black hole to prove it.

**Only per-project registers push.** The client-level rollup is the
union of the project registers; pushing both would file every item
twice under the same ids and flap between the two on every run. The
rollup cannot be bound, and its regeneration never fires the hook.

**Pushes are triggered by register writes, not timers.** Registers
regenerate at uneven times; anything polling the filesystem pushes
stale files and misses fresh ones. Ingest is idempotent portal-side, so
pushing on every write costs nothing.

## Tests

1299 backend tests, up from 1288. The eleven new ones map to the
integration spec's acceptance criteria: verbatim wire body, transient
vs permanent vs broken-binding semantics (including the worker
retrying transient failures and only those), the 403 → broken →
re-bind cycle, unbound and rollup scopes never pushing, the token
absent from disk, logs and hostile-echo error paths, and the
register-write hook surviving an exploding callback.

136 extension tests, unchanged. The portal push URL is scheme
allow-listed (https, or localhost for a dev portal) before any request
is built.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 3 / 0 new, personal-data 0.

## Not in this release, and why

The Cognito-backed opportunity picker (bind without any pasting) — the
spec explicitly allows paste-once now, picker later, with no wire
change. Artifact uploads via presign are the optional second phase.
Speaker-roster resolution of `SPEAKER_02`-style owners — the biggest
data-quality win the portal surfaced — is the next scheduled piece of
work, not a rider on this one. And per the spec's warning: re-tag the
mis-filed Guardian session (or bind both projects to one opportunity
deliberately) before binding Guardian.
