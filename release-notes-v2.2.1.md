# v2.2.1 — universal Mac build + dependency self-heal

A small patch release that fixes two install-path issues from v2.2.0
without touching any user-visible features.

> ## ⚠️ macOS install — READ THIS FIRST
>
> ### Step 1: download the .dmg
>
> v2.2.1 ships **a single universal `.dmg`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab:
>
> - `Meeting.Recorder_2.2.1_universal.dmg`
>
> (v2.2.0 shipped two architecture-specific files; v2.2.1 consolidates
> them into one.)
>
> ### Step 2: bypass Gatekeeper
>
> The build is **unsigned** (no Apple Developer cert yet), so macOS will
> say *"damaged and can't be opened"* when you double-click the DMG.
> It is **not** damaged — it's the quarantine attribute your browser
> added on download. Pick whichever path is easier; both work, both
> are one-time per install.
>
> **Path A — System Settings (no Terminal):**
>
> 1. Double-click the DMG, drag the app to **Applications**.
> 2. Double-click `Meeting Recorder` in Applications. macOS refuses
>    with the "damaged" warning. Click Done / Cancel.
> 3. Open **System Settings → Privacy & Security**. Scroll to the
>    Security section. Click **Open Anyway** next to the Meeting
>    Recorder blocked-app message.
> 4. Re-double-click the app. macOS asks once more — click Open. Done.
>
> **Path B — Terminal:**
>
> ```sh
> xattr -cr ~/Downloads/Meeting*.dmg
> open ~/Downloads/Meeting*.dmg
> # In Finder: drag the app icon to Applications.
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> macOS treats the app as trusted on every subsequent launch — you only
> do this once. **Right-click → Open does not work** on macOS Sequoia /
> Sonoma.
>
> **Windows users** — none of this Gatekeeper stuff applies. Download
> the `.msi` or `.exe` from the Releases page and double-click.

## What changed since v2.2.0

### Single universal2 macOS DMG (was: only Apple Silicon was actually built)

v2.2.0's release workflow declared a `macos-13` (Intel) matrix entry
alongside `macos-14` (Apple Silicon), but GitHub deprecated free-tier
`macos-13` runners; the Intel job either failed to allocate or errored
at queue time. With `fail-fast: false` set, the other matrix entries
published anyway, so Intel Mac users had nothing to download from the
v2.2.0 release.

v2.2.1 builds a universal2 (fat) binary on `macos-14` instead. Tauri's
`--target universal-apple-darwin` compiles both arm64 and x86_64,
lipos them into a single `.app`, and produces one `.dmg` that runs on
every Mac. Build is ~2 min slower; the artifact is ~30-50% larger
than a native single-arch build; no paid Intel runner needed.

### Backend self-heals when a venv package is missing

Some users hit a "sentence-transformers isn't installed" warning in
Settings → Semantic Index after upgrading from a pre-v2.1.0 venv —
the bootstrap detected the existing venv as populated and skipped
re-installing the new requirements that v2.1.0+ introduced. v2.2.1
adds a runtime self-heal: on backend boot, three feature-critical
packages (`sentence_transformers`, `speechbrain`, `anthropic`) get
import-tested. If any are missing, the backend re-runs
`pip install -r requirements-{cpu,mac}.txt` against the running
interpreter and continues. ~10ms in the common case where everything
is already installed; one-time ~30s pause when an actual repair is
needed.

Catches three failure modes the existing bootstrap fingerprint check
missed:

1. Initial pip install was interrupted partway (network blip during
   wheel downloads).
2. Upgrades where a new package was added to requirements after the
   user's venv was created — and the fingerprint check happened not
   to fire.
3. The user manually `pip uninstall`'d something.

### Install docs

README + MAC_SETUP.md + this release-notes file all use the same
two-path Gatekeeper workaround now (System Settings or `xattr -cr`),
since the pre-v2.1.0 right-click-Open trick stopped working on macOS
Sequoia / Sonoma.

## No feature changes

Everything in v2.2.0 — dual-stream live transcription, in-call search,
Groq + Gemini provider presets, the three crash fixes — is unchanged
in v2.2.1. If you're already happily running v2.2.0, the only reasons
to upgrade are:

- You're on an Intel Mac and couldn't install v2.2.0 at all
- You hit the "sentence-transformers not installed" warning and don't
  want to manually run pip in the venv

For everyone else this is a quiet patch release.
