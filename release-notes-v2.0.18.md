# v2.0.18

## No more "pythonw.exe stopped working" dialog

Root cause: the backend occasionally crashes during first-launch model load (torch / pyannote DLLs get scan-interrupted by corporate EDR, exit code `0xC0000005` access violation). The watchdog respawns, the second attempt succeeds — but Windows Error Reporting would pop up a "program stopped working" dialog attached to the crashed pythonw.exe. Closing it → next crash → dialog again.

Fix: `server.py` now calls `SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)` at the very top of the file, before torch/pyannote imports. WER dialogs are suppressed; the watchdog still sees the exit code and respawns. User sees nothing.

The underlying crash isn't fixed (that would require tracking down exactly which DLL trips corporate AV during load), but it's a transient first-launch-only event and the retry succeeds every time in the logs we have.

Usage Guide Troubleshooting section has a new entry documenting the fix + how to diagnose if it ever resurfaces.
