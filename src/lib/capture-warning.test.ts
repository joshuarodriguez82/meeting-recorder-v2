/**
 * A capture warning has to reach someone who is not on the Record tab.
 *
 * FIELD REPORT 2026-09-15: system audio was refused at the start of a
 * meeting. The backend knew within a second; the user found out by
 * chance, mid-meeting, because the only signal was a banner on a tab
 * nobody looks at during an auto-recorded call.
 *
 * The payload below is not hand-written. It was captured from the real
 * GET /recording/status handler in that state, and
 * backend/tests/test_capture_health.py::
 * test_the_frontend_fixture_is_what_the_endpoint_sends holds it to the
 * endpoint — so if the backend renames the field, that test goes red
 * rather than this one staying green against a payload nobody sends.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import type { RecordingStatus } from "./api";
import {
  MIC_DEAD,
  SYSTEM_AUDIO_DEAD,
  SYSTEM_AUDIO_UNAVAILABLE,
  captureAnnouncement,
  captureWarningShortLabel,
  captureWarningTitle,
} from "./capture-warning";
import FIELD_STATUS from "./__fixtures__/recording-status-system-audio-unavailable.json";

const field = FIELD_STATUS as unknown as RecordingStatus;

describe("announcing a refused system-audio open", () => {
  it("announces the field payload", () => {
    const a = captureAnnouncement(field, new Set());
    expect(a).not.toBeNull();
    expect(a!.title).toBe("Other participants aren't being recorded");
    expect(a!.body).toBe(field.capture_warning);
  });

  it("carries the backend's remedy, not a generic restart", () => {
    const a = captureAnnouncement(field, new Set())!;
    expect(a.body).toContain("Default Format");
  });

  it("announces once per recording, not once per poll", () => {
    const seen = new Set<string>();
    const first = captureAnnouncement(field, seen)!;
    seen.add(first.key);
    expect(captureAnnouncement(field, seen)).toBeNull();
    expect(captureAnnouncement({ ...field }, seen)).toBeNull();
  });

  it("does not re-announce when only the wording changes", () => {
    const seen = new Set<string>();
    seen.add(captureAnnouncement(field, seen)!.key);
    const reworded = { ...field, capture_warning: "something else" };
    expect(captureAnnouncement(reworded, seen)).toBeNull();
  });

  it("announces a second, different problem in the same recording", () => {
    const seen = new Set<string>();
    seen.add(captureAnnouncement(field, seen)!.key);
    const micDied = {
      ...field,
      capture_warning: "No microphone audio for 50 seconds",
      capture_warning_code: MIC_DEAD,
    };
    const a = captureAnnouncement(micDied, seen);
    expect(a?.title).toBe("Your microphone isn't being recorded");
  });

  it("announces again for the next recording", () => {
    const seen = new Set<string>();
    seen.add(captureAnnouncement(field, seen)!.key);
    const next = { ...field, session_id: "NEXTMEET" };
    expect(captureAnnouncement(next, seen)).not.toBeNull();
  });
});

describe("when there is nothing to announce", () => {
  it("stays quiet without a warning", () => {
    const ok = { ...field, capture_warning: null, capture_warning_code: null };
    expect(captureAnnouncement(ok, new Set())).toBeNull();
  });

  it("stays quiet once the recording has stopped", () => {
    const stopped = { ...field, is_recording: false };
    expect(captureAnnouncement(stopped, new Set())).toBeNull();
  });

  it("stays quiet before the first poll", () => {
    expect(captureAnnouncement(null, new Set())).toBeNull();
    expect(captureAnnouncement(undefined, new Set())).toBeNull();
  });

  it("still announces a warning that arrives without a code", () => {
    const uncoded = { ...field, capture_warning_code: null };
    const a = captureAnnouncement(uncoded, new Set());
    expect(a?.title).toBe("Capture problem detected");
  });
});

describe("titles", () => {
  it.each([
    [SYSTEM_AUDIO_UNAVAILABLE, "Other participants aren't being recorded",
      "Only your mic is recording"],
    [MIC_DEAD, "Your microphone isn't being recorded", "No microphone audio"],
    [SYSTEM_AUDIO_DEAD, "System audio stopped", "System audio stopped"],
    ["something_new", "Capture problem detected", "Capture problem"],
  ])("%s", (code, title, short) => {
    expect(captureWarningTitle(code)).toBe(title);
    expect(captureWarningShortLabel(code)).toBe(short);
  });
});

describe("the codes are the backend's codes", () => {
  // Prefer the producer: read the constants out of the Python module
  // rather than restating them. A rename there without a rename here
  // would otherwise leave every warning titled "Capture problem".
  const PY = readFileSync(
    join(process.cwd(), "backend/core/capture_health.py"), "utf8");

  it.each([
    ["MIC_DEAD", MIC_DEAD],
    ["SYSTEM_AUDIO_UNAVAILABLE", SYSTEM_AUDIO_UNAVAILABLE],
    ["SYSTEM_AUDIO_DEAD", SYSTEM_AUDIO_DEAD],
  ])("%s", (name, value) => {
    expect(PY).toMatch(new RegExp(`^${name} = "${value}"$`, "m"));
  });
});
