# v2.6.2 — API-key, model-id, and cloud-sync fixes

Fixes three real bugs that broke summarization and device sync, plus
the Android APK build. All v2.6.1 features are included.

## Install (macOS)

> v2.6.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.6.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.6.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.6.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Fixed since v2.6.1

- **"401 invalid x-api-key" on summarize / auto-process.** Saving
  settings routed the API key into the OS keychain and then *blanked*
  it from `config.env`. When the keychain entry later became
  unreadable (an unsigned app rebuilt with a new signature, a Windows
  Credential Manager entry written under a different context), the key
  resolved to empty and every Claude call failed with 401. The key is
  now always kept in `config.env` as the durable source of truth and a
  stale keychain can no longer shadow it.
- **"Claude Haiku 3.5" failed with 404 model not found.** The dropdown
  sent `claude-3-5-haiku-latest`, an alias the Anthropic API doesn't
  resolve. Fixed to the canonical `claude-3-5-haiku-20241022`, plus a
  settings-layer auto-heal so configs that already saved the dead id
  don't need to be re-picked by hand.
- **New clients / sessions not showing on another device.** A file
  synced via OneDrive/iCloud arrives as an online-only placeholder;
  the backend read it as empty and silently reported "no clients" /
  skipped the session. It now nudges the sync client to download the
  file and, if it genuinely can't, shows an actionable message instead
  of hiding your data. (Tip: right-click the synced recordings folder
  → **Always Keep on This Device**.)
- **OpenRouter free-model list went stale and 404'd.** Free model ids
  rotate constantly. The OpenRouter model dropdown now fetches the
  live free roster from OpenRouter directly, so it can't go stale
  again; the bundled list is only a no-network fallback.
- **Android companion APK failed to compile** (a private
  `startForeground` helper illegally shadowed `Service.startForeground`).
  Renamed so the sideloadable APK builds.
- Screenshots are now skipped for text-only Claude models (3.5 Haiku)
  instead of triggering a hard API error.

## Everything from v2.6.1 and v2.6.0

Working in-app Download, correct embedded version, auto-record/
auto-stop fixes, never-auto-record toggle, automatic speaker naming,
screenshots (capture + viewer + multi-monitor), auto-process on by
default, auto-refreshing Follow-Ups/Commitments/Decisions, click-to-
expand calendar meeting detail + one-click Join, resolved Exchange
attendee names, retention across client folders + orphans, app-wide
working external links, and calendar performance fixes.

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
- If your PC was hit by the 401 before upgrading: the fix applies
  going forward. After installing v2.6.2, open Settings, re-enter your
  Anthropic API key once, and Save — it now persists correctly.
