// Which files can be imported as a session, and what the import window
// says about them. Mirrors backend/core/media_import.py's SUPPORTED_EXTS —
// the backend is the authority and refuses anything else with a 400.

export const VIDEO_EXTENSIONS = [
  "mp4", "mov", "m4v", "mkv", "webm", "avi", "wmv",
] as const;

export const AUDIO_EXTENSIONS = [
  "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "wma",
] as const;

export const IMPORT_EXTENSIONS: readonly string[] = [
  ...AUDIO_EXTENSIONS, ...VIDEO_EXTENSIONS,
];

/** The file name of a Windows or POSIX path. */
export function fileNameOf(path: string): string {
  const parts = path.trim().split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
}

export function extensionOf(path: string): string {
  const name = fileNameOf(path);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** The name a session gets when none is typed: the file name without
 *  its extension, as the backend does. */
export function defaultMeetingName(path: string): string {
  const name = fileNameOf(path);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

export function isImportable(path: string): boolean {
  return IMPORT_EXTENSIONS.includes(extensionOf(path));
}

export function isVideo(path: string): boolean {
  return (VIDEO_EXTENSIONS as readonly string[]).includes(extensionOf(path));
}

/** From a drag-and-drop, the first file that can be imported. */
export function pickImportable(paths: readonly string[]): string | null {
  return paths.find(isImportable) ?? null;
}

// Transcripts Teams / Zoom download, imported with a recording or alone.
// Mirrors backend/core/transcript_import.py's TRANSCRIPT_EXTS.
export const TRANSCRIPT_EXTENSIONS = ["vtt", "srt", "docx", "txt"] as const;

export function isTranscript(path: string): boolean {
  return (TRANSCRIPT_EXTENSIONS as readonly string[]).includes(extensionOf(path));
}

/** One meeting to import: a recording, its transcript, or both. */
export interface ImportItem {
  recording: string;
  transcript: string;
  name: string;
}

/** The meeting an item will be called when no name is typed. */
export function itemName(item: ImportItem): string {
  return item.name.trim()
    || defaultMeetingName(item.recording || item.transcript);
}

/**
 * Files picked or dropped together, as meetings to import. A recording
 * and a transcript with the same file name ("Weekly sync.mp4" and
 * "Weekly sync.vtt") are one meeting; anything else is its own. Files
 * that can't be imported are returned separately so the window can say
 * which. Adding to an existing list fills in a missing half before
 * starting a new meeting, and never adds the same file twice.
 */
export function groupImportFiles(
  paths: readonly string[],
  existing: readonly ImportItem[] = [],
): { items: ImportItem[]; rejected: string[] } {
  const items = existing.map((i) => ({ ...i }));
  const rejected: string[] = [];
  const have = new Set(
    items.flatMap((i) => [i.recording, i.transcript]).filter(Boolean));
  const stem = (p: string) => defaultMeetingName(p).toLowerCase();

  for (const raw of paths) {
    const p = raw.trim();
    if (!p || have.has(p)) continue;
    const recording = isImportable(p);
    const transcript = isTranscript(p);
    if (!recording && !transcript) {
      rejected.push(p);
      continue;
    }
    have.add(p);
    const slot = recording ? "recording" : "transcript";
    const other = recording ? "transcript" : "recording";
    const partner = items.find(
      (i) => !i[slot] && i[other] && stem(i[other]) === stem(p));
    if (partner) {
      partner[slot] = p;
    } else {
      items.push({
        recording: recording ? p : "",
        transcript: transcript ? p : "",
        name: "",
      });
    }
  }
  return { items, rejected };
}
