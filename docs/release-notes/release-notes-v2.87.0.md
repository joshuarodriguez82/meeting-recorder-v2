# v2.87.0 — Claude Haiku 5.5, about 10× cheaper, and new Claude models work from Settings alone

## Install (macOS)

> v2.87.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.87.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.87.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.87.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## Claude Haiku 5.5

**Claude Haiku 5.5** is now in **Settings → AI Models** and is the
default for new installs. It costs **$0.10 per million input tokens and
$0.50 per million output tokens** (for requests up to 100K tokens) —
about a tenth of Haiku 4.5's $1 / $5. With 30-second check-ins, Live
Co-Pilot's estimate drops from about $0.71 to about $0.07 an hour.

An existing install keeps the model it has saved; pick **Claude Haiku
5.5** in Settings → AI Models to switch.

## New Claude models work without an app update

Claude's 5.x models (Haiku 5.5, Sonnet 5.5, Opus 5.5) can begin a reply
by thinking before they answer. The app read only the start of each
reply, so with any of them selected every summary, extraction and
Co-Pilot check-in would have come back empty. Now:

- **The answer is found wherever it is in the reply**, whether or not
  the model thought first.
- **The app asks Anthropic what the chosen model supports** and shapes
  each request to match, rather than relying on the model's name. A
  model released after this version works the same way.
- **The model list comes live from Anthropic**, so new models appear in
  Settings as they're released. **Custom…** still takes any model ID.
- **Thinking is kept light** on models that let the app choose, so cost
  and speed stay close to before. Advanced: set `AI_EFFORT` (`low`,
  `medium`, `high`, `xhigh`, `max`, or `default` to leave it to the
  model) in the app's settings file to change it.
- **A reply that runs out of room** while the model is still thinking
  is retried once with more room instead of failing.
- **A request the model declines** now says so, rather than producing
  an empty summary.

## Co-Pilot cost estimates

- Prices added for Haiku 5.5, Sonnet 5.5, Opus 5.5, Opus 5, Opus 4.6–4.8
  and Fable 5 / 5.1.
- **Opus 4.7 was over-estimated threefold** — it was priced at the old
  Opus 4 rate ($15 / $75); it's $5 / $25.
- For a model the app doesn't have a price for, Settings now asks for
  one (USD per million tokens, input and output) so the estimate still
  works.
