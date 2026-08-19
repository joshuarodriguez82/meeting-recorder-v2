# v2.39.0 — "never contacts Outlook" now includes email

## Install (macOS)

> v2.39.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.39.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.39.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.39.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## The setting said one thing and the app did another

**Calendar source → extension** describes itself as *"Never contacts
Outlook."* People choose it precisely because a desktop Outlook isn't
available to them.

Then **Draft follow-up emails** launched desktop Outlook anyway and
filed drafts into whichever profile it found. In the field that produced
ten drafts a user could never see, in a client they don't use.

On macOS the same thing happened through AppleScript — probing Mail.app,
then driving Microsoft Outlook, then writing files to Downloads.

With **extension** selected, no mail client is contacted on either
platform now.

## What you get instead

Follow-ups are prepared exactly as before — same drafting, same owner
grouping, same recipient resolution — and delivered as **Outlook Web
compose links**, one per person. Each opens a prefilled compose window
in the browser session you're already signed into. Right mailbox, no
sign-in prompt, no desktop client.

They open **one at a time**, from a list in the session. Ten compose
windows firing at once would be worse than the problem being fixed.

The message says what actually happened:

> 2 Outlook Web compose links ready — **nothing has been saved to any
> mailbox and no drafts exist yet.** Open them one at a time to review
> in your browser; each becomes a draft only once you save it in Outlook
> Web.

A compose link is not a saved draft, and the app no longer blurs the
two. That confusion is what sent someone hunting through a Drafts folder
for messages that were never written.

## Long messages don't get silently cut

A compose link is a URL, and URLs have limits — enforced not by modern
browsers so much as by the corporate proxies and sign-on gateways this
mode exists to work around.

The recipient and subject are never shortened. If a message is too long
for the link, the body is trimmed **at a word boundary**, the link says
so where you'll see it, and the full text stays in the app with a button
to copy it. Truncation is a property of the link, never of your message.

Length is measured after encoding, since a newline or an accented
character costs several characters in a URL — measuring the raw text
would blow the budget on exactly the messages closest to it.

## Nothing about calendars changed

Calendar display and auto-record were only just fixed, so this
deliberately touches none of it. The setting is **read** and nothing
else. No calendar file was modified, and a regression test pins the
calendar feed's behaviour across every value of the setting — including
which sources are consulted and which wins when both return the same
meeting — so any future change there fails loudly instead of quietly.

A second test parses the new modules and asserts neither imports any
calendar code at all.

## Unchanged for everyone else

On **local**, **outlook** or **auto**, drafting behaves exactly as it
did — real drafts saved into Outlook, with the folder and account
read-back added in v2.38.0. Those files were not modified.

## Tests

1124 backend tests, up from 1086. The 38 new ones cover: extension mode
never reaching Outlook or AppleScript on either platform; the other
modes behaving identically; URL encoding of subjects and bodies
containing ampersands, hashes, plus signs, newlines and non-ASCII;
over-budget bodies trimming in the link while the full text stays
intact; and the message never describing a compose link as a saved
draft.
