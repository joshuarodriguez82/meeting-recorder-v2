import { describe, expect, it } from "vitest";
import {
  defaultMeetingName, extensionOf, fileNameOf, isImportable, isVideo,
  pickImportable,
} from "./media-import";

describe("media import file handling", () => {
  it("reads names from Windows and macOS paths", () => {
    expect(fileNameOf("C:\\Users\\<you>\\Downloads\\Acme sync.mp4"))
      .toBe("Acme sync.mp4");
    expect(fileNameOf("~/Downloads/Acme sync.MOV")).toBe("Acme sync.MOV");
    expect(extensionOf("~/Downloads/Acme sync.MOV")).toBe("mov");
  });

  it("names the meeting after the file, keeping dots inside the name", () => {
    expect(defaultMeetingName("C:\\x\\Q3 review v1.2.mp4"))
      .toBe("Q3 review v1.2");
    expect(defaultMeetingName("~/notes")).toBe("notes");
  });

  it("knows a video from audio", () => {
    expect(isVideo("a.mp4")).toBe(true);
    expect(isVideo("a.WEBM")).toBe(true);
    expect(isVideo("a.m4a")).toBe(false);
    expect(isImportable("a.m4a")).toBe(true);
    expect(isImportable("a.docx")).toBe(false);
    expect(isImportable(".mp4")).toBe(false);
  });

  it("takes the first importable file from a drop", () => {
    expect(pickImportable(["~/agenda.docx", "~/call.mp4", "~/b.wav"]))
      .toBe("~/call.mp4");
    expect(pickImportable(["~/agenda.docx"])).toBeNull();
    expect(pickImportable([])).toBeNull();
  });
});
