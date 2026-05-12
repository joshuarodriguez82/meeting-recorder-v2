# v2.4.1 — In-app auto-updater

Adds a self-update flow so future releases install themselves with one
click instead of you re-downloading installers from the GitHub releases
page. From v2.4.1 onwards, the app checks GitHub for newer releases at
launch and surfaces a small toast if one is available; you confirm in
Settings → App Updates and the new version downloads, signature-
verifies, replaces the install, and relaunches.

> **The first install of v2.4.1 still has to be manual.** The updater
> only works once the user is on a build that contains it. After this,
> v2.4.2 / v2.5.0 / etc. flow through the updater automatically.

## What it does

- **On launch**: silently asks GitHub if there's a newer release. If
  yes, shows a non-blocking toast that points at Settings → App
  Updates. If no (or no network), shows nothing. Never blocks startup.
- **In Settings → App Updates** (new card): shows the current version,
  has a **Check Now** button, and an **Install & Restart** button when
  an update is available. Release notes from the next version preview
  inline with an expandable details disclosure.
- **Install flow**: downloads the platform-appropriate installer,
  verifies its embedded minisign signature against the public key
  baked into the running binary, replaces the install on disk, and
  relaunches the app.

## Trust model

The updater downloads from
`github.com/joshuarodriguez82/meeting-recorder-v2/releases/latest/download/latest.json`.
That URL always redirects to the manifest of the latest release. Each
manifest entry includes a base64-encoded minisign signature over the
installer file. The signature is verified locally against a public key
that's compiled into the running app — there's no trust placed in
GitHub serving the right file, just in the keypair we control.

A MitM attacker who substitutes a malicious installer can't sign it
without our private key, and the verification step rejects it.

## Repo setup required before v2.4.1 actually self-updates

The CI workflow has the signing + manifest plumbing wired up, but the
keypair has to be generated once and the private key stored as a
GitHub Actions secret. Until that happens, builds will succeed but
silently skip signing — the updater plugin is loaded but stays inert
(no `latest.json` published, no signatures to verify).

### One-time setup steps

1. **Generate the keypair locally** (any machine with the Tauri CLI):

   ```sh
   cd meeting-recorder-v2
   npx tauri signer generate -w ~/meeting-recorder-updater.key
   ```

   You'll be prompted for a password — pick a strong one, write it
   down. The command produces:
   - A **private key** at `~/meeting-recorder-updater.key`. **Treat as
     secret.** Never commit it.
   - A **public key** printed to stdout (base64). Looks like
     `dW50cnVzdGVkIGNvbW1lbnQ6...`

2. **Commit the public key to `tauri.conf.json`**. Open it, find:

   ```json
   "updater": { ..., "pubkey": "" }
   ```

   Replace the empty string with the public key from step 1.
   Commit + push.

3. **Set GitHub Actions secrets** at
   `https://github.com/joshuarodriguez82/meeting-recorder-v2/settings/secrets/actions`:

   - `TAURI_SIGNING_PRIVATE_KEY` → entire content of
     `~/meeting-recorder-updater.key`
   - `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` → the password you chose

4. **Re-tag v2.4.1** (or cut v2.4.2 if v2.4.1 has already been built
   without signing) so CI re-runs with the secrets configured. After
   the new run finishes, the release should have a `latest.json` file
   attached, and from then on the updater works end-to-end.

5. **Back up `~/meeting-recorder-updater.key`** somewhere safe (a
   password manager). If the private key is lost, you can't ship
   updates that any v2.4.1+ user can install — they'd have to do a
   fresh install from the GitHub releases page using a build with the
   new pubkey. So don't lose it.

## What's also in this release

Nothing else. This is a focused infrastructure release so the next
features can flow to users without manual re-installs. The Android
companion (v3.0 plan) will benefit from this too — once it lands,
it'll have its own update channel that mirrors this same mechanism.

## Install (macOS)

> v2.4.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.4.1_universal.zip`.
>
> The build is still unsigned for Gatekeeper purposes (ad-hoc
> signature doesn't satisfy notarization). First launch needs the
> Gatekeeper bypass as before.
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

Once installed, the v2.4.1 → v2.4.2 update (and every release after
that) will flow through the in-app updater. Last manual install per
machine.
