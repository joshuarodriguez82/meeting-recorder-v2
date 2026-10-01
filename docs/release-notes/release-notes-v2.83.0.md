# v2.83.0 — Teams links, a Co-Pilot out of beta, and recordings that stay in sync

## Install (macOS)

> v2.83.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.83.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.83.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.83.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension too

**The extension goes to 1.25.0** — it's what brings Teams links back.
Download `chrome-extension.zip` from the release and reload it at
`chrome://extensions`.

## Teams meetings get their join link

Meetings imported from Outlook on the web showed a join link only when
it was a Zoom meeting; Teams meetings had none.

The cause is Microsoft Defender **Safe Links**: Microsoft 365
organizations rewrite every link in mail and meeting invites into a
`safelinks.protection.outlook.com` link with the real address hidden
inside. The app looked at the outer address, didn't recognise it as a
meeting, and moved on — so every Teams "Join the meeting now" link was
skipped. Zoom links survived because they're usually typed into
Location, which Safe Links leaves alone.

The app now looks inside Safe Links for the real meeting address, in
the extension and in the app itself, and opens the Teams link directly.
Only real Teams, Zoom, Webex and Google Meet join links are ever
accepted — unwrapping can't turn an ordinary link into a "join" button.
It also reads the Teams link from two more fields Outlook uses for it.

## Recordings stay in sync through quiet stretches

On Windows, when nothing was playing — a pause in the call, someone on
mute, a screen share with no sound — Windows sends the app no system
audio at all, and the app joined what it did get back to back. Every
quiet stretch was cut out of the other participants' track, so
everything they said afterwards sat earlier in the recording than it
was said. Your diagnostics showed the effect: 8 seconds out on a
25-minute call, 98 seconds on a 16-minute one, and almost two hours
on a call that ran 2 hours 22 minutes.

That shifted the transcript order and the speaker attribution, which
compares the two tracks moment by moment. The app now fills those
gaps with silence by the clock, so the other side stays where it was
said. Normal playback is never touched, and audio that arrived is never
trimmed. The same protection applies to the new native Mac system
audio.

## The Co-Pilot is out of beta

The Co-Pilot has been rebuilt around how it's actually used mid-call:

- **It keeps working whichever tab you're on.** It used to stop the
  moment you left the Record tab — and with it the notes your summary
  draws on.
- **One board, not a pile of updates.** Each suggestion appears once;
  when the Co-Pilot raises it again it's merged in ("raised 3×") rather
  than repeated. Newly raised items are marked **new**.
- **You decide what stays.** Mark an item **done** when you've asked or
  handled it, **save** it as a follow-up, decision or note, or
  **dismiss** it. The Co-Pilot is told, so it stops raising it, and what
  you dismissed is left out of the meeting summary. Handled items fold
  away under **Handled**, with Undo.
- **It knows the meeting.** It's now told the meeting's name, client,
  project, organiser and attendees from the invite, and who is speaking —
  "Jane Roe" or "Speaker 2", not just "them". Until now it was never
  even told the meeting's name.
- **Ask it.** Type a question about the call so far — "what did they
  say about the timeline?" — and it answers from the transcript, saying
  so when it hasn't come up.
- Pause, interval changes and persona/meeting-type changes take effect
  immediately.

## Smaller fixes

- **Backend log:** quiet polling requests no longer fill the log, and
  lines are no longer written twice. The log you send in a diagnostics
  bundle now covers weeks instead of hours.
- **Crash reports:** a crash is now reported once, on the next start,
  instead of on every start for weeks afterwards.
