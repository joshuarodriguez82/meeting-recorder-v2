# v2.19.2 — the backend stops needing a reconnect, and summaries stop getting cut off

> **What this release fixes:**
>
> 1. **No more "reconnect to the backend before you can record."** The
>    watchdog that restarts the backend could permanently give up ~25
>    seconds after a crash — after which nothing would ever bring the
>    backend back, and the only cure was restarting the whole app. It
>    now retries forever, and it checks whether the backend can actually
>    *serve* rather than just whether the process exists.
> 2. **Summaries and extractions are no longer silently truncated.**
>    Summaries were capped at 1,024 output tokens (~750 words) and
>    nothing checked whether the model got cut off — so a half-written
>    summary was saved, exported, and emailed as if it were complete.
>    Budgets are now 8× larger and truncation is detected and clearly
>    marked.

## Install (macOS)

> v2.19.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.19.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.19.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.19.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The backend no longer needs a manual reconnect

Two defects in the watchdog combined to leave the app with a dead
backend and no way back except restarting it.

**A held port permanently killed recovery.** After a crash, the backend's
network port can stay bound for up to ~2 minutes (Windows `TIME_WAIT`,
or a lingering child process). The watchdog polled every 5 seconds and
counted *each tick spent waiting for that port* as a failed restart
attempt. After five of them — about 25 seconds — it hit its limit and
shut its own thread down permanently. From that point the backend was
never respawned again for the life of the app, which is exactly why
recording required a restart. The precise condition the port check
existed to handle was the condition that killed the watchdog.

Waiting for a port is now just waiting: it never consumes the restart
budget, and a genuinely failed restart backs off (5s → 60s) and keeps
trying **forever** instead of giving up.

**It only asked "is the process alive", never "can it serve".** A
backend can be running and still useless — its socket broken across a
sleep/resume cycle, its event loop wedged during a native model load.
The watchdog saw a live process and did nothing. It now probes the
backend's `/health` endpoint every 5 seconds and, after ~30 seconds of
sustained unreachability on a live process, replaces it. That threshold
is deliberately patient: a backend busy transcribing is busy, not dead,
and killing one would cost real work.

## Summaries stop getting cut off

Real summaries were ending mid-sentence — "…based on KB match quality,"
— and being saved and emailed as though finished.

**The budget was far too small.** The summary call asked for 1,024
output tokens, roughly 750 words. A real meeting summary routinely runs
longer, so the model was cut off mid-word. The same 1,024 cap applied to
action items, decisions, and prep briefs.

| Step | Before | After |
|---|---|---|
| Summary | 1,024 | **8,192** |
| Action items / Decisions | 1,024 | **8,192** |
| Requirements | 2,048 | **8,192** |
| Structured extraction | 2,048 | **4,096** |
| Prep briefs | 1,024 / 1,500 | **4,096** |
| Daily briefing parse | 2,048 | **4,096** |
| Speaker identification | 512 | **1,024** |

**Nothing detected the truncation.** The API reports when it cuts a
response off, but no code path ever looked — so there was no error, no
log line, and no marker. The only way to notice was to read to the end.
Both provider paths now detect it, log a warning naming the limit, and
append a visible **"⚠️ This output was cut off"** marker to the artifact.

Budgets are also provider-aware: OpenAI-compatible endpoints get 1.5×,
because "thinking" models (Gemini 2.5 and friends) spend hidden
reasoning tokens against the same ceiling and therefore return less
visible output for an identical budget — the same effect that made the
live co-pilot return nothing before v2.17.0.

> **After updating:** any session whose summary was already truncated
> stays truncated — the fix prevents new truncation, it doesn't
> retroactively repair old output. Open those sessions and re-run
> **Process** to regenerate them in full.

## Under the hood

- Watchdog policy extracted into a pure `next_watchdog_action()` and the
  health check into `probe_health(port)`, both unit-tested — including a
  regression test asserting a held port stays a *wait* even after
  thousands of consecutive ticks, so this can never silently become a
  give-up again.
- `cargo test` now runs in CI (Linux) alongside `cargo check`.
- New `_budget()` helper centralises output-token sizing per provider.
- 6 new backend tests cover the truncation marker and budget floors,
  including a guard that fails if the summary budget drifts back toward
  1,024. Full backend suite: 122 passing.
