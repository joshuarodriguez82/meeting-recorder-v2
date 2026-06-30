# v2.14.0 — Outlook Web sync for the Today tab + Live Co-Pilot parity

> **What this release adds:**
>
> 1. **Outlook Web sync for the daily brief.** Today's calendar
>    auto-pulled from outlook.office.com via your installed Chrome —
>    no more pasting Microsoft Copilot scheduled-prompt output by
>    hand. Works on personal machines where IT has blocked Graph /
>    calendar API access (web access still works). Two new buttons
>    on the Today tab: **Sync now** and **Sign in to Microsoft**.
> 2. **Live Co-Pilot parity with the main AI Provider.** The Live
>    Co-Pilot section in Settings now has the same six-preset
>    switcher (Anthropic / Groq / Gemini / OpenRouter / Ollama /
>    Custom), the same live model-discovery dropdown, and the same
>    Test connection button — no more typing model IDs by hand.
> 3. **Stale Gemini preset label fixed.** The main AI Provider
>    dropdown's Gemini entry used to say "(Gemini 2.0 Flash)", which
>    made it look like Gemini support was capped on 2.0 even after
>    v2.13.0 added the 2.5 family. Now says "(Gemini 2.5 Flash,
>    2.5 Pro, …)".

This release is pure quality of life — no recording-pipeline changes,
no data-loss fixes. Just shaving daily friction off the Today tab and
bringing the Live Co-Pilot section to feature parity with the main
provider config.

## Install (macOS)

> v2.14.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.14.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.14.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.14.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's new

### 1. Outlook Web sync for the daily brief (the headline)

The Today tab's daily briefing used to depend on you pasting the
output of a Microsoft 365 Copilot scheduled prompt every morning.
Two things broke that:

1. **IT started blocking Graph / calendar API access on personal
   machines.** Killed any "real" Outlook integration we could ship.
2. **Copilot's scheduled prompts became unreliable** — running at
   the wrong time or not at all on some days.

You can still log in to `outlook.office.com` via Chrome on your
personal machine — that path is allowed. v2.14.0 uses your existing
authenticated browser session as the data path.

**How it works.** A new backend service drives your installed Chrome
via Playwright (`channel='chrome'`, no bundled Chromium — keeps the
installer lean) against a persistent profile at
`<recordings_dir>/web-session/`. Two new buttons on the Today tab:

- **Sync now** — opens Chrome headlessly, navigates to
  `outlook.office.com/calendar/view/day`, extracts the day-view's
  text, and feeds it through **the same LLM parser** the manual
  paste-import already uses. Lands in the same `DailyBriefingService`
  store, renders in the same Today view, supports the same
  action-item checkboxes. No second Today view, no parallel storage.
- **Sign in to Microsoft** — opens a HEADED Chrome window at OWA so
  you can sign in or re-MFA. Closes itself when you close the
  window. The persistent profile keeps cookies across launches;
  weekly MFA re-auth is the expected cadence given typical M365
  conditional-access policies.

**Auth-expired UX.** When the persistent session's cookies expire
(every ~7 days), `Sync now` returns **423 LOCKED** instead of a
generic error. The Today tab catches that and renders an amber
banner — "Microsoft 365 sign-in expired — Sign in" — with the
sign-in button wired to the same flow. So the failure mode is
self-healing in 10 seconds: tap the Authenticator prompt, close the
window, click Sync now again.

**No fixed-time cron.** v2.14.0 deliberately does NOT auto-sync at
7am — your machine isn't on at 7am. Sync is on-click only. (A
follow-up release may add "auto-sync on first launch of the day"
once the manual path is proven.)

**The existing manual Import briefing button stays** as a fallback
for the day Microsoft inevitably changes OWA's DOM and the scraper
needs a tune-up.

### 2. Live Co-Pilot model card now matches the main AI Provider

Before this release, Settings → Live Co-Pilot model was a compact
override panel with text inputs for the model id + base URL and
only two provider-family choices ("anthropic" or "openai-compat").
Now it has full parity with the main AI Provider section:

- **Same six-preset switcher** — Anthropic, Groq, Gemini, OpenRouter,
  Ollama, Custom. Picking a preset auto-fills the base URL and a
  sensible default model id.
- **Same live model-discovery dropdown** — no more typing
  `llama-3.1-70b-instruct:free` by hand. Calls
  `GET /providers/available-models?scope=live` so the backend reads
  your `live_*` settings keys (the override account/key, not the
  main one) and surfaces every current model from your provider.
- **Same Test connection button** — fires a 1-token chat completion
  against the LIVE summarizer specifically (`scope=live` in the
  body). Emerald card with latency + model + reply on success; red
  card with the verbatim error on failure.
- **Same per-preset help text** — "Get a Groq key at
  console.groq.com", "Gemini API key" label vs "Groq API key" label,
  etc.

The "Use a different model for live ticks" toggle still gates
everything; turning it off clears the `live_*` settings the same way
it did before. Existing user configs continue to work.

### 3. Stale Gemini preset label fixed

The main AI Provider dropdown's Gemini entry used to read:

> Google Gemini — free tier (Gemini 2.0 Flash)

That text was hardcoded UI copy that never got updated when v2.13.0
added the 2.5 family (2.5 Flash, 2.5 Flash-Lite, 2.5 Pro). It made
it look like Gemini support was capped on 2.0 — which it wasn't;
just the preset's pitch line was wrong. Now reads:

> Google Gemini — free tier (Gemini 2.5 Flash, 2.5 Pro, …)

The actual model dropdown inside Gemini was already correct from
v2.13.0; only the preset label needed updating.

## Backend additions

Both v2.13.0 QoL endpoints gained an optional `scope` so the Live
Co-Pilot card can probe its own config without affecting the main
summarizer:

- `GET /providers/available-models?scope=live` reads
  `live_anthropic_api_key` / `live_openai_api_key` /
  `live_openai_base_url` instead of the main keys. Cache key gains
  the scope so main + live caches don't poison each other.
- `POST /diagnostics/llm-test` accepts `{scope?: "main" | "live"}`
  in the body. `scope=live` routes to `svc.live_summarizer`.

Defaults preserve existing call shape — anything that doesn't pass
`scope` keeps working as v2.13.0 did.

New scraper service:

- `backend/services/outlook_web_scraper.py` — drives Chrome via
  Playwright with a persistent profile; lazy import so the rest of
  the backend keeps working if Playwright isn't installed yet.
- New endpoints `POST /briefing/signin` and `POST /briefing/sync`.
  Shared `asyncio.Lock` serializes the two so Chrome's
  profile-dir lock doesn't error on overlapping clicks.

New backend dependency:

- `playwright>=1.40` in both `requirements-cpu.txt` and
  `requirements-mac.txt`. **No bundled Chromium** —
  `channel='chrome'` uses your installed Chrome. First launch after
  upgrade adds ~10-30 seconds to the venv bootstrap as pip pulls
  Playwright in.

## Tests

- Total backend test count: **64** (was 45 in v2.13.0).
- 19 new in `test_outlook_web_scraper.py` covering pure helpers,
  error class hierarchy, lazy-import contract, profile directory
  placement, login-URL detection, and the free-form briefing
  formatter that joins OWA text with action items.
- All run on every PR.

## Bundle changes

None. The new scraper service is pure Python and ships under the
existing `services/` allowlist in `zip-bundle.py`. Playwright pulls
itself in via the venv bootstrap at first launch.

## Known not yet patched

Carried forward from v2.13.0:

- **Subprocess-isolated transcribe + diarize** — deferred (needs
  persistent worker; naive subprocess regresses startup latency by
  10-30s per recording).
- **RecordingService decomposition** — 1,300-line god-object remains.
- **macOS Bluetooth audio-format-mismatch banner** — Windows-only.
- **Subprocess timeout on finalize child** — `subprocess.run` has no
  timeout; a stuck child hangs the parent's stop path.

Specific to the new Outlook Web sync:

- **No auto-sync on launch** — Sync is on-click only. The "first
  launch of the day fires Sync once automatically" follow-up is
  tracked but not in this release.
- **No Teams Web extraction** — calendar only. Teams chats and
  @mentions are a follow-up; calendar covers ~80% of the daily
  brief's value.
- **No action-item join** — the formatter helper already accepts
  open action items from the recorder's commitments service; the
  backend doesn't yet query and pass them through. Half-day add for
  a follow-up.
- **No fallback when Chrome is missing** — Sync returns 503 with an
  actionable message. We could ship a "Install Chrome" dialog
  later; today the toast carries enough info.
