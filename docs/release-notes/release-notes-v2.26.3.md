# v2.26.3 — Settings bars actually fixed this time (measured, not guessed)

## Install (macOS)

> v2.26.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.26.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.26.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.26.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The Settings bars

v2.26.1 tried to fix the tab bar and Save bar overlaying page content.
It made the gap larger instead.

Both attempts assumed a sticky element pinned with `top: 0` sticks to the
top of the visible scroll area. It doesn't — it sticks to the top of the
scrolling container's **content box**, which sits below that container's
padding. So the bars parked inside the padding band and content kept
scrolling in full view above and below them, and adding compensating
margins didn't move where they pinned.

This time the layout was measured in a real browser rather than reasoned
about:

| | Before | Now | Scroll viewport |
|---|---|---|---|
| Tab bar top | 88px | **64px** | 64px |
| Save bar bottom | 816px | **880px** | 880px |

Both bars now pin flush to the edges of the visible area, with their
backgrounds extended across the padding band so nothing can appear above
the tabs or below the Save button. Verified by screenshot, not by
inference.

The fix is two negative sticky offsets that cancel the container's
padding. They're commented on the spot with the measured numbers and the
reason, because they look wrong out of context and would otherwise be
"cleaned up" straight back into this bug.

## Note

Being able to render and measure the interface headlessly is new. Two
consecutive layout fixes were shipped blind and both were wrong; that
shouldn't have taken two attempts to notice. Layout changes can now be
checked before release rather than after.
