# v2.0.15

Docs-only release. In-app Usage Guide (Help tab) rewritten to cover everything that landed in v2.0.14:

- **Getting Started** — first-run setup now explains the AI Provider choice (Anthropic / OpenRouter / Ollama / Custom OpenAI-compatible) instead of assuming Anthropic. HuggingFace token is still required; Anthropic key is now optional.
- **Recording** — notes that the Upcoming Meetings panel shows 7 days (up from 36h) and that the Project autocomplete is scoped to the selected client.
- **New section: AI Provider & Models** — detailed walkthrough of each provider option, where to get keys, how to set up Ollama locally, free-tier limits on OpenRouter.
- **Knowledge Base** — describes the Open Recordings Folder and Load Session buttons in the Sessions view.
- **Clients & Projects** — describes the Designated Folder card, the Browse… folder picker, and auto-export behavior.
- **Workflow Automation** — corrects the startup-shortcut note (v2.0.14 uses in-process COM, not a suppressed PowerShell spawn).
- **Cost** — breaks out per-provider cost expectations (paid Claude, free OpenRouter, free Ollama).
- **Troubleshooting** — updates the "calendar shows no meetings" entry for the 7-day window, adds a new "Summarization API call failed" entry covering the new provider options.

No code behavior change.
