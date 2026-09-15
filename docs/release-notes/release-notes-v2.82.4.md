# v2.82.4 — System audio is no longer given up on after four identical tries

## Install (macOS)

> v2.82.4 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.4_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.4_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.4_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## A meeting recorded with nobody else's voice in it

On Windows, a whole meeting was captured with **only the microphone**.
The far end — everyone else on the call — was missing. The log said so
once and the recording continued:

```
[FAIL] Loopback buffer=0    failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
[FAIL] Loopback buffer=1024 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
[FAIL] Loopback buffer=4096 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
[FAIL] Loopback buffer=2048 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
System audio capture unavailable. Mic only.
```

Four attempts, one error, four times. That error is Windows saying
*"this combination of sample rate and channel count is not what this
speaker is using."* The app's response was to try the same rate and the
same channel count again with a different buffer size — which cannot
answer that question. Two of the four attempts were not even different
from each other.

System audio now steps through **rates and channel counts**, not just
buffer sizes: the speaker's own rate and channels first (unchanged, so
a machine that works today takes exactly the same path), then stereo
and mono, then the other standard rate. A device the app cannot read at
all still gets a full ladder instead of one guess.

When the stream opens at a rate other than the one first requested, the
recording is now written at **that** rate. Previously the file would
have carried the wrong sample rate in its header, which plays back at
the wrong speed and drifts out of sync with the microphone track.

The log line names the format it tried instead of only the buffer size,
so four failures now tell you what was actually refused.

### The same mistake, one layer over

v2.80.3 fixed exactly this for the **microphone**: a ladder that varied
sample rate, block size and latency, and never varied the one parameter
that was wrong. System audio never got that treatment. It has it now,
and the decision lives in its own module with 18 tests — five of which
fail against the previous behaviour.

## Still being looked at

Two other problems turned up in the same report and are **not** fixed
here:

- A recording whose **stop** failed with a Python type error, losing
  that session's audio. The cause is understood well enough to know the
  fix is not a one-liner, and rushing it risks the save path.
- **Voice fingerprinting silently unavailable** on one install because
  the installed speech library no longer matches what the app expects.
  That is an environment mismatch rather than a code defect, and it
  needs a proper startup check rather than a log line nobody reads.
