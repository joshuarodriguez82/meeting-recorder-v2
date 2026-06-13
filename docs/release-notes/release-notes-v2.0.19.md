# v2.0.19

## Fix: phantom "pythonw.exe" console window

Long-standing laptop bug, present since day 1. Actual cause (confirmed via Joshua describing the popup as "a black prompt window with pythonw.exe in the tab, close the X, it comes right back"):

Intel MKL / OpenMP / CUDA runtime libraries in some configurations call `AllocConsole()` during their first real use to install a console control handler. On `pythonw.exe` (GUI subsystem, no console by default) `AllocConsole` creates a **visible conhost window titled with the parent EXE name** — hence "pythonw.exe" in the tab. Closing the window kills conhost, but any next call to `AllocConsole` creates a new one, so it looks like the window keeps coming back.

`CREATE_NO_WINDOW` on the Rust spawn side only prevents a console at spawn time — it doesn't block a DLL from allocating one later.

Fix: `server.py` now calls `FreeConsole()` at startup and runs a 2-second background watchdog that polls `GetConsoleWindow()` and calls `FreeConsole()` on any console that appears. Any phantom console gets detached before it's visible for more than a blink. `pythonw` is the only client, so conhost exits and the window closes.

Usage Guide Troubleshooting has a new entry specifically for this; the older WER-dialog entry (v2.0.18) is kept since those are two different failure modes that look similar but need different fixes.

No change to any user-visible functionality — the app just stops showing the console.
