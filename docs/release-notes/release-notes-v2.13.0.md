# v2.13.0 — Live model discovery + Gemini 2.5 + test-connection button

> **What this release adds:**
>
> 1. **Live model discovery.** When you open Settings → AI, the model
>    dropdown now queries your provider's `/models` endpoint and
>    populates with whatever they currently expose. No more app updates
>    when Anthropic ships a new Claude, Google ships a new Gemini, or
>    your OpenAI-compat provider adds a model. The hardcoded list stays
>    as a fallback for the offline / no-key path.
> 2. **Gemini 2.5 in the fallback list.** Until the live fetch returns,
>    the dropdown shows 2.5 Flash (recommended), 2.5 Flash-Lite, and
>    2.5 Pro. 2.0 / 1.5 entries are kept and labeled **(legacy)**.
> 3. **Test connection button.** Settings → AI now has a button that
>    fires a 1-token chat completion against whichever provider + model
>    + key you have configured and reports back: latency, the model
>    that actually answered, and either the reply or the verbatim
>    error. 10-second timeout so a misconfigured base URL or wrong key
>    fails fast instead of hanging the page.

No data-pipeline or recording changes in this release. Strictly settings-side quality of life.

## Install (macOS)

> v2.13.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.13.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.13.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.13.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's new

### 1. Live model discovery (the headline)

Every prior release that shipped a new model — Claude 4.6, Gemini 2.5,
the next Llama — required us to edit a hardcoded list in
`settings-view.tsx` and cut a build. Users got the new model on the
next install, which for a desktop app is days, not minutes. That was
backwards. The provider already knows what they expose; we should ask
them.

v2.13.0 adds a backend endpoint `GET /providers/available-models` that
fetches from whichever provider's `/v1/models` shape applies, normalizes
to `[{value, label}]`, and caches per-`(provider, base_url)` for 5
minutes so opening Settings doesn't hammer the upstream. Four adapters
ship:

| Provider | Endpoint | Label format |
|---|---|---|
| Anthropic | `https://api.anthropic.com/v1/models` (`x-api-key` + `anthropic-version: 2023-06-01`) | `display_name` (e.g. "Claude Sonnet 4.6") |
| Gemini (native) | `https://generativelanguage.googleapis.com/v1beta/models?key=...` | `displayName`, filtered to entries that support `generateContent` |
| OpenAI-compat (Groq, OpenRouter, Together, etc.) | `{base_url}/models` (`Authorization: Bearer …`) | `id` or `id · owner` when `owned_by` is present |
| Ollama (local) | `{host}/api/tags` | `name · X.X GB` so you know what's eating disk |

The settings UI calls this on mount + whenever you change provider /
preset / base URL. If it returns a list, that's what populates the
dropdown. If it returns empty or errors, the hardcoded fallback list
takes over — the UI is never worse than it was. Errors are logged but
silent in the UI, exactly like v2.12.0's auto-screenshot behavior:
best-effort, no toast spam.

Specifically: when you set up Anthropic with a key and reload Settings,
you should see *every* current Claude in the dropdown (including any
released between this build and you reading this) instead of whatever
was hardcoded the day we shipped. Same for Gemini, Groq, OpenRouter,
LM Studio, Ollama.

### 2. Gemini 2.5 in the hardcoded fallback list

The fallback list (what you see before / instead of the live fetch)
now leads with the 2.5 family:

- **Gemini 2.5 Flash** — recommended default. 1M-token context, fast.
- **Gemini 2.5 Flash-Lite** — cheaper, smaller context.
- **Gemini 2.5 Pro** — best reasoning, slower.
- Gemini 2.0 / 1.5 entries are kept and labeled **(legacy)** so older
  configs don't break.

When the live fetch succeeds it overrides this list anyway, but the
fallback is what new users see on the very first key-paste before the
network round-trip lands.

### 3. Test connection button (Settings → AI)

Wiring up an API key + base URL has always been silent — you either
saw the summarizer work after the next recording or you didn't, and
the failure mode could be the key, the URL, the model name, the
network, the proxy, or the provider being down. The new
**Test connection** button removes the guesswork:

- Calls `POST /diagnostics/llm-test` with whatever's currently in the
  form (provider, base URL, model, API key).
- Backend fires a 1-token chat completion via the same code path the
  summarizer uses (`svc.summarizer._chat`) wrapped in
  `asyncio.wait_for(timeout=10s)`.
- UI renders an emerald card on success showing latency in ms, the
  model that actually answered, and the 1-token reply.
- On failure it renders a red card with the verbatim error message —
  401 means key, 404 means base URL or model name, `ENOTFOUND` /
  `ECONNREFUSED` means network, `asyncio.TimeoutError` means the
  upstream is slow or unreachable.

The probe is intentionally cheap — 1 token — so users can click it
freely while debugging configs.

## Bundle changes

None. No new bundled scripts, no allowlist edits, no Python dependency
adds. The model-fetch helpers are stdlib-only (`urllib.request`); no
`httpx` or `requests` added.

## Tests

Six new tests in `backend/tests/test_provider_model_fetch.py` pin the
shape of each adapter so a future provider-schema change is caught at
CI time:

- `test_anthropic_models_returns_id_and_display_name` — filters
  `type=="model"`, surfaces `display_name`.
- `test_anthropic_returns_empty_without_key` — missing key is not an
  error, the UI just falls back.
- `test_openai_compat_models_handles_owned_by` — appends owner when
  present, omits separator when not.
- `test_gemini_models_strips_model_prefix_and_filters_to_chat` —
  strips `models/` prefix, filters to `generateContent`-capable
  entries (excludes embedding models).
- `test_ollama_local_models_shows_size_in_gb` — labels with GB so
  the user knows disk impact.
- `test_empty_base_url_returns_empty_list` — no fetch attempted when
  there's nothing to fetch.

Each test stubs `_stdlib_get_json` with a captured response payload
from the relevant provider's documented schema; no live network calls
in CI. Total backend test count: **45** (was 39 in v2.12.0). All run
on every PR.

## Known not yet patched

Carried forward from v2.12.0 — nothing new added or removed:

- **Subprocess-isolated transcribe + diarize.** The v2.12.0 read-side
  local-copy fix addressed the actually-observed crash class
  (cloud-sync contention during file read). A native crash inside
  faster-whisper / pyannote during compute is still theoretically
  possible but never field-observed. Isolating it correctly requires
  a persistent worker (fresh subprocess per session would regress
  startup latency by 10-30s per recording); deferred to a later
  release.
- **RecordingService decomposition** — 1,300-line god-object remains.
- **macOS Bluetooth headset drift detection** — Windows-only banner.
- **Subprocess timeout on finalize child** — `subprocess.run` has no
  timeout; a genuinely-stuck child would hang the parent's stop path
  forever. Small fix, tracked for next release.
