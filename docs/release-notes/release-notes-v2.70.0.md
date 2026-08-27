# v2.70.0 — fresh installs work again

## Install (macOS)

> v2.70.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.70.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.70.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.70.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## If you are installing on a NEW machine, use this build

This release fixes a first-launch failure that has been present since
v2.8.0 and that **only affects machines installing for the first
time.** If the app already runs on your computer, you were never
exposed to it — and updating is still worth doing, because the next
machine you set up would have hit it.

On first launch the app builds its Python environment by downloading
packages. That step forbids source builds, for good reasons: source
builds can take half an hour and can pop up console windows the app
can't suppress. Two packages in the speaker-diarization stack have
never published a pre-built version, so the rule rejected them and the
whole setup failed:

- `antlr4-python3-runtime`, reached through `pyannote.audio`
- `docopt`, reached through `pyannote.metrics`

Underneath that sat a genuine version conflict: newer
`pyannote.metrics` releases require a NumPy that the pinned PyTorch
stack cannot use, and the last compatible release is the one needing
`docopt`. Unsatisfiable in both directions — the installer would spend
several minutes exploring combinations before giving up.

Both are fixed: the two packages are now exempted from the
source-build ban (both are plain Python — they take seconds and need
no compiler), and `pyannote.metrics` is pinned to the version that
matches the rest of the stack. Verified with the installer's exact
settings: setup now completes in about 90 seconds with every
audio-processing component at the version it was tested with.

**Why nobody noticed:** upgrade installs skip packages that are
already present, so every existing machine carried working versions
forward from before the rule was introduced. Only a genuinely fresh
environment ever ran the failing step.

## Dependencies are now locked to a tested set

Until now, roughly twenty of the app's packages had no version
recorded — each install resolved them fresh against whatever had been
published that day. That is how the fresh-install failure above went
unnoticed for so long, and it is also why two machines installing a
week apart could end up running different code.

Every package, including everything they depend on in turn, is now
pinned to an exact version: **158 for Windows, 151 for macOS.** Those
lists are produced by a CI job that installs on real Windows and macOS
runners using the same settings your machine uses, so what you install
is a combination that was actually built and verified rather than one
assembled on the fly.

Practically: installs are more predictable, and a bad upstream release
can no longer reach your machine without someone reviewing the change
first.

## Also in this release

Everything from v2.69.0 — the six delivery-phase summary templates,
the auth token no longer appearing in `backend.log`, and the
still-recording session dialog fix — carries forward.

The "show in folder" button now only opens folders belonging to the
app (your recordings folder, the archive, and configured client export
folders) and no longer creates a folder as a side effect of opening
one. No visible change in normal use.
