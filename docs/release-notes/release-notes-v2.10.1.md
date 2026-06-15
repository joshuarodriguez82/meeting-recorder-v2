# Meeting Recorder v2.10.1

Polish + fixes for the v2.10 Today view, plus auto-record pattern feedback. The Today/Daily Briefing tab is now **opt-in** — it stays out of the way unless you turn it on.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.10.1_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
> 2. Drag into `/Applications`.
> 3. Double-click. Dismiss the "damaged" warning.
> 4. **System Settings → Privacy & Security**, scroll to bottom, click **Open Anyway**.
> 5. Double-click again, click **Open**.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_*_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> Windows users — just install the `.msi` or `.exe`.

---

## What changed

### Today / Daily Briefing is now opt-in

The Today tab assumes a power-user setup — a daily Microsoft 365 Copilot scheduled prompt whose output you paste in. Not everyone runs that, so it's **off by default**.

- New persistent setting: **Settings → Today / Daily Briefing tab**. Off stays off, on stays on, across restarts.
- When off: no Today tab in the sidebar, app lands on Record.
- When on: Today tab appears at the top of the sidebar and becomes the default landing view.
- Toggling reflects immediately — the tab appears/disappears without a restart.

### Today view fixes

- **Import dialog no longer blows off-screen.** A long pasted briefing used to expand the modal past the viewport and hide the Parse button. The dialog is now fixed-height with an internally-scrolling textarea; Cancel/Parse stay pinned.
- **Briefing parse uses your real AI provider.** The import previously hardcoded Anthropic and 404'd for users whose live model was a non-Claude name (e.g. `llama3.1` on Ollama/OpenRouter). It now uses your main provider (with the live provider as fallback) — briefing parsing is quality work, so it routes to your main model like post-meeting summaries do.
- **Removed clipboard auto-fill.** The import dialog used to silently dump whatever was on your clipboard into the textarea. It now starts empty; paste with Ctrl+V.

### Auto-record skip patterns now show on meeting tiles

If a Settings substring pattern (e.g. `canceled`) matches a meeting's title, the Record view tile now shows **"Skipped (pattern)"** in amber instead of a plain "No auto" button. Previously the backend honored the pattern but the UI gave no indication, making the feature look broken. Clicking a pattern-blocked tile explains it's governed by a Settings pattern (edit it there) rather than adding a redundant per-meeting block.

### Pre-meeting Brief context box

The **Brief** modal (on each upcoming meeting) now shows its "Add context" box at the top on open, instead of hiding it until after the first brief generates. Drop in things the invite and meeting history can't see (exec asks, procurement redlines, a customer mood shift) and hit **Regenerate with context**. First brief still auto-generates one-click. Mirrors the Prep Brief tab's context input.

---

## What didn't change

- All v2.9.x defense layers (orphan-kill, deadman switch, watchdog timer, capture-stall detector, WAV integrity, ghost-session audit) — active.
- Recording, transcript, summarization, live co-pilot — unchanged.
- Existing sessions / settings / config — preserved on upgrade.

---

## Upgrade notes

- **Windows** — `Meeting Recorder_2.10.1_x64-setup.exe`. Installs over your existing version.
- **macOS** — see the install block at the top.
- **No new dependencies**, no bootstrap changes.
- **Today tab**: if you used it in v2.10.0, it's now off by default — re-enable it in Settings → Today / Daily Briefing tab. Your imported briefings are preserved on disk.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
