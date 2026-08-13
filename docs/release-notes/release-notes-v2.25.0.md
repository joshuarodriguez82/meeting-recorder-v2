# v2.25.0 — security hardening, and no more pickle

## Install (macOS)

> v2.25.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.25.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.25.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.25.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Search indexes no longer use pickle

Search and knowledge-folder indexes were stored as Python `pickle`
files. Loading a pickle executes whatever code is inside it, so a
tampered `.pkl` meant arbitrary code execution just from opening Search.

The usual dismissal — "an attacker who can write that file already owns
the machine" — **did not apply here**. Those `.embeddings.pkl` sidecars
live in the recordings folder, which is synced between machines through
Google Drive, and the archive folder replicates them deliberately. A
compromised cloud account or a bad share could put a hostile pickle on
disk with no local access at all.

Indexes are now stored as NumPy `.npz` (array data) plus a `.json`
sidecar (metadata), loaded with `allow_pickle=False` passed explicitly
rather than left to a default that has changed between NumPy versions.

**Legacy `.pkl` files are never opened — not even once to migrate.**
Migrating by unpickling would have preserved the exact vulnerability, on
exactly the files most likely to be tampered with. Instead they're
deleted on sight and the index is rebuilt from the transcript or source
document.

**What you'll notice:** the first time you search after updating, indexes
rebuild in the background. With a large library that takes a while, and
sessions won't appear in semantic search results until theirs is rebuilt.
Everything else works normally throughout. This is a one-time cost.

The cross-machine sync path was also updated so it only ever copies the
new formats — otherwise it would have kept replicating pickles between
your machines while appearing fixed.

## Served files are confined to your recordings folders

The endpoints that serve session audio and screenshots took their file
paths from the session's JSON file and served whatever was there. A
tampered session JSON could point at any file on disk.

That matters here for the same reason as above: session JSONs are
cloud-synced, so they aren't fully trusted local input. Both endpoints
now resolve the path and require it to sit inside one of your configured
recording or archive roots, using the app's real multi-root logic — your
archive folders still work exactly as before.

## API auth no longer fails open

If `MEETING_RECORDER_TOKEN` was unset, the backend disabled
authentication entirely. The packaged app always injects a token, so this
only ever affected running `server.py` by hand — but "no token" silently
meaning "no auth" is the wrong default.

Auth now fails closed. Running the backend standalone without auth
requires setting `MEETING_RECORDER_AUTH_DISABLED=1` deliberately. The
packaged app is unaffected: the Tauri shell always injects a real token
and panics rather than starting without one.

## Live API keys now reach the keychain

`LIVE_OPENAI_API_KEY` and `LIVE_ANTHROPIC_API_KEY` were being persisted
with no keychain protection at all, while the other three keys got it.
All five now go through the OS keychain.

### Why the keys are still also written to config.env

A security review flagged the plaintext copy in `config.env` and
recommended removing it. That change was implemented, then **reverted
deliberately**.

The code already documented why: an earlier build did exactly this, and
whenever a keychain entry later became unreadable — an unsigned macOS app
rebuilt with a new ad-hoc signature, or a Windows Credential Manager
entry written in a different context — the key was simply gone, producing
a hard `401 invalid x-api-key` with no fallback.

Verifying the keychain write catches a keychain that lies about
succeeding. It cannot catch an entry that becomes unreadable later, which
is the failure that actually happened. Since this app updates often and
the macOS build is unsigned, that risk is live — and `config.env` lives
in local app data, which is **not** cloud-synced, so the plaintext copy
is local-only.

Losing your API keys on every upgrade is worse than a local-only
plaintext copy. Keychain-only storage needs two things first: a signed,
notarized macOS build, and a recovery flow that detects an unreadable
entry at load time and asks you to re-enter the key. Both are recorded in
the code for whoever revisits this.

One genuine bug did come out of it: the loader consulted the keychain
*before* the file, even though the file was documented as authoritative.
A stale keychain entry could therefore shadow a freshly saved key — the
same 401 by a different route. A non-empty file value now wins, with the
keychain consulted only when the file has nothing.

## Dependency scanning in CI

`pip-audit`, `npm audit` and `cargo audit` now run on pull requests,
pushes to `main`, and weekly — new advisories land against unchanged
code, which only a scheduled scan catches.

The job is **non-blocking by design** and is not wired into the release
workflow. This project pins heavy ML dependencies for documented
compatibility reasons, and those pins already carry advisories that can't
be acted on without a migration project. A gating check would be red from
day one and disabled within a week.

Setting it up surfaced a real packaging bug: `requirements-cpu.txt` lists
`pywin32` and `pyaudiowpatch` with no platform marker, unlike the
neighbouring `pycaw` line. That file can't resolve on a non-Windows
machine. It's audited on Windows for now; the marker itself still wants
fixing.

## Version metadata

`Cargo.toml` had drifted to `2.10.4` and `package.json` sat at `0.1.0`
while the app shipped as 2.2x. All three now match the release version.
`tauri.conf.json` remains the single source of truth the release workflow
reads.

## Still being verified

The `0xC0000005` crash fix from v2.23.2 is still being judged on
evidence: `crash.log` is append-only, so either new
`Windows fatal exception` entries stop appearing or they don't. Nothing
in this release touches that code path.

A SQLite index for session metadata is built and tested (~8.6× faster
session listing) but deliberately **not** shipped here — it rewrites the
exact code path that appears in every crash dump, and shipping it now
would make the crash-fix result unreadable.
