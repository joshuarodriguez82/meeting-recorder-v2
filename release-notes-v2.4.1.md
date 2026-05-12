# v2.4.1 — In-app update notifications

Adds a lightweight "you're out of date, click to grab the new one"
nudge on app launch so you don't have to remember to check the
releases page manually. Deliberately not a full in-place installer
— the keypair management and CI signing that an in-place updater
requires aren't worth it for a small-team tool. See the architecture
note below.

## What it does

- **On launch**: silently queries
  `api.github.com/repos/joshuarodriguez82/meeting-recorder-v2/releases/latest`
  and compares against the running app version. If newer, surfaces a
  non-blocking toast: `Update available: v2.4.2 — [Download]`. Click
  the Download action and the GitHub release page opens in your
  default browser.
- **In Settings → App Updates** (new card): shows the current version,
  has a **Check Now** button, and an **Open Download Page** button
  when an update exists. Release notes from the next version preview
  inline with an expandable details disclosure.
- **Failures collapse silently.** No network, GitHub rate-limit, repo
  temporarily unreachable — the toast just doesn't appear. No error
  popups at startup.

## What it does NOT do

It does not download and install the new version for you. You still
download the installer from the GitHub release page (`.exe` / `.msi`
/ `.zip` for Mac) and run it manually, same as today. The nudge is
the value-add — you no longer have to remember to check.

## Architecture note: why no in-place install

A signed in-place updater (the Tauri updater plugin) would let users
click "Install & Restart" and skip the manual download. But it requires:

- Generating a minisign keypair and never losing the private key
  (lose it → no v2.4.1+ user can ever auto-update again, they all
  have to reinstall from a fresh build with a new pubkey)
- Setting two GitHub Actions secrets that sign every release artifact
- Per-platform `.sig` files attached to each release
- A `latest.json` manifest published with each release

For a personal / small-team tool where the threat model is thin
(attacker has to MitM your team's GitHub traffic AND not be detected),
the maintenance cost outweighs the convenience win. The notification
model is one-time write, zero ongoing key management.

If the user base grows or the threat surface changes — e.g., enterprise
deployment where corporate proxies might inject — revisit and swap in
the signed updater. The frontend abstraction (`src/lib/updater.ts`)
isolates the check logic so swapping it later is local.

## Install (macOS)

> v2.4.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.4.1_universal.zip`.
>
> Still unsigned for Gatekeeper purposes. First launch needs the
> Gatekeeper bypass.
>
> **Path A — Finder:** double-click the `.zip`, drag the `.app` to
> `/Applications`, double-click, dismiss the "damaged" warning, then
> **System Settings → Privacy & Security → Open Anyway**.
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
> **Windows users**: download `Meeting.Recorder_2.4.1_x64-setup.exe`
> or `.msi` and double-click.

## After install

Open Settings → App Updates. You should see your current version and
"You're on the latest release." From now on, when v2.4.2 (or later)
ships, you'll see a toast on launch nudging you to download it.

## Repo maintainer note

This release intentionally requires **zero secret/key management on
the repo**. No new GitHub Actions secrets, no keypair to generate,
no manifest files to attach to releases. Just tag and ship as
normal.
