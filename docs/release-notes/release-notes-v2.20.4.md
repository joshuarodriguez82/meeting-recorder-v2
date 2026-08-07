# v2.20.4 — your client folders stop following you to the wrong machine

> Pairs with v2.20.3. Together these make sharing a library between a
> Mac and a PC actually work.

## Install (macOS)

> v2.20.4 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.20.4_universal.zip`.
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
> unzip -o Meeting.Recorder_2.20.4_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.20.4_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Windows paths were landing on the Mac

v2.20.2 made your client list roam between machines. It roamed too
much. A client carries its **Designated Folder** and **Knowledge
Folder** — real paths on the machine that set them — and those were
copied across verbatim.

The result, straight from a real log on a Mac:

```
Reconcile 'Aon':   queued 1 session(s) for export to G:\My Drive\Aon
Reconcile 'Ricoh': queued 1 session(s) for export to G:\My Drive\Ricoh
```

`G:\` is a Windows drive letter. That Mac was queuing work against
folders that cannot exist on it, over and over.

The rule is now what it should always have been:

**Your client list travels. Your folder paths stay put.**

Add a client on the PC and it appears on the Mac with its name intact
and its folders blank, waiting for you to point them at somewhere that
exists on *that* machine. Each machine keeps its own Designated and
Knowledge folders permanently — one can never overwrite the other's.

A machine that hasn't synced yet also can't delete clients it hasn't
heard about.

## It cleans up the mess it made

If foreign paths already landed on your machine, this release clears
them on the next sync and tells you: *"Cleared 2 folder paths that
belong to another machine."*

Conservative on purpose — only paths that are impossible here are
cleared. A folder on an unplugged external drive is left exactly as you
set it.

## Setting it up

1. Both machines on v2.20.4
2. **Settings → Session Archive** → the same synced folder on each

   Use a folder of its own — not your Cloud Mirror folder. Sharing one
   folder between two features makes every problem harder to see.
3. **Sync now** on the machine holding your meetings

Sessions and client names appear on the other machine. Set that
machine's own folders where you want them.

Audio still never leaves the machine that recorded it.
