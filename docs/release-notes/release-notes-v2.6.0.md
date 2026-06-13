# v2.6.0 — Auto-record/auto-stop fixes, screenshots, smarter calendar & speakers

A big reliability + capability release. Auto-record actually fires now,
auto-stop no longer trips when you mute yourself, recordings analyze
themselves automatically, you can screenshot into the summary, calendar
invites expand with attendees/agenda/one-click Join, and speakers get
named for you.

## Install (macOS)

> v2.6.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.6.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.6.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.6.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Bug fixes

- **Auto-record never started.** The calendar query dropped events that
  had already begun, so the "is a meeting happening now?" check could
  never be true. Auto-record now scans today's meetings (including
  in-progress ones) and fires correctly — even if you enable it after
  the meeting started or restart the app mid-meeting.
- **Auto-stop triggered when you muted yourself.** The dead-air timer
  only listened to your mic. Far-end (other participants') audio now
  also keeps the recording alive, so muting yourself no longer ends the
  recording.
- **In-call "This call" search returned nothing**, and **session audio
  playback was broken**, in the packaged app — both were pointed at a
  stale fixed port from before the port became dynamic. Fixed.
- **Cleanup ("failed to fetch") and automatic cleanup never ran.**
  Retention is implemented and no longer blocks the backend; it now
  cleans the main folder **and** every client Designated Folder,
  including orphaned recordings whose session was deleted.
- **External links did nothing** in the desktop app (Join, Settings,
  Usage Guide). Every `http(s)` link now opens in your real browser.
- **Calendar sometimes showed no meetings** on slow Exchange — longer
  fetch budget and the per-day scan is now cached so background polling
  can't starve the list.

## New

- **Auto-process after stop is ON by default.** Stop a recording and
  the full pipeline (transcribe → speakers → summary → action items →
  decisions → requirements → commitments) runs automatically. Toggle in
  Settings.
- **Screenshots.** A Screenshot button next to Stop captures your
  screen (multi-monitor picker), saved with the meeting, fed to Claude
  as visual context, and viewable on the session's new **Screenshots**
  tab.
- **Click a calendar meeting to expand it** — attendees, the invite
  agenda/body, and a one-click **Join meeting** button. The agenda also
  feeds the AI Brief so it's tailored to that specific meeting. Exchange
  attendees now resolve to real names/emails instead of directory codes.
- **Never auto-record a meeting.** A per-meeting "No auto" toggle
  permanently skips a meeting (and its recurring series).
- **Automatic known-speaker naming.** Speakers who introduce themselves
  or are called on by name are auto-named and their voiceprint saved,
  so they're recognized in future meetings with no manual labeling.
- **Follow-ups / Commitments / Decisions auto-refresh** when you open
  the tab or return to the app, so freshly-processed calls show up.

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
