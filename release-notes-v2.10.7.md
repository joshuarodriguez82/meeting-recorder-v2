# v2.10.7 — Backend bootstrap survives Python 3.13 + tokenizers wheel gap; hf_hub pin relaxed

Root-cause fix for the v2.10.6 install failure that needed a marker-file
workaround in the field. Two changes; either one alone would have
prevented the field repro, both together close the door on it.

1. **Critical** — `huggingface_hub==0.23.0` relaxed to `>=0.23,<1.0`.
   The strict equality pin forced pip's resolver to backtrack into
   `transformers 4.41.x` → `tokenizers 0.19.1`, which has no Python 3.13
   wheel. Source builds of that tokenizers version fail because the
   bundled `pyo3 0.21.2` references `PyUnicode_FromKindAndData` /
   `PyUnicode_4BYTE_KIND`, both removed from the Python C API in 3.13.
   The whole bootstrap died at "no matching distribution found for
   tokenizers" on every new install. Loosening the pin lets pip pick a
   newer hf_hub that ships py313 wheels for the full downstream chain.
   The pyannote.audio constraint (`use_auth_token=` keyword, removed in
   hf_hub 1.0) is preserved by the upper bound.
2. **Defense-in-depth** — bootstrap pip-install pass is now best-effort
   on the upgrade path. If an existing venv from an earlier version
   still has the critical modules (`fastapi`, `sounddevice`,
   `faster_whisper`, `pyannote.audio`, `torch`, `huggingface_hub`) and
   only the pip-install reverify pass trips, the backend now logs the
   pip warning and starts anyway instead of refusing to launch. The
   marker file stays unwritten so the next launch retries. Fresh venvs
   still fail hard — there's nothing to fall back to.

## Install (macOS)

> v2.10.7 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.10.7_universal.zip`.
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
> unzip -o Meeting.Recorder_2.10.7_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.10.7_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's fixed

### 1. `huggingface_hub` pin loosened across all three requirements files

`backend/requirements.txt`, `backend/requirements-cpu.txt`, and
`backend/requirements-mac.txt` now carry:

```
huggingface_hub>=0.23,<1.0
```

instead of `==0.23.0`. The upper bound still satisfies pyannote.audio
3.3.2's `use_auth_token=` requirement (that keyword was removed in
hf_hub 1.0). The lower bound matches what was already tested. The pip
resolver is no longer cornered into an unbuildable transformers /
tokenizers combination on Python 3.13.

Why the strict pin existed in the first place: an earlier release wanted
to keep the test matrix narrow when pyannote's hf_hub compatibility
window was unclear. Now that we know the lower bound for
`use_auth_token=` and the upper bound for hf_hub 1.0 specifically, the
range pin is both more permissive AND tighter on the actual breakage.

### 2. Bootstrap doesn't fail closed when the existing venv is healthy

`src-tauri/src/lib.rs::bootstrap_app_venv` previously returned an error
the moment `pip install -r` exited non-zero. For a fresh venv that's
correct — there are no Python modules installed yet, so a pip failure
genuinely means the backend can't start. For an UPGRADE from a working
prior version, however, the existing venv often has every module the
backend needs at startup, and a pip reverify failure (because the new
requirements file ran into a resolver quirk on the current pip / Python
combination) shouldn't be fatal.

The new behavior:

- **Fresh venv + pip install fails** → fatal, as before.
- **Existing venv + pip install fails + critical modules import** →
  logged warning, backend starts using the existing wheel set. The
  marker file is NOT updated, so the install retries on the next
  launch.
- **Existing venv + pip install fails + critical modules missing** →
  fatal, as before. The user gets the same actionable error they'd have
  gotten in 2.10.6.

The probe runs `python -c "import fastapi, pydantic, sounddevice,
soundfile, faster_whisper, pyannote.audio, torch, huggingface_hub"` and
considers the venv runnable iff every import succeeds.

## Known not yet patched

- **Marker file is still required to invalidate a venv** — the
  bootstrap recognizes "requirements changed" by string equality
  between the bundled `requirements*.txt` and a copy stashed in the
  venv. A hash would compare smaller; a true lockfile (PEP 751,
  `uv.lock`, `pip-tools`) would let us drop the resolver entirely and
  install with `--no-deps`. Tracked for a later refactor, not blocking
  this fix.
- **Existing v2.10.6 venvs that landed mid-installation** — a venv
  that was created by v2.10.6's failed bootstrap can be missing torch /
  pyannote entirely (the install errored before those got installed).
  Upgrading to v2.10.7 will trigger the pip reverify with the loosened
  hf_hub pin; on a working pip + Python that completes cleanly, and the
  venv is repaired. If pip still fails on that machine for a different
  reason, the critical-modules probe will (correctly) report missing
  modules and the bootstrap will report the pip error as before — same
  failure mode 2.10.6 had, no regression.
