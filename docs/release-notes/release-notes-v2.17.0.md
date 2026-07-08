# v2.17.0 — recording recovery hardening + a live co-pilot that stops repeating itself

> **What this release adds:**
>
> 1. **A mid-recording crash can no longer silently truncate your
>    call.** If the app is killed while recording (installer launch,
>    crash, power loss), the capture WAV's header is left unfinalized —
>    it advertises a fraction of the audio that's physically on disk.
>    Recovery used to trust that header, merge the short length, and
>    then delete the temp that held the whole recording. Now it repairs
>    the header first and refuses to discard the source if the merge
>    comes out short.
> 2. **The live co-pilot stops repeating itself and stops padding.**
>    "Vendor lock-in" and "request an update" no longer resurface every
>    tick; generic filler is dropped, near-duplicates across the whole
>    meeting are de-duplicated, and the prompt now biases hard toward
>    silence + specificity (name the system/vendor/number, or say
>    nothing).
> 3. **Gemini live co-pilot returns output again.** Gemini 2.5's hidden
>    reasoning was exhausting the token budget and returning empty
>    content (the "connection test works but the panel stays blank"
>    bug). Non-Anthropic providers now get a real token budget, coach
>    ticks request a JSON object explicitly, and an empty/unparseable
>    response surfaces a reason instead of going silently blank.

## Install (macOS)

> v2.17.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.17.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.17.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.17.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Recording recovery hardening

When the backend is killed mid-recording, the streaming capture WAV
keeps every audio frame on disk but its RIFF/`data` length fields are
only finalized on a clean stop. A hard kill leaves them stale, so every
reader — `sf.info`, the merge, media players — sees only the handful of
frames the header advertises even though the full recording is present.
Field repro: session 191D826D (2026-06-30) captured 20+ minutes; the
header said ~1 minute; recovery merged 1 minute and then deleted the
temp that held everything.

Two fixes:

- **Repair the header before trusting it.** Recovery now rebuilds the
  RIFF + `data` chunk sizes from the file's real byte count when they're
  short of what the bytes imply, so the full capture is merged. No-op on
  cleanly-closed files.
- **Never discard a full source for a short merge.** If the merged
  output is still far shorter than the mic temp's bytes imply, the short
  merge is dropped and the temp files are **kept** for manual recovery,
  rather than replacing the only full copy with a fragment.

This can't recover audio already lost before this build, but it stops
the next mid-recording kill from repeating it.

## Live co-pilot

Real field session feedback (a discovery call on a local Ollama model,
plus a Gemini test that produced nothing) drove three changes:

- **No more broken record.** Every suggestion is de-duplicated against
  every prior tick in the same meeting (normalized near-duplicate match,
  including "slightly-expanded reword" containment), so "vendor lock-in"
  and "linked-account status" can't reappear 30 times.
- **No more filler.** Generic process-chatter ("request an update",
  "schedule a follow-up", "request documentation") is dropped unless
  it's anchored to a specific named artifact/person. A hard cap of 3
  items per tick backstops a misbehaving model.
- **Sharper prompt.** The output contract now biases hard toward
  silence (0–1 items is the common, correct output), bans bare
  "vendor lock-in", and requires each item to name the specific system,
  vendor, number, or decision from the transcript.
- **Gemini returns output.** Non-Anthropic providers get a much larger
  coach token budget (1500 wide / 640 hot) so a thinking model's hidden
  reasoning doesn't consume the whole allowance; coach ticks send
  `response_format=json_object` (best-effort, with a plain-call
  fallback); and an empty/unparseable response now returns a visible
  reason instead of a silently blank panel.

> **Tip:** for real-time relevance the model matters more than the
> prompt. Pointing the live co-pilot at **Anthropic Haiku** (or Gemini,
> now that it works) instead of a small local model is the single
> biggest quality lever — it's pennies per meeting at a 45s cadence.

## Under the hood

- The FastAPI app can now be imported headlessly (dependency self-repair
  is skippable via a flag), and a new **route-parity test** snapshots
  the full route table so future refactors can't silently drop an
  endpoint. Prep for an in-progress server.py modularization (not user-
  visible).

## Known / recommended

- **Move your recordings folder off Google Drive Stream (`G:\`).**
  Writing recordings directly into a cloud-stream folder is behind the
  finalize stalls, evicted audio, ghost sessions, and disappearing
  Sessions list. Use a **local** folder and let Drive's *Mirror files*
  mode sync it — that's "write local, sync to cloud, other devices
  read" with none of the contention.
- **Editable AI prompts (all features) and a tabbed Settings layout**
  are planned follow-ups.
