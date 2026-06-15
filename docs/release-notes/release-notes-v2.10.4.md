# Meeting Recorder v2.10.4

Closes the processing-integrity holes behind the wrong-summary incident, and adds read-only audio sync measurement.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.10.4_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
> 2. Drag into `/Applications`.
> 3. Double-click. Dismiss the "damaged" warning.
> 4. **System Settings → Privacy & Security**, scroll to bottom, click **Open Anyway**.
> 5. Double-click again, click **Open**.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_*_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> Windows users — just install the `.msi` or `.exe`.

---

## Processing integrity — the two remaining race fixes

v2.10.3's lock stopped *concurrent* jobs from crossing. v2.10.4 fixes the two underlying mechanisms that let a session get corrupted in the first place:

- **Re-transcribe now replaces, never appends.** `process_session` used to *append* segments, so re-processing a session concatenated the new transcript onto the old one — that's how two meetings merged into one (a single session ended up with both transcripts and a blended summary). It now clears segments + speakers first, so a re-process reflects exactly that audio.
- **Extraction no longer clobbers fields.** The post-transcribe step ran five extractions concurrently, each saving the whole session independently — the last writer won and silently nulled the others (a summary could come back blank even though it "succeeded"). It now computes all extractions, applies them to one session, and saves once.

Together with the v2.10.3 lock, the wrong-summary / merged-meeting class of bug is closed end to end.

## Audio sync — read-only measurement (no audio changed)

Dual-device capture (mic + system-audio loopback) runs on two hardware clocks that tick at slightly different rates, so long meetings can drift. v2.10.4 **measures** this without altering any audio:

- Per-stream sample + buffer-overflow counters during capture.
- At stop, each stream's delivered duration is compared to the wall-clock window, and the mic-vs-system-audio divergence is computed.
- Logged as `SYNC_INTEGRITY`, and surfaced as a blue info chip on the Sessions list when a stream falls behind, the tracks drift apart, or buffer overflows occurred.

This is deliberately measurement-only — it tells us whether (and how fast) drift actually happens on real hardware before any correction is built. A proper drift-correction pass and acoustic-echo handling are being evaluated against this data, not shipped blind.

---

## What didn't change
- Everything from v2.10.3 carries forward.
- Existing sessions / settings / config preserved.

## Upgrade notes
- **Windows** — `Meeting Recorder_2.10.4_x64-setup.exe`. Installs over your existing version.
- **macOS** — see the install block at the top.
- **No new dependencies**, no bootstrap changes.
- Recommended for anyone on v2.10.0–v2.10.3.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
