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
