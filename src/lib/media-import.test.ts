import { describe, expect, it } from "vitest";
import {
  defaultMeetingName, extensionOf, fileNameOf, groupImportFiles,
  isImportable, isTranscript, isVideo, itemName, pickImportable,
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

  it("tells transcripts from recordings", () => {
    expect(isTranscript("t.vtt")).toBe(true);
    expect(isTranscript("t.DOCX")).toBe(true);
    expect(isTranscript("t.mp4")).toBe(false);
    expect(isImportable("t.vtt")).toBe(false);
  });
});

describe("grouping dropped files into meetings", () => {
  it("pairs a recording with the transcript of the same name", () => {
    const { items, rejected } = groupImportFiles([
      "C:\\d\\Weekly sync.mp4",
      "C:\\d\\Globex kickoff.mp4",
      "C:\\d\\weekly SYNC.vtt",
      "C:\\d\\agenda.pdf",
    ]);
    expect(items).toEqual([
      { recording: "C:\\d\\Weekly sync.mp4",
        transcript: "C:\\d\\weekly SYNC.vtt", name: "" },
      { recording: "C:\\d\\Globex kickoff.mp4", transcript: "", name: "" },
    ]);
    expect(rejected).toEqual(["C:\\d\\agenda.pdf"]);
  });

  it("imports a transcript on its own", () => {
    const { items } = groupImportFiles(["~/Hooli review.docx"]);
    expect(items).toEqual([
      { recording: "", transcript: "~/Hooli review.docx", name: "" }]);
    expect(itemName(items[0])).toBe("Hooli review");
  });

  it("adds to an existing list without duplicates, filling a missing half", () => {
    const first = groupImportFiles(["~/a.mp4"]).items;
    first[0].name = "Kept name";
    const { items } = groupImportFiles(["~/a.mp4", "~/a.vtt", "~/b.wav"], first);
    expect(items).toEqual([
      { recording: "~/a.mp4", transcript: "~/a.vtt", name: "Kept name" },
      { recording: "~/b.wav", transcript: "", name: "" },
    ]);
  });

  it("never pairs two recordings", () => {
    const { items } = groupImportFiles(["~/a.mp4", "~/a.m4a"]);
    expect(items).toHaveLength(2);
  });
});
