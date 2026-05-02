#!/usr/bin/env bash
# Inject macOS privacy-usage strings into the built .app's Info.plist.
#
# Tauri 2 doesn't expose Info.plist key injection in tauri.conf.json, so
# we patch the bundle after `npm run tauri build` produces it. macOS will
# refuse to grant mic / calendar / Apple Events access at runtime if these
# strings are missing — the app silently fails on first use instead of
# prompting the user.
#
# Usage:
#   ./scripts/macos-postbuild.sh
#
# Re-run any time you rebuild. Idempotent — overwrites existing values.
# Walks every Meeting Recorder.app under target/ so it works for both
# native (target/release/bundle/macos/...) and target-prefixed
# (target/aarch64-apple-darwin/release/bundle/macos/...) builds, which
# matters in CI where we cross-compile for both Mac architectures.

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "macos-postbuild.sh: not running on macOS, skipping." >&2
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

inject() {
    local plist="$1"
    if [[ ! -f "$plist" ]]; then
        return 1
    fi
    echo "Patching $plist"
    /usr/bin/plutil -replace NSMicrophoneUsageDescription -string \
        "Meeting Recorder captures your meeting audio for transcription and summarization." "$plist"
    /usr/bin/plutil -replace NSCalendarsUsageDescription -string \
        "Meeting Recorder reads your calendar to show upcoming meetings and prefill recording details." "$plist"
    /usr/bin/plutil -replace NSAppleEventsUsageDescription -string \
        "Meeting Recorder uses Mail and Outlook to draft follow-up emails after a meeting." "$plist"
    /usr/bin/plutil -replace LSUIElement -bool false "$plist"
    /usr/bin/plutil -replace NSHighResolutionCapable -bool true "$plist"
}

PATCHED=0
# Find every "Meeting Recorder.app" anywhere under src-tauri/target/ —
# covers default builds, target-triple builds (e.g. aarch64-apple-darwin),
# debug builds, and custom CARGO_TARGET_DIR setups.
while IFS= read -r -d '' bundle; do
    if inject "$bundle/Contents/Info.plist"; then
        PATCHED=$((PATCHED+1))
    fi
done < <(find "$REPO_ROOT/src-tauri/target" -type d -name "Meeting Recorder.app" -print0 2>/dev/null)

if [[ "$PATCHED" -eq 0 ]]; then
    echo "macos-postbuild.sh: no .app bundle found yet. Run \`npm run tauri build\` (or \`npx tauri build\`) first."
    exit 1
fi

echo "Done — patched $PATCHED bundle(s). macOS will prompt for mic / calendar / automation access on first use."
