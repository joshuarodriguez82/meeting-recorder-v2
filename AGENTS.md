<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Never commit real names, customers, meeting content or personal paths

This repo is **public**. Anyone can clone it. Nothing in it may identify a real
person or a real customer — not in source, not in tests or fixtures, not in
design mockups, not in release notes, not in commit messages, and not in PR
descriptions.

That means none of the following, anywhere:

- **Real people.** Colleagues, meeting organisers, customer contacts. Not full
  names, not surnames, not `Last, First [REGION]` calendar forms, not personal
  usernames.
- **Real organisations.** Customers, prospects, and the employer this app was
  written at.
- **Real meeting content.** Subjects, agendas, attendee lists, transcript
  excerpts, action items — anything captured from an actual calendar or call.
- **Personal identifiers.** Email addresses outside the reserved `example.*`
  domains, and home-directory paths naming a real account
  (`C:\Users\<real-user>\…`, `/Users/<real-user>/…`).

**Field data must be anonymised before it is used as an example.** Real capture
output is the best source of test fixtures precisely because it is awkward, and
that is the trap: the awkwardness is the reason to keep the *shape* and the
reason you must change the *content*. When you anonymise, preserve the property
the example exists to exercise — the comma-and-suffix-and-bracket organiser
form, the non-ASCII diacritics, the pipe or slash inside a subject — and swap in
an obviously fictional name. The repo's placeholders are Acme, Globex, Initech,
Umbrella, Hooli, Zorg and Northwind for organisations, and the Doe / Roe / Poe /
Noh family for people; extend those rather than inventing new plausible-sounding
ones. Replacing one real-looking name with another real-looking name fixes
nothing. Use `C:\Users\<you>\…`, `~/…` and `user@example.com` for paths and
mail.

**Why this rule exists.** In August 2026 a scrub found real colleague and
organiser names in 10 source and test files, real customer names in ~90 places
across 25 files, and the author's personal email and home paths in 4 more. The
worst of it was in `docs/release-notes/*.md` — which `release.yml` feeds to
`body_path` — so those names were **published verbatim in GitHub Release bodies**
and remain in git history. Scrubbing the working tree does not undo either.

`security-scan.yml`'s `personal-data` job blocks a known name or a personal path
from landing again; see `docs/ci-security-and-ai-review.md` for how to add a
term. It only knows what it has been told, so it is a backstop, not a substitute
for not writing the name in the first place.

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
