# v2.23.0 — meetings from Outlook Web reach the Record tab, and crashes finally leave a trace

## Install (macOS)

> v2.23.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.23.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.23.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.23.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Meetings the browser extension finds now show up where you record

The Chrome extension reads your Outlook Web and Teams calendar, but
until now everything it found went only into your **Today** briefing.
The **Upcoming Meetings** list on the Record tab read your local Outlook
and nothing else.

So a meeting the extension could see but local Outlook couldn't — one
that hadn't synced, or lives only in the web calendar — showed up in
your morning briefing and was impossible to start a recording from.

Both sources now feed that list. Meetings found in both are shown once,
matched on title and start time even when one side says "Updated! AWS
Town Hall" and the other says "AWS Town Hall". Your local calendar wins
when they overlap, because it carries attendees and the invite body.

Meetings that came only from the extension are marked **From Outlook
Web**.

> **Auto-record stays off for those.** Their times are read out of
> scraped web text rather than a real calendar entry, and starting a
> recording at a misread time is worse than not starting one. Those
> rows say **Manual only** — press **Use** and start it yourself.

Also: if Outlook is slow to respond, the list no longer comes back
empty. You still get whatever the extension found.

## Crashes finally leave a trace

Some of you have hit the backend restarting on its own — the
"Reconnecting to backend" message, sometimes losing the processing of a
meeting you just finished.

This has been chased since **v2.0.18** across five separate releases and
never solved, for one reason: the failure kills the app's engine so
abruptly that it never gets to write down what it was doing. The log
simply stops mid-line. Every fix so far has been an educated guess about
what was running at the time.

This release installs a crash handler that records what was happening at
the instant of the failure — which file, which line, which library —
into its own file that survives the crash. The app also now writes a
clear marker into its log at the moment it notices, so the last moments
before a crash are easy to find.

**Settings → Data & Diagnostics** shows the crash report with a copy
button.

This does not fix the crash. It's the instrument that will let it be
fixed. **If you hit it, send that crash report** — it's the piece that
has been missing for five attempts.

## Nothing else changed

No changes to recording, transcription, summaries, or your data.
