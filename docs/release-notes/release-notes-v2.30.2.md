# v2.30.2 — your recordings were never short

## Install (macOS)

> v2.30.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.30.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.30.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.30.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**. If you already updated
for v2.30.1, there's nothing to do.

## "About 5 min appears to be missing" was wrong

Recordings were reported as truncated, with a CRITICAL warning claiming
a 14% audio deficit. **No audio was missing.** Two sessions from the
same morning:

| session | `finalize done in` | reported "missing" |
|---|---|---|
| A | 278.5s | 278.9s |
| B | 144.8s | 145.2s |

The shortfall equals the post-processing time, to within half a second.

`ended_at` was stamped *after* the finalize subprocess returned, so the
"recording window" that the audio got measured against silently
included however long finalize ran. Session A started at 08:31:04 and
capture stopped at 08:59:14 — 1690 seconds. The mic delivered 1690.2s
and the loopback 1690.3s, agreeing with each other to **0.2s**, with
**zero dropped frames** on either stream. The capture was flawless; the
ruler was wrong.

`ended_at` now marks when capture stopped. Finalize cost is recorded
separately.

**This had been happening since the beginning** — finalize was
previously 3–15 seconds, comfortably under the 2%-of-window alarm
threshold, so nobody saw it. Turning on echo cancellation took finalize
to 278 seconds and pushed a long-standing measurement bug past its
trigger.

**Your session durations were inflated too.** A recording listed as
"32m 49s" was 28m 10s of audio; the extra 4m 39s was post-processing
time counted as recording time. Durations now reflect what was actually
captured, so previously-listed lengths may shrink slightly. Nothing was
lost — the number is just honest now.

## You can finally see whether echo cancellation did anything

AEC is opt-in, and `settings.py` says it stays off by default "until
there's field evidence it helps more than it costs." That evidence was
being generated and thrown away.

Every AEC attempt logs its decision — accepted or rejected, why, and
the echo reduction achieved in dB. That line is written by the finalize
**subprocess**. The parent only mirrored the child's *stderr* into the
log, while the logger writes to *stdout* — so every log line the child
ever produced was discarded. A field log covering two confirmed
AEC-enabled finalizes contained not one AEC decision.

Both streams are now mirrored, so the whole finalize subprocess is
visible for the first time — not only AEC. The outcome is also stored
on the session: whether AEC was requested, whether it was accepted, the
reason, `erle_db`, and the residual delay.

Three states are kept distinct: **not requested**, **requested and
decided**, and **requested but no decision came back** (a crashed or
silent subprocess). That last case is recorded as itself and warned
about. An outcome nobody could read must never pass for a clean one —
this codebase has shipped that same mistake as document hits rendering
as "Untitled" meetings and as extension posts rendering as "never
posted."

## Deciding on AEC

With `erle_db` now recorded, the toggle becomes a measurable decision.
The cost is real — roughly 40x the finalize time, about 4.6 minutes of
processing on a 28-minute recording. If your `erle_db` comes back low
or the decision is rejected, turn it off; you're paying that for
nothing. It only earns its keep on the setup it was built for: an
external mic with **speakers** rather than a headset, where the far end
comes back out of the speakers and gets picked up a second time.

## Tests

704 backend tests, up from 688. The 16 new ones cover a slow finalize
not producing a false warning, a genuinely short WAV still warning, a
real mic gap still being flagged, and the AEC outcome persisting across
accepted, rejected, not-requested, no-decision, and subprocess-crash.
