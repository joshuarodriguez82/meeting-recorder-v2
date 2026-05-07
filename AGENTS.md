<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# Release notes must always include macOS install commands

Every `release-notes-vX.Y.Z.md` file MUST contain the Mac Gatekeeper-bypass instructions in a callout block near the top. The build is unsigned, so users hit "damaged and can't be opened" on first launch and need explicit guidance. Include BOTH paths so users can pick:

- **Path A — System Settings** (no Terminal): double-click the app, dismiss the "damaged" warning, then System Settings → Privacy & Security → Open Anyway, double-click again, click Open.
- **Path B — Terminal:**
  ```sh
  xattr -cr ~/Downloads/Meeting*.dmg
  open ~/Downloads/Meeting*.dmg
  # drag to Applications, then:
  xattr -cr "/Applications/Meeting Recorder.app"
  open "/Applications/Meeting Recorder.app"
  ```

Update the version number in the DMG filename (`Meeting.Recorder_X.Y.Z_universal.dmg`) for each release. Mention Windows users can ignore Gatekeeper guidance and just download the `.msi`/`.exe`. Keep this block until the app is signed and notarized — at that point this whole instruction can come out.
