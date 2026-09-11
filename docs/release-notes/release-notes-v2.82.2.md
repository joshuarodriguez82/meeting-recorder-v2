# v2.82.2 — The export sweep and the mic check had never run

## Install (macOS)

> v2.82.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## One typo, three dead features

A backend log from a normal working day contained this line **123
times in four hours**:

> Export sweep failed: 'bool' object is not callable

That is the export reconciliation sweep — the safety net that notices
when a meeting's files never reached the Designated Folder and copies
them. It runs every couple of minutes. It had a **100% failure rate**,
and had never once completed since the loop was written.

The cause is three characters. `is_recording` is a property, and three
places asked for it as `is_recording()`, which raises rather than
returning true or false. Each of those three is a whole feature:

- **The export sweep.** Dead. Every meeting relied entirely on the
  immediate export added in v2.82.0; anything that path missed — an
  offline folder, a copy that used up its retries — waited forever
  rather than a couple of minutes.
- **The microphone readiness check**, added in v2.81.0. It answered an
  error on **every single request**. The feature has never worked for
  anyone since it shipped.
- **The knowledge indexer's "is anything else running?" guard**, which
  is how indexing stays out of the way of a recording.

All three now work.

### Why no test caught it

Every stand-in for the recording service in the test suite declared
`is_recording` as a method. The stand-ins answered a call the real
object refuses, so the suite stayed green while three production paths
raised the moment they were reached.

The stand-ins have been corrected, and there is now a check that scans
the whole backend for any property being called as a method — because
fixing three call sites does nothing about the fourth. Nothing about
`x.is_recording()` looks wrong when you read it; you have to know how
the attribute is declared in another file, and Python only finds out
when the line actually runs.

Reintroducing the bug turns five tests red.

## Closing the app left no trace

A backend log showed **89 starts, 89 prior-crash markers, and zero
clean stops**. The app records a "stopped cleanly" marker so that a
start without one means something went wrong — and that marker had
never once been written, so every start looked unclean and the signal
said nothing.

The handler that writes it was correct and was never reached. Closing
the app kills the backend outright, on purpose (the window is gone;
there is nothing left to serve), and that path skipped the marker. It
now writes one first, and records which of the two exits it was.

This is the signal you would go looking for the next time a recording
goes missing.

## Whether a meeting had a join link is now countable

Every counter for the extension's join-link extraction reads zero,
while meetings themselves import fine — 44 kept out of 48, the four
drops all genuinely cancelled. There is a fallback that pulls the link
out of the invite body, so zero extraction does not necessarily mean a
missing **Join** button.

Nothing counted the outcome, only the attempts, which made "is the Join
button missing?" unanswerable from a diagnostics bundle. The import now
records how many meetings ended up with a join link. Counts only — the
link itself is meeting content and never leaves the machine.

