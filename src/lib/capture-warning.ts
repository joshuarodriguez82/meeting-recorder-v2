/**
 * Turning a live capture warning into something the user actually sees.
 *
 * FIELD REPORT 2026-09-15: a meeting was recorded with only the
 * microphone — everyone else on the call missing — because system audio
 * was refused at the first second. The backend knew. The only place it
 * said so was a banner on the Record tab, and during an auto-recorded
 * meeting nobody is on the Record tab; they are in the meeting.
 *
 * So the warning now travels: a native notification and a toast that
 * stays until dismissed, raised from the page shell (mounted on every
 * tab), plus a label on the sidebar's recording strip. The backend sends
 * a stable `capture_warning_code` (core/capture_health.py) alongside the
 * message; this module decides, per status poll, whether that code is
 * new for this recording and what to call it.
 *
 * Pure so it is testable without a DOM — see vitest.config.ts for why
 * there isn't one.
 */

import type { RecordingStatus } from "./api";

/** Must match the constants in backend/core/capture_health.py. The
 *  vitest suite reads that file to hold them together. */
export const MIC_DEAD = "mic_dead";
export const SYSTEM_AUDIO_UNAVAILABLE = "system_audio_unavailable";
export const SYSTEM_AUDIO_DEAD = "system_audio_dead";

type StatusFields = Pick<
  RecordingStatus,
  "is_recording" | "session_id" | "capture_warning" | "capture_warning_code"
>;

export interface CaptureAnnouncement {
  /** Identity for "already announced": one per recording per code. */
  key: string;
  title: string;
  body: string;
}

/** Heading for the warning — the consequence, in the user's terms. */
export function captureWarningTitle(code: string | null | undefined): string {
  switch (code) {
    case SYSTEM_AUDIO_UNAVAILABLE:
      return "Other participants aren't being recorded";
    case MIC_DEAD:
      return "Your microphone isn't being recorded";
    case SYSTEM_AUDIO_DEAD:
      return "System audio stopped";
    default:
      return "Capture problem detected";
  }
}

/** Short form for the sidebar strip, which has one line to spare. */
export function captureWarningShortLabel(
  code: string | null | undefined,
): string {
  switch (code) {
    case SYSTEM_AUDIO_UNAVAILABLE:
      return "Only your mic is recording";
    case MIC_DEAD:
      return "No microphone audio";
    case SYSTEM_AUDIO_DEAD:
      return "System audio stopped";
    default:
      return "Capture problem";
  }
}

/**
 * The announcement this poll should raise, or null.
 *
 * Once per (recording, code): the status is polled every couple of
 * seconds and a warning that re-notifies on each poll is one people
 * learn to ignore. A different code in the same recording — the mic
 * dying after system audio was already refused — is a new fact and is
 * announced. The caller records `key` after announcing.
 */
export function captureAnnouncement(
  status: StatusFields | null | undefined,
  alreadyAnnounced: ReadonlySet<string>,
): CaptureAnnouncement | null {
  if (!status?.is_recording || !status.capture_warning) return null;
  const code = status.capture_warning_code || "capture_warning";
  const key = `${status.session_id ?? "?"}:${code}`;
  if (alreadyAnnounced.has(key)) return null;
  return {
    key,
    title: captureWarningTitle(status.capture_warning_code),
    body: status.capture_warning,
  };
}
