# v2.32.0 — the app already knew which voice was yours

## Install (macOS)

> v2.32.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.32.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.32.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.32.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## Your transcript stops crediting you with the other side's words

The long-standing complaint — your speaker turns containing sentences
the far end said — was blamed on acoustic echo for months. It wasn't
echo. A real session finally measured it:

```
Echo cancellation: not applied (erle_non_positive)
```

Echo reduction at or below zero means there was no echo path to cancel.
On a headset your callers' audio never reaches your microphone. The
echo canceller spent **9.8 minutes** on a one-hour recording, found
nothing to remove, and correctly discarded its own output.

The real cause was upstream. The app captures two physically separate
streams — your **microphone**, and the system **loopback** carrying
everything your computer played, which is by definition the far end.
Finalize merged both into a single mono file, and speaker diarization
was then asked to work out who spoke from voice characteristics alone.

Perfect information, discarded at the merge, then guessed at.

Which stream captured a sound is not an inference. It's ground truth,
and it is now used.

## How it works

During finalize, while both raw streams still exist, the two channels
are compared frame by frame — after applying the alignment offset the
app already computes, since comparing unaligned audio would go wrong at
exactly the moments that matter.

Each 32 ms frame is classified as **you**, **far end**, **overlap**, or
**silence**, using each channel's own noise floor rather than an
absolute threshold — mic gain varies by 30 dB between a headset boom
and a laptop array, and one rule has to work across both. A 6 dB margin
decides dominance, and short runs are smoothed away so labels can't
flap mid-syllable.

That timeline is stored beside the session. At processing time, spans
confidently identified as you override the voice-based guess. Turns are
**cut** at those boundaries rather than voted on as a whole, because a
single turn routinely straddles a handover — voting would either hand
the far end's tail to you or throw away your opening words.

Far-end spans deliberately do **not** force a label. Separating your
callers from each other is what voice clustering is good at; all that
was needed was to stop it lending them your identity.

## When it stands down

Channel evidence isn't always trustworthy, and a feature like this is
only safe if it knows when to stay out of the way. Attribution reverts
to today's behaviour, unchanged and without error, when:

- **There's no loopback** — a mic-only recording has no second channel.
- **Conference room mode is on** — the mic is capturing the whole room
  by design, so channel dominance means nothing.
- **The recording was made on speakers.** Far-end audio bleeds into the
  mic and the signal degrades. Two independent checks: how much speech
  lands in overlap, and whether the two channels' loudness envelopes
  actually correlate — near zero on a headset, near one on speakers,
  and immune to gain differences. Both must fire, because either alone
  has an innocent explanation.
- **Anything looks wrong** — too little speech, low confidence, a
  malformed or unrecognised sidecar, or a recording recovered by the
  crash path where the two streams were aligned by a fallback heuristic
  rather than a real measurement.

Sessions recorded before this release have no timeline and behave
exactly as they do today.

A stand-down is **recorded**, not silent. "No file" and "conference
room mode" are different facts, and this codebase has repeatedly
shipped bugs by letting an unreadable result pass for a clean one.

There's a settings kill switch, on by default.

## What this does not do

It separates **you** from **them**. Telling three people on the far end
apart from each other is still voice clustering, unchanged.

The mixed audio file is untouched — verified byte-for-byte identical to
what the previous version produced. This is analysis alongside the
merge, not a change to your recording.

## On echo cancellation

If it's still on, turn it off. It measured zero benefit on a headset
and costs roughly a sixth of your meeting length in processing every
time. Keep it in mind only for a genuine speakerphone or conference
room, where an echo path actually exists — and where, not
coincidentally, this new attribution stands down.

## Tests

779 backend tests, up from 741. The 38 new ones cover: speech only in
the mic attributed to you; speech only in loopback never attributed to
you; identical labelling with the mic scaled from 0.05× to 8×; every
stand-down path; a missing or malformed timeline falling back cleanly;
and a bleed sweep from clean headset to heavy speaker leakage
confirming the guards fire in the damaging range and stay out of the
way below it.

## Honest limitations

**Thresholds are calibrated on synthetic audio, not real speech.** The
margins are physically reasoned and behave correctly across the test
sweep, but where a real headset session actually sits is unmeasured.
Every threshold is a named constant and every session records its own
measured values, so retuning against real recordings is a one-line
change.

**The end-to-end win is unproven.** The mapping is tested against
constructed turns; it has not been run against a real diarization of a
real multi-speaker meeting. Your next recorded call is the first
genuine test.

**Simultaneous speech resolves to whoever is louder** in each frame,
rather than being marked as overlap. Only evenly-matched double-talk
lands in the overlap bucket. That's a deliberate judgment and worth
revisiting against real audio.
