# v2.26.2 — fixes recording being impossible on v2.26.0 / v2.26.1

**If you are on v2.26.0 or v2.26.1, update immediately.** Those builds
could not record.

## Install (macOS)

> v2.26.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.26.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.26.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.26.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What broke

v2.26.0 introduced the SQLite session index. Wiring it up added one
argument near the top of the backend's start-up routine:

```python
index_db_path=str(USER_DATA_DIR / "session_index.db"),
```

`USER_DATA_DIR` is imported at the top of that file. But the same
start-up routine *also* contained a redundant local re-import of it,
about twenty lines further down. In Python that makes the name **local
to the entire function**, so every reference above the import — including
the new one — raises `UnboundLocalError` before the import line is ever
reached.

The failure mode was nastier than a plain crash:

- The settings object is assigned on the routine's **first** line, so it
  survived the error.
- Every service constructed **after** the failure point was never built
  at all.
- Start-up was guarded by "have we already loaded settings?" — which was
  now true — so it **never retried**.

The result was a backend that looked alive and answered some requests
normally, while others failed with `'NoneType' object has no attribute
'get_all'`, the device lists came back empty, and the Record tab showed
**"Not ready to record"** with no microphone or system-audio device
selectable. Restarting didn't help, because the failure was
deterministic.

## The fix

The redundant local import is gone, with a comment on the spot explaining
why re-adding it would break start-up again.

More importantly, the underlying fragility is fixed. Start-up no longer
treats "settings were loaded" as "start-up finished." It now tracks
completion explicitly, set as the very last step once every service
exists. A failure part-way through is retried on the next request instead
of leaving a permanently half-built backend with no way to recover short
of a downgrade.

Four regression tests lock this in, including one that scans the start-up
routine for *any* local re-import of a name already imported at module
level — not just this one. The next occurrence of this mistake fails in
CI rather than in a meeting.

## What was unaffected

Recording, audio, and your data were never at risk — this was a start-up
wiring error, not a data-path bug. No recordings or sessions were harmed
by it. Everything in v2.26.0 and v2.26.1 (screenshots, echo cancellation,
the session index, the spinner and Settings layout fixes) is intact.
