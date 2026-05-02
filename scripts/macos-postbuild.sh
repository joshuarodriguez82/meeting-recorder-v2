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

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "macos-postbuild.sh: not running on macOS, skipping." >&2
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_BUNDLE="$REPO_ROOT/src-tauri/target/release/bundle/macos/Meeting Recorder.app"
DEBUG_BUNDLE="$REPO_ROOT/src-tauri/target/debug/bundle/macos/Meeting Recorder.app"

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
for bundle in "$APP_BUNDLE" "$DEBUG_BUNDLE"; do
    if [[ -d "$bundle" ]]; then
        inject "$bundle/Contents/Info.plist" && PATCHED=$((PATCHED+1)) || true
    fi
done

if [[ "$PATCHED" -eq 0 ]]; then
    echo "macos-postbuild.sh: no .app bundle found yet. Run \`npm run tauri build\` first."
    exit 1
fi

echo "Done. macOS will prompt for mic / calendar / automation access on first use."
