# Security

This document describes the security model as built, so you can review it
against the code rather than take it on faith. File paths are given for
every claim.

## Reporting

Open a GitHub issue for anything that is not sensitive. For anything that
is — a credential leak, an exploitable endpoint — use GitHub's private
vulnerability reporting on this repo instead of a public issue.

## Threat model

This is a **local desktop app**. The backend is a FastAPI process bound to
`127.0.0.1` only (`backend/server.py`, bottom of file). The threats it
defends against:

1. **Other local processes and browser tabs.** Binding to localhost does
   not keep out the user's own browser — any web page can send requests
   to `http://127.0.0.1:*`. Defenses: a mandatory bearer token on every
   non-exempt request, and CORS pinned to the app's own origins
   (`_ALLOWED_ORIGINS` in `backend/server.py`). Only `/health` is exempt.
2. **Credential theft from disk.** LLM API keys live in the OS keychain
   (Windows Credential Manager / macOS Keychain / Secret Service), not in
   config files — `backend/config/secrets.py`. Portal edit tokens are
   also keychain-only and deliberately do **not** roam between machines
   even though the rest of the portal binding does
   (`backend/services/portal_push_service.py`).
3. **Leakage through shareable artifacts.** Diagnostics bundles redact
   settings through an allow-list (anything matching
   key/token/secret/password is dropped — `backend/utils/diagnostics_bundle.py`),
   and the auth token is scrubbed from access-log lines before they reach
   `backend.log` (`backend/utils/access_log_redaction.py`).

## The backend auth token

- Generated once from the OS CSPRNG (32 bytes, hex) and persisted so the
  Chrome extension's saved copy survives app restarts —
  `src-tauri/src/lib.rs::generate_backend_token`.
- Checked with constant-time comparison on every request; the middleware
  fails **closed** when the token env var is missing
  (`backend/server.py::require_backend_token`).
- Two presentation channels: `Authorization: Bearer` (normal), and
  `?token=` for EventSource/`<audio>`/`<img>` which cannot set headers.
  The query channel is why access-log redaction exists.
- `MEETING_RECORDER_AUTH_DISABLED=1` disables auth entirely. It is a
  development escape hatch for running `python server.py` standalone.
  Never set it on a machine that records real meetings.

## The Chrome extension

`chrome-extension/` requests the `debugger` permission. That is the most
powerful permission in the manifest and it deserves suspicion, so here is
its exact use: during a calendar capture, the extension attaches the
Chrome DevTools protocol to its own capture tab to read network response
bodies that Outlook's service worker fetches (invisible to page-level
interception). It attaches at capture start and detaches in a `finally`
(`chrome-extension/background.js`, `DebuggerHarvest`). It does not attach
to arbitrary tabs, and captured data goes only to the local backend over
localhost with the user-pasted token.

## The Tauri shell

Capabilities are minimal (`src-tauri/capabilities/default.json`): no shell
plugin, no filesystem plugin; outbound HTTP from the webview is limited to
`api.github.com` (update check). The webview CSP is strict
(`src-tauri/tauri.conf.json`) — `script-src 'self'`, no frames, no
external hosts.

## CI gates

Every PR runs (`.github/workflows/security-scan.yml`,
`dependency-audit.yml`):

- **bandit** (Python SAST), **semgrep** (JS/TS + cross-language),
  **cargo clippy** — each gated on a committed baseline: pre-existing
  findings stay visible, **new** findings fail the build.
- **pip-audit / npm audit / cargo audit** — dependency CVE scans, weekly
  and on every PR.
- **personal-data deny-list** — this repo is public and must contain no
  real person, customer, or meeting data. The deny-list is salted hashes,
  not plaintext (`scripts/ci/personal-data-terms.json`), so the list
  itself does not republish what it blocks. See `AGENTS.md` for the
  policy and the incident that created it.

Scanner versions are pinned; baseline refreshes are deliberate,
reviewable commits (`docs/ci-security-and-ai-review.md`).

## Known limitations

Honesty section — these are accepted, not unknown:

- **Unsigned builds.** The macOS app is not notarized (Gatekeeper bypass
  documented in every release note) and the Windows installer is
  unsigned. Signing is on the roadmap; until then, verify the SHA-256
  digests GitHub publishes per release asset.
- **The token authorizes everything.** There is one token and every
  endpoint trusts it equally; there are no per-scope permissions. Anyone
  who has it (or local malware that reads the extension's
  `chrome.storage`) can do what the app can do.
- **Git history predates the 2026-08 scrub.** The working tree is clean
  and CI keeps it that way, but history from before the scrub is what it
  is. Treat old commits accordingly.
