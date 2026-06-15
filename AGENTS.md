<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Release notes must always include macOS install commands

Release notes live at `docs/release-notes/release-notes-vX.Y.Z.md` (moved out of the repo root in June 2026 — 47 of them were the first thing anyone saw cloning the repo; release.yml's body_path points at the new location). Every release-notes file MUST contain the Mac Gatekeeper-bypass instructions in a callout block near the top. The build is unsigned, so users hit "damaged and can't be opened" on first launch and need explicit guidance.

We ship the macOS app as a **ditto-zipped `.app`** (`Meeting.Recorder_X.Y.Z_universal.zip`), not a `.dmg`. Tauri's `bundle_dmg.sh` is chronically broken on the macos-14 GitHub Actions runner — its AppleScript-against-Finder layout step gives up silently in the headless CI environment. The workflow builds with `--bundles app` and `ditto`-zips the bundle. Apple recommends ditto for un-notarized distribution because it preserves extended attributes and (future) code signatures.

Include BOTH paths so users can pick:

- **Path A — System Settings** (no Terminal): double-click the `.zip` in Finder (Archive Utility auto-extracts to `Meeting Recorder.app`), drag the `.app` to `/Applications`, double-click, dismiss the "damaged" warning, then System Settings → Privacy & Security → Open Anyway, double-click again, click Open.
- **Path B — Terminal:**
  ```sh
  cd ~/Downloads
  unzip -o Meeting.Recorder_*_universal.zip
  mv "Meeting Recorder.app" /Applications/
  xattr -cr "/Applications/Meeting Recorder.app"
  open "/Applications/Meeting Recorder.app"
  ```

Update the version number in the ZIP filename (`Meeting.Recorder_X.Y.Z_universal.zip`) for each release. Mention Windows users can ignore Gatekeeper guidance and just download the `.msi`/`.exe`. Keep this block until the app is signed and notarized — at that point this whole instruction can come out.

# Diagnose with data, not guesses

When ANY release / CI / build / deploy issue is reported, the FIRST step is to read the actual logs and source-of-truth state — not to speculate about the user's local environment. Specifically:

- **Tag points at the wrong commit?** Hit `mcp__github__get_tag` (or list_tags) — that returns the literal SHA the remote has recorded for the tag. Compare to the SHA you expected. Don't ask the user to run `git log -1` and trust the result.
- **Release built wrong artifact?** Hit `mcp__github__get_release_by_tag` — asset filenames + SHAs are right there. Also fetch the workflow run via the GitHub Actions API / `gh run view` so you can see what version variables the build actually saw.
- **CI failed?** Read the run log via the GitHub MCP / Actions API. Look for the exact failing step, not the summary.
- **Filename / version mismatch?** Read the release workflow YAML and find where each artifact's name is computed. Different artifacts may pull from different sources (e.g. tag name vs. `tauri.conf.json.version`) — that's a known source of "macOS says X but Windows says Y" inconsistencies in this repo.

Pattern to apply, in order:
1. Pull the actual artifact / tag / release / run metadata via the GitHub API or MCP.
2. Pull the workflow source that produced it.
3. Cross-reference: which line of YAML produced which filename / which version.
4. Only then propose a fix — and the fix should reference the specific data you observed, not "probably your local checkout is stale."

The 2.7.5 / 2.7.6 / 2.7.7 release runs all tagged the same stale commit `1b523e7`, and the diagnosis of WHY took until v2.7.8 because earlier turns guessed about the user's local state instead of reading the tag's actual SHA from the API. That cost three botched releases. Don't repeat it.
