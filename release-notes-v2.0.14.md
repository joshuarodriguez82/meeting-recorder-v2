# v2.0.14

## Highlights

- **Calendar no longer looks broken on weekends.** Default window bumped from 36h to 168h (7 days) so late-Friday through Sunday doesn't show an empty panel.
- **Folder picker for per-client export folders.** The Designated Folder field in the Clients view now has a Browse… button that opens a native Windows folder dialog instead of requiring a pasted path.
- **Free AI models via OpenRouter, Ollama, and any OpenAI-compatible endpoint.** New AI Provider dropdown in Settings with presets for:
  - **Anthropic** — Claude Haiku/Sonnet/Opus (existing behavior, unchanged)
  - **OpenRouter** — free-tier Llama 3.3 70B, Gemini 2.0 Flash, Qwen 2.5 72B, DeepSeek R1, Mistral Small
  - **Ollama** — local, offline, no API key required
  - **Custom** — any OpenAI-compatible endpoint (LM Studio / vLLM / Groq / Together / LocalAI / etc.)

## Fixes

- **Calendar panel no longer "flashes away" when switching nav.** Meeting list is now owned by the app shell so moving between Record/Sessions/etc. doesn't drop the loaded list. Silent auto-refresh on window focus keeps the prior list on transient Outlook hiccups instead of blanking.
- **Black CMD window flash on launch-on-startup toggle eliminated.** Startup shortcut now uses win32com COM automation directly instead of shelling out to `powershell.exe`. Also removes an AV-kill vector on locked-down corporate laptops.
- **Project dropdown is scoped to the selected client.** You can no longer accidentally tag a SimpliSafe meeting with an Acme project — the Project autocomplete in Record view and the Session Detail dialog only lists projects that were previously tagged under the currently-selected client.

## Features

- **Open Recordings Folder** button (Sessions view) — opens `%LOCALAPPDATA%\MeetingRecorder\recordings` (or your configured path) in Explorer.
- **Load Session** button (Sessions view) — import an external WAV/MP3/M4A/FLAC file as a new session. Copies into the recordings folder and creates the JSON shell; transcription runs the same way as any other session.
- **Designated Folder per client** — after tagging a meeting to a client with a designated folder, transcripts / summaries / action items / decisions / requirements auto-copy there (and the WAV too on Stop Recording or Load Session). No more manual file shuffling.

## Internals

- `Summarizer` now provider-agnostic — one `_chat()` helper dispatches to either the Anthropic SDK or `openai.AsyncOpenAI` depending on the `ai_provider` setting.
- New settings: `ai_provider`, `openai_api_key`, `openai_base_url`. Missing fields default to `anthropic` for backward compatibility.
- `openai` Python SDK added to `requirements-cpu.txt` (lazy-imported, so Anthropic-only users pay no import cost).
