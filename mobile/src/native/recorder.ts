import { registerPlugin, Capacitor } from "@capacitor/core";

// Native plugin contract. Implemented in Kotlin (RecorderPlugin.kt) and
// stubbed for the browser in createWebFallback() below so `npm run dev`
// can drive the whole UI without an Android device.
export interface PermissionState {
  microphone: "granted" | "denied" | "prompt";
  notifications: "granted" | "denied" | "prompt";
}

// Which AudioSource actually captured. "VOICE_CALL" = the device gave
// us the real call stream (both sides, even off-speaker). MIC /
// UNPROCESSED / VOICE_RECOGNITION = acoustic capture (far end needs
// speakerphone). null until a recording has started.
export type AudioSource =
  | "VOICE_CALL"
  | "VOICE_RECOGNITION"
  | "UNPROCESSED"
  | "MIC"
  | "WEB"
  | null;

export interface RecordingStatus {
  recording: boolean;
  sessionId: string | null;
  audioSource: AudioSource;
  // Milliseconds since the foreground service started capturing. Lets
  // the UI restore a live timer after the app was backgrounded/killed
  // while the service kept running.
  elapsedMs: number;
}

export interface StopResult {
  // Absolute path to the finished .m4a in the app cache dir. Not yet in
  // the synced folder — call writeSession() to commit it.
  path: string;
  durationMs: number;
  audioSource: AudioSource;
}

export interface WriteSessionOptions {
  treeUri: string;
  // Canonical stem, e.g. "session_AB12CD34" — the native side appends
  // .m4a / .json so both land next to each other in the synced folder.
  baseName: string;
  json: string;
  audioPath: string;
}

export interface SyncedSession {
  name: string; // "session_AB12CD34.json"
  json: string; // raw file contents
}

export interface RecorderPlugin {
  checkPermissions(): Promise<PermissionState>;
  requestPermissions(): Promise<PermissionState>;

  // SAF: one-time folder grant. Returns the persisted tree URI string,
  // or rejects if the user cancelled the picker.
  pickFolder(): Promise<{ treeUri: string; label: string }>;
  // True when we still hold a persisted grant for treeUri (the user can
  // revoke it from Android Settings, or a factory-level OneDrive reset
  // can drop it).
  hasFolderAccess(opts: { treeUri: string }): Promise<{ ok: boolean }>;

  // captureMode "auto" (default): try the call-audio ladder
  // (VOICE_CALL → VOICE_RECOGNITION → UNPROCESSED → MIC) and report
  // which the device allowed. "mic": force plain mic / speakerphone.
  startRecording(opts: { sessionId: string; captureMode?: "auto" | "mic" }): Promise<void>;
  stopRecording(): Promise<StopResult>;
  getStatus(): Promise<RecordingStatus>;

  writeSession(opts: WriteSessionOptions): Promise<{ audioUri: string; jsonUri: string }>;
  // Enumerate session_*.json already in the synced folder so the
  // Recordings tab can show what the desktop has and hasn't processed.
  listSyncedSessions(opts: { treeUri: string }): Promise<{ sessions: SyncedSession[] }>;
  // Read a single text file (used for client_configs.json) or null.
  readTextFile(opts: { treeUri: string; name: string }): Promise<{ content: string | null }>;
}

export const isNative = Capacitor.isNativePlatform();

export const Recorder = registerPlugin<RecorderPlugin>("Recorder", {
  web: () => createWebFallback(),
});

// --- Browser fallback -------------------------------------------------
// Records via MediaRecorder, "writes" by triggering a download, and
// keeps the synced-folder list in memory. Enough to click through every
// screen during `npm run dev`; never ships to the phone.
function createWebFallback(): RecorderPlugin {
  let mediaRecorder: MediaRecorder | null = null;
  let chunks: Blob[] = [];
  let startedAt = 0;
  let sessionId: string | null = null;
  let lastBlobUrl: string | null = null;
  const memSessions: SyncedSession[] = [];

  const perm: PermissionState = {
    microphone: "prompt",
    notifications: "granted",
  };

  return {
    async checkPermissions() {
      return perm;
    },
    async requestPermissions() {
      try {
        const s = await navigator.mediaDevices.getUserMedia({ audio: true });
        s.getTracks().forEach((t) => t.stop());
        perm.microphone = "granted";
      } catch {
        perm.microphone = "denied";
      }
      return perm;
    },
    async pickFolder() {
      return { treeUri: "web://downloads", label: "Browser downloads (dev only)" };
    },
    async hasFolderAccess() {
      return { ok: true };
    },
    async startRecording(opts) {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size) chunks.push(e.data);
      };
      mediaRecorder.start();
      startedAt = Date.now();
      sessionId = opts.sessionId;
    },
    async stopRecording() {
      const mr = mediaRecorder;
      if (!mr) throw new Error("not recording");
      const durationMs = Date.now() - startedAt;
      await new Promise<void>((resolve) => {
        mr.onstop = () => resolve();
        mr.stop();
        mr.stream.getTracks().forEach((t) => t.stop());
      });
      const blob = new Blob(chunks, { type: "audio/webm" });
      if (lastBlobUrl) URL.revokeObjectURL(lastBlobUrl);
      lastBlobUrl = URL.createObjectURL(blob);
      mediaRecorder = null;
      return { path: lastBlobUrl, durationMs, audioSource: "WEB" as const };
    },
    async getStatus() {
      return {
        recording: mediaRecorder !== null,
        sessionId,
        audioSource: (mediaRecorder ? "WEB" : null) as "WEB" | null,
        elapsedMs: mediaRecorder ? Date.now() - startedAt : 0,
      };
    },
    async writeSession(opts) {
      memSessions.unshift({ name: `${opts.baseName}.json`, json: opts.json });
      const a = document.createElement("a");
      a.href = opts.audioPath;
      a.download = `${opts.baseName}.webm`;
      a.click();
      const jb = new Blob([opts.json], { type: "application/json" });
      const ju = URL.createObjectURL(jb);
      const a2 = document.createElement("a");
      a2.href = ju;
      a2.download = `${opts.baseName}.json`;
      a2.click();
      setTimeout(() => URL.revokeObjectURL(ju), 5000);
      return { audioUri: opts.audioPath, jsonUri: ju };
    },
    async listSyncedSessions() {
      return { sessions: memSessions };
    },
    async readTextFile() {
      return { content: null };
    },
  };
}
