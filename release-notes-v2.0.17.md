# v2.0.17

## Retention now covers client Designated Folders

When a session's WAV is auto-copied into a client's Designated Folder, the copy is now tracked alongside the original. When retention runs (manually via **Settings → Clean up now** or on your configured interval), **both** the original in `recordings/` and the copy in the client folder age out on the same schedule.

- Processed sessions' audio ages out N days after transcription (default 7).
- Unprocessed sessions' audio ages out M days after recording (default 30).
- Text exports (transcript, summary, action items, decisions, requirements) in the client folder are **never** deleted — that's your permanent archive. Only the bulky WAV goes.
- If you renamed or moved the copy yourself, retention quietly skips it instead of erroring.

## Internals

- `Session` model gains `exported_audio_paths: List[str]`.
- `_auto_export_to_client` records each audio copy it makes on the session and persists before continuing.
- `retention_service.cleanup()` walks those paths alongside the primary `audio_path` and deletes ones over the configured age threshold, counting freed bytes in the usual `{processed_deleted, unprocessed_deleted, bytes_freed}` stats.

## Usage Guide

Retention section updated to explain the new scope.
