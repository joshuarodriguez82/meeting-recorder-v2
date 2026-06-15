# v2.11.0 — Backend auth + WebView CSP + updater CORS fix + CI release gate

Security-first minor release. Four user-visible changes plus a CI
hardening that pays back the v2.7.5/v2.10.5 botched-tag class of bug.

1. **Critical security** — backend sidecar is now authenticated with a
   per-launch random token. Closes a real drive-by exfiltration hole
   on any machine where the user visits an attacker-controlled webpage
   while Meeting Recorder is running.
2. **Defense-in-depth** — Content Security Policy on the WebView,
   replacing Tauri's `csp: null` default.
3. **Update check fix** — `Settings → App Updates → Check Now` works
   again. The v2.10.6 build was hitting a CORS wall against GitHub
   from the WebView; the new build routes the call through Rust where
   CORS doesn't apply.
4. **CI release gate** — a new `verify-version` job aborts any release
   whose tag doesn't match `tauri.conf.json.version`. The 2.7.5–2.7.7
   class of "release page says X, artifacts built from Y" is closed
   mechanically.

## Install (macOS)

> v2.11.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.11.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.11.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.11.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's new

### 1. Per-launch backend auth token

The Python sidecar binds to `127.0.0.1`, but **localhost is not an
auth boundary**. Any browser tab the user opens (`example.com`,
`reddit.com`, a malicious ad iframe) can fetch
`http://127.0.0.1:17645/sessions` and read transcripts, kick off
recordings, or pull commitments. Before v2.11.0 the sidecar accepted
every request unauthenticated and CORS was `allow_origins=["*"]`.

The fix:

- **CORS narrows** from `*` to the legitimate Tauri origins
  (`tauri.localhost` on Windows, `tauri://localhost` on macOS/Linux)
  plus the Next dev server. First defense; not the boundary.
- **Per-launch shared secret.** The Tauri shell draws 32 bytes from
  the OS CSPRNG at launch, hex-encodes them, and stashes the result
  in memory. The sidecar gets it via the `MEETING_RECORDER_TOKEN` env
  var (parent → child only, never on the wire); the WebView gets it
  via a Tauri IPC command. Neither channel is reachable from a
  foreign webpage.
- **Every request is checked.** Python validates with
  `secrets.compare_digest` (timing-safe). `/health` stays exempt for
  the liveness probe. The WebView attaches the token via
  `Authorization: Bearer …` on fetch paths, and `?token=…` on
  EventSource and `<audio>`/`<img>` `src` URLs (which can't carry
  headers). The query-string consumers leave the token in uvicorn's
  local access log — a trade-off we accept because the alternative is
  no audio playback.

After upgrading, opening `https://example.com` in your normal browser
and running `fetch('http://127.0.0.1:17645/sessions')` from the
console returns `401 Unauthorized` instead of your session list.

### 2. WebView Content Security Policy

`csp: null` (Tauri's default) means any XSS in the frontend — say, a
compromised dependency tomorrow — has free rein to read whatever the
WebView can. The new auth token closes the wire to the sidecar; CSP
limits the blast radius of in-WebView code.

Production policy:

- `default-src 'self'` — only bundled assets
- `connect-src` adds `ipc:` + `http://ipc.localhost` (Tauri IPC), the
  sidecar at `127.0.0.1:*`, and `api.github.com` for the updater
- `img-src` + `media-src` allow `127.0.0.1:*` (audio playback,
  screenshot grid) and `data:` (inline previews)
- `style-src 'self' 'unsafe-inline'` — the Tailwind tax; runtime
  style injection is unavoidable without a nonce pipeline
- `script-src 'self'` — no inline JS, no eval
- `object-src` + `frame-src 'none'`, `base-uri`/`form-action 'self'`

Dev variant keeps Next HMR alive (`ws://localhost:3000`,
`'unsafe-eval'`, `'unsafe-inline'` for `script-src`). Production
never carries those.

### 3. Update check no longer fails with "Failed to fetch"

Field repro on v2.10.6: `Settings → App Updates → Check Now`
returned *"Network error: Failed to fetch."* DevTools console
revealed the actual reason — **CORS**, not the new CSP:

```
Access to fetch at 'https://api.github.com/.../releases/latest' from
origin 'http://tauri.localhost' has been blocked by CORS policy:
No 'Access-Control-Allow-Origin' header is present on the requested
resource.
```

GitHub *does* return `Access-Control-Allow-Origin: *` on a plain GET
(`curl` confirms), but the WebView's preflight goes down a different
code path GitHub doesn't satisfy. This was a pre-existing v2.10.6
issue independent of the new CSP — the CSP's `connect-src` already
allowed `https://api.github.com`.

Fix: the updater now goes through `tauri-plugin-http`, which proxies
the request via Rust's `reqwest` instead of the WebView. CORS doesn't
apply to Rust-side HTTP. The plugin is scoped to
`https://api.github.com/*` only via the Tauri capability file — the
WebView can't ask it to reach arbitrary URLs.

This was the wrong layer for the call all along. Now fixed.

### 4. Release gate — tag and version must match

A new `verify-version` job runs before the build matrix and fails
fast if the pushed tag (e.g. `v2.11.0`) doesn't match
`tauri.conf.json.version` on the commit the tag points at. The
v2.7.5/v2.7.6/v2.7.7 series tagged the same stale commit three times
in a row before anyone noticed; that whole class of bug is now
caught in ~30 seconds on the cheapest GitHub-hosted runner before any
artifact gets built.

### Quiet improvements

- **Backend test suite in CI** — `pytest backend/tests` now runs on
  every PR with numpy/scipy/soundfile/pytest (light deps; ~17 s job
  including install). 22 tests pin the data-loss surface:
  `finalize_recording_streaming` (audio merge for live stop AND crash
  recovery), `recovery_service` (the "failed merge keeps temps on
  disk" invariant), `session_service` (round-trip + atomic write +
  sidecar-file skip). Heavier ML tests stay out of this job.
- **Repo cleanup** — 47 historical release-notes files moved from
  the repo root to `docs/release-notes/`. Design mockups to
  `docs/design/`, `HANDOFF.md` to `docs/history/`. The release
  workflow's `body_path` updated to match. Five dead Next.js
  scaffold SVGs removed from `public/`.

## Upgrade notes

- **All existing sessions, settings, and configs are preserved.** No
  schema changes, no folder moves, no Python deps re-installed.
- **First launch** of v2.11.0 generates a new backend auth token
  automatically. You won't see a prompt.
- **If "Check Now" still fails after upgrading**, open DevTools
  (right-click in the app → Inspect → Console) and grab the error
  text. It should NOT say "blocked by CORS policy" any more.

## Known not yet patched

- **CSP smoke-test coverage** — CI runs `npm run build` and
  typechecks, but doesn't drive the packaged Tauri WebView through
  every view. If a corner of the UI emits something the new CSP
  blocks at runtime (an inline `<script>`, a `data:` URL in
  `script-src`, etc.), it will show up only after install. Report
  any DevTools CSP violations and a hotfix can land same-day.
- **Audio-format mismatch banner on macOS / Linux** — same as
  v2.10.6: `IAudioClient::GetMixFormat` is Windows-only. CoreAudio
  HAL queries tracked for a follow-up.
