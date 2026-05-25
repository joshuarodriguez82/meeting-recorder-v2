# Production Readiness Checklist

Meeting Recorder is moving from a personal tool to a paid product. This document tracks what needs to be true before charging customers, organized by category and severity.

**Severity legend**
- **P0** — Launch blocker. Don't take a single paying customer without this done.
- **P1** — Before paid GA. Can soft-launch to a friends-and-family alpha without it, but not bill customers.
- **P2** — Post-launch hygiene. Important but not urgent.

Status uses `[ ]` = not started, `[~]` = in progress, `[x]` = done.

---

## 1. Distribution & code signing

- [ ] **P0 — macOS code signing + notarization.** Apple Developer ID Application certificate ($99/yr). Replaces the ad-hoc codesign step in `.github/workflows/release.yml:163`. Removes the Gatekeeper "damaged" warning entirely; the user's first-launch flow becomes "double-click, click Open." Notarization gives Apple's malware-scanner approval — without it, the OS shows "developer cannot be verified" on every machine.
- [ ] **P0 — Windows EV codesigning certificate.** ~$300–500/yr. Avoids SmartScreen "unrecognized app" warnings that scare users. The MSI/NSIS installer needs to be signed with `signtool` after the Tauri build.
- [ ] **P0 — Remove the Gatekeeper-bypass instructions from release notes.** AGENTS.md mandates them today because the build is unsigned. Once signed+notarized, those instructions become misleading and the AGENTS.md block can come out.
- [ ] **P1 — Auto-updater signing key.** Tauri's updater plugin already verifies signatures. Set up a long-lived signing key for update manifests, document the rotation procedure.
- [ ] **P2 — Linux distribution.** AppImage + DEB/RPM. Only matters if you have Linux customers; defer until demand exists.

## 2. Security

- [ ] **P0 — Secret storage.** `config.env` holds Anthropic / HuggingFace / OpenAI keys in plaintext as the source of truth, with a keychain mirror (see `backend/config/settings.py:330` for the rationale). Acceptable for a single-user tool, problematic for paid customers on shared machines. Decision: keep file as authoritative + restrict permissions (chmod 600 on POSIX, ACLs on Windows), OR move authoritative storage into OS keychain and let the file go.
- [ ] **P0 — Input validation on the new Co-Pilot endpoints.** `live_openai_base_url` is user-supplied and gets passed straight to the OpenAI SDK as `base_url` (server.py around line 644). A malicious base URL could exfiltrate transcript content. Restrict to HTTPS + a known-good allowlist OR show a "this points outside localhost — confirm" UI gate.
- [ ] **P0 — File path validation.** Endpoints like `/sessions/{id}/audio` and `/sessions/{id}/screenshots/{i}` serve files from disk. Audit that the session_id can't traverse paths (`../../etc/passwd`) and that file extension/MIME enforcement is in place.
- [ ] **P1 — Dependency vulnerability scan.** Run `npm audit`, `pip-audit` (Python), and `cargo audit` (Rust) on every release. Fail CI on HIGH or CRITICAL findings. Set up Dependabot.
- [ ] **P1 — `/security-review` skill against `main`.** Built into this Claude Code setup. Targeted second pass after `/ultrareview` covers the breadth.
- [ ] **P1 — CSP for the Tauri WebView.** Restrict what the frontend can fetch — no `connect-src *`. Especially important once arbitrary live-model base URLs are configurable.
- [ ] **P2 — At-rest encryption for stored recordings.** Audio files + transcripts sit on disk unencrypted. OS-level disk encryption is the user's responsibility, but for sensitive verticals (legal, medical) you may want per-session encryption with a user-managed key.
- [ ] **P2 — Audit logging.** Who accessed what session and when. Not in scope today but customers in regulated industries will ask.

## 3. Licensing, auth, billing

- [ ] **P0 — License-key validation OR sign-in.** Nothing in the code today distinguishes a paying customer from a free one. Pick one:
  - **Offline license keys** (simplest): user pastes a key, app validates locally against a signed manifest. Cheap, no server. Easy to crack.
  - **Online auth** (sturdier): app calls a license server on launch + periodically. Detects sharing. Needs you to run a server + handle outages gracefully.
- [ ] **P0 — Billing integration.** Stripe or Paddle. Webhook handler updates license validity.
- [ ] **P1 — Trial-period mechanics.** N-day full-feature trial, then degrade to free tier (or block entirely). Needs the license layer above.
- [ ] **P1 — Tier enforcement.** If you plan tiers (free / pro / team), each tier's gates need to live somewhere checkable from the app. The simplest path is per-feature flags in the license manifest.

## 4. Operational

- [ ] **P0 — Crash reporting.** Sentry, Rollbar, or similar. Without it you won't know when a customer's app dies — they'll just stop using it and you won't know why.
- [ ] **P0 — Hardened release process.** The v2.7.5 / 2.7.6 / 2.7.7 saga (three releases tagged at the same stale commit) revealed that the tag-driven workflow doesn't verify what's in the tagged tree. Add a CI step at the top of `release.yml` that fails fast if `tauri.conf.json.version` doesn't equal `${{ github.ref_name }}` with the leading `v` stripped. One-line check, prevents the entire class of bug. Codified the data-first diagnosis rule in AGENTS.md so future Claude sessions don't waste turns guessing at local state.
- [ ] **P1 — Anonymized usage telemetry.** Opt-in. What features get used, where users drop off, recording lengths. Drives roadmap decisions. PostHog, Plausible, or similar privacy-respecting analytics.
- [ ] **P1 — Status page.** Even a static one. When the license server / Anthropic API / OpenRouter / whatever is down, customers need a place to check.
- [ ] **P1 — Incident response runbook.** What do you do when (a) the Anthropic key in your secret store gets leaked, (b) a customer reports a crash you can't reproduce, (c) a malicious update gets signed and pushed.
- [ ] **P2 — Backup / DR plan for the license server** (if you go online). Database backups, recovery time objective, etc.

## 5. Code quality

- [ ] **P0 — Run `/ultrareview` on `main`.** Built into this Claude Code setup, multi-agent cloud review. Catches architecture issues, dead code, performance pitfalls, bug risk across the whole branch. Triage findings before launch.
- [ ] **P1 — Fix the lingering ESLint errors flagged on every recent PR.** `react-hooks/set-state-in-effect` violations in `record-view.tsx`, `settings-view.tsx`, `known-speakers-section.tsx`, `session-detail-dialog.tsx`. They were pre-existing during the Co-Pilot PRs — fix them in a dedicated cleanup PR.
- [ ] **P1 — Test coverage.** No `pytest` / `vitest` coverage report exists today. Add coverage to CI, set a floor (60%? 70%?), block PRs that drop it. Focus tests on the audio / transcription / persistence layer first — that's where data-loss bugs live.
- [ ] **P1 — TypeScript `strict: true`.** Confirm `tsconfig.json` is strict and that nobody's silenced errors with `// @ts-ignore`. Grep the codebase.
- [ ] **P2 — Rust `clippy` clean.** `src-tauri/` should lint clean. Add to PR checks.

## 6. Reliability

- [ ] **P0 — Backend crash recovery.** What happens if the Python backend dies mid-recording? Does the user lose audio? The current `_finalize_recording_streaming` writes incrementally to the WAV — confirm that a hard crash leaves a recoverable file. Test it.
- [ ] **P0 — Long-meeting stress test.** 4-hour meetings hit the `hard_cap_hours` watchdog, but the live-transcription pipeline, the rolling-segment buffer for Co-Pilot (capped at 2000 segments, see `live_transcriber.py`), and the post-stop processing have all been exercised on shorter calls. Run an 8-hour empty-room recording to surface any leaks or queue overflows.
- [ ] **P1 — Stop-button reliability.** The Phase A work shipped a fix for "Start button disappeared after Stop until tab switch." Regression-test the Stop path under: (a) immediate restart, (b) network failure during processing, (c) backend OOM during transcription.
- [ ] **P1 — Whisper / pyannote model integrity.** First-launch downloads ~200MB to the user's machine. Failures during the download leave the app in a broken state. Verify the retry + checksum logic.

## 7. UX polish

- [ ] **P1 — Onboarding flow.** First-launch right now expects the user to know to paste an Anthropic key + HuggingFace token. Most paid customers won't. Either bundle a working OpenRouter free-tier key for the trial, OR build a guided "paste your keys / sign up for a free Anthropic account" flow.
- [ ] **P1 — Error messages users can act on.** Audit FastAPI's HTTPException detail strings — they're often developer-facing ("409 detail: ...") and confuse end users.
- [ ] **P2 — Accessibility audit.** WCAG AA at minimum. Keyboard nav, screen-reader labels on the Switch/Button primitives, focus management in dialogs.
- [ ] **P2 — i18n.** Hard-coded English copy everywhere today. Worth lifting into a strings file before any localization work, even if you only ship English at launch.

## 8. Documentation

- [ ] **P0 — Privacy policy.** What audio do you collect, where does it go, who has access. Legally required for paid product.
- [ ] **P0 — Terms of service / EULA.** Same.
- [ ] **P1 — User-facing docs.** The in-app Usage Guide is good but customers will Google before installing. A docs site (Mintlify, Docusaurus, or just a few GitHub Pages markdown files) is table-stakes.
- [ ] **P1 — Refund / cancellation policy.**

## 9. Pre-launch checklist

Once everything above is done:

- [ ] Run `/ultrareview` + `/security-review` once more on `main`.
- [ ] Cut a release candidate (`v3.0.0-rc.1`). Don't tag from the working copy — push tag with explicit SHA per the diagnose-with-data rule in AGENTS.md.
- [ ] Hand the RC to 5–10 friendly customers for a week.
- [ ] Run the long-meeting stress test against the RC.
- [ ] Verify the auto-updater path from `v2.x.x` (current public) → `v3.0.0` works for every existing user.
- [ ] Roll out the production v3.0.0 release.
- [ ] Have Sentry / status page / billing webhooks all proven working before the first paid signup hits.

---

**Reviewer:** maintainer.
**Last updated:** see git log of this file.
