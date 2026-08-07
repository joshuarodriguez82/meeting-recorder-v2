# v2.20.3 — fixes sessions disappearing after setting a Session Archive

> **If your library shrank after configuring a Session Archive, this is
> the fix. Nothing was deleted — your files were always on disk.**

## Install (macOS)

> v2.20.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.20.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.20.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.20.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## One bad folder took down the whole library

Setting a Session Archive to a Google Drive folder could make most of
your sessions vanish from the app — a 73-session library dropping to a
handful.

The sessions were never gone. They sat on local disk the entire time.

When the app looks for sessions it now scans several folders. That loop
had no error handling:

```python
for root in self._scan_roots():
    paths.extend(root.rglob("session_*.json"))
```

Cloud folders fail transiently — a sync client stalls, a stream
hiccups, a permission blips. Any of those raised an error that escaped
the whole function, so the request that lists your sessions failed
outright. Not "the cloud folder returned nothing" — **everything**,
including the sessions sitting on your own hard drive.

Folders are now isolated. One that fails contributes nothing and the
rest still return their sessions. Your local recordings folder is
always read first, so a network folder can never cost you your own
library no matter what it does.

## And it says so now

An unreachable folder is reported instead of silently shrinking your
list — named in the log with a pointer to check your sync client.

A short list that quietly meant "a folder errored" is what made this
so hard to pin down. It's the fourth time this week the app has treated
*couldn't read it* as *there's nothing there* — after the Today tab
briefing, the Clients list, and session discovery itself. Every one of
those now says what happened.

## Recommendation

If you turned on Session Archive and things got worse, this release
fixes the disappearing sessions. Roaming a library between two machines
is still rough around the edges, and leaving the archive field empty
costs you nothing but roaming.
