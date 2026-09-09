# v2.80.3 — recording starts on headsets that refused to open

## Install (macOS)

> v2.80.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.80.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.80.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.80.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## "All mic configurations failed"

On some Windows machines Start Recording failed outright with:

> Failed to start audio capture: All mic configurations failed. Last
> error: Error opening InputStream: Invalid number of channels

Two things went wrong, and the second one is ours.

**First**, the selected headset refused to open — a driver-level
refusal that no application can talk it out of. That is exactly what
the fallback is for: the same physical microphone is visible to Windows
several times over, through different audio subsystems, and if one
refuses, another usually works.

**Second, the fallback could not work.** It asked every alternative for
the same number of audio channels the *first* device had reported.
Windows does not describe one microphone identically across those
subsystems — one may call a headset stereo and another call it mono —
so every fallback was asking for a channel count that entry did not
have, and was refused for that reason alone. Fifteen attempts, all
failing on a setting the app never varied.

Now each entry is asked what it supports, and a device that claims two
channels is also tried at one — a headset that advertises stereo and
accepts only mono is the same failure in different clothing. The two
attempts most likely to succeed now come first, rather than after six
that cannot.

If it still fails, the message names what **your selected device** said,
rather than whatever the last fallback complained about — which was
usually a different device failing for an unrelated reason and sent you
looking in the wrong place.

## If you hit this

Install this build and try again. If it still will not start, the error
now carries the real reason; send it along with **Settings → Export
diagnostics** and it will point at the actual driver problem.
