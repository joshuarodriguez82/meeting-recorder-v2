/**
 * Client for the Python FastAPI backend sidecar.
 * Runs at http://127.0.0.1:17645 alongside the Tauri app.
 */

const BASE_URL = "http://127.0.0.1:17645";

export interface Settings {
  anthropic_api_key: string;
  hf_token: string;
  whisper_model: string;
  max_speakers: number;
  recordings_dir: string;
  email_to: string;
  claude_model: string;
  notify_minutes_before: number;
  auto_process_after_stop: boolean;
  launch_on_startup: boolean;
  auto_follow_up_email: boolean;
  retention_enabled: boolean;
  retention_processed_days: number;
  retention_unprocessed_days: number;
  is_configured: boolean;
}

export interface AudioDevice {
  index: number;
  name: string;
  max_input_channels?: number;
  max_output_channels?: number;
  channels?: number;
  default_samplerate: number;
}

export interface Meeting {
  subject: string;
  start: string;
  end: string;
  location: string;
  organizer: string;
  attendees: string[];
  duration: number;
}

export interface SessionSummary {
  session_id: string;
  display_name: string;
  started_at: string;
  ended_at: string | null;
  duration_s: number;
  audio_path: string | null;
  audio_exists: boolean;
  has_transcript: boolean;
  has_summary: boolean;
  has_action_items: boolean;
  has_requirements: boolean;
  has_decisions: boolean;
  client: string;
  project: string;
  action_items: string;
  summary: string;
  decisions: string;
  requirements: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

export interface RecordingStatus {
  is_recording: boolean;
  session_id: string | null;
  started_at: string | null;
  duration_s: number;
  models_ready: boolean;
  models_loading: boolean;
  models_error: string | null;
}

export interface SessionFull {
  session_id: string;
  display_name: string;
  started_at: string | null;
  ended_at: string | null;
  audio_path: string | null;
  summary: string | null;
  action_items: string | null;
  requirements: string | null;
  decisions: string | null;
  template: string;
  client: string;
  project: string;
  attendees: string[];
  segments: Array<{ speaker_id: string; start: number; end: number; text: string }>;
  speakers: Record<string, { speaker_id: string; display_name: string }>;
}

export const api = {
  health: () => request<{ status: string; version: string }>("/health"),
  getTemplates: () => request<string[]>("/templates"),

  // Recording
  recordingStatus: () => request<RecordingStatus>("/recording/status"),
  startRecording: (body: {
    mic_device_index: number | null;
    output_device_index: number | null;
    meeting_name: string;
    template: string;
    client: string;
    project: string;
    attendees: string[];
  }) =>
    request<{ session_id: string }>("/recording/start", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  stopRecording: () =>
    request<{ session_id: string; audio_path: string }>("/recording/stop", {
      method: "POST",
    }),
  loadModels: () =>
    request<{ loading: boolean }>("/models/load", { method: "POST" }),

  // AI extraction
  processSession: (id: string) =>
    request<{ ok: boolean; segments: number; speakers: number }>(
      `/sessions/${id}/process`, { method: "POST" }
    ),
  summarize: (id: string, template: string) =>
    request<{ ok: boolean; summary: string }>(
      `/sessions/${id}/summarize`,
      { method: "POST", body: JSON.stringify({ template }) }
    ),
  actionItems: (id: string) =>
    request<{ ok: boolean; action_items: string }>(
      `/sessions/${id}/action-items`, { method: "POST" }
    ),
  requirements: (id: string) =>
    request<{ ok: boolean; requirements: string }>(
      `/sessions/${id}/requirements`, { method: "POST" }
    ),
  decisions: (id: string) =>
    request<{ ok: boolean; decisions: string }>(
      `/sessions/${id}/decisions`, { method: "POST" }
    ),

  getSessionFull: (id: string) =>
    request<SessionFull>(`/sessions/${id}`),

  patchSession: (id: string, patch: {
    display_name?: string;
    client?: string;
    project?: string;
    template?: string;
  }) =>
    request<{ ok: boolean }>(`/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  renameSpeaker: (session_id: string, speaker_id: string, display_name: string) =>
    request<{ ok: boolean; speaker_id: string; display_name: string }>(
      `/sessions/${session_id}/speakers/${encodeURIComponent(speaker_id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ display_name }),
      }
    ),

  bulkTag: (session_ids: string[], client?: string, project?: string) =>
    request<{ updated: number }>("/tags/apply", {
      method: "POST",
      body: JSON.stringify({ session_ids, client, project }),
    }),

  suggestTagging: (client: string, project = "") =>
    request<{ suggestions: Array<{
      session_id: string;
      display_name: string;
      started_at: string;
      confidence: number;
      reason: string;
    }> }>("/clients/suggest-tagging", {
      method: "POST",
      body: JSON.stringify({ client, project }),
    }),

  prepBrief: (subject: string, client: string, project: string) =>
    request<{ brief: string; related_count: number }>("/prep-brief", {
      method: "POST",
      body: JSON.stringify({ subject, client, project }),
    }),


  getSessionRaw: (id: string) =>
    request<Record<string, unknown>>(`/sessions/${id}`),

  // Settings
  getSettings: () => request<Settings>("/settings"),
  saveSettings: (s: Settings) =>
    request<{ ok: boolean }>("/settings", {
      method: "POST",
      body: JSON.stringify(s),
    }),

  // Audio devices
  getAudioDevices: () =>
    request<{ input: AudioDevice[]; output: AudioDevice[] }>("/audio/devices"),

  // Calendar
  getCalendarToday: () => request<Meeting[]>("/calendar/today"),
  getUpcomingMeetings: (hours: number = 36, refresh = false) =>
    request<Meeting[]>(
      `/calendar/upcoming?hours=${hours}${refresh ? "&refresh=true" : ""}`
    ),
  isCalendarAvailable: () =>
    request<{ available: boolean }>("/calendar/available"),

  // Sessions
  listSessions: () => request<SessionSummary[]>("/sessions"),
  deleteSession: (id: string) =>
    request<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" }),

  // Retention
  getRetentionStats: () =>
    request<{ total_bytes: number; session_count: number; wav_count: number }>(
      "/retention/stats"
    ),
  runRetentionCleanup: (processed_days: number, unprocessed_days: number) =>
    request<{
      deleted_count: number;
      bytes_freed: number;
      processed_deleted: number;
      unprocessed_deleted: number;
      orphans_deleted: number;
    }>(
      `/retention/cleanup?processed_days=${processed_days}&unprocessed_days=${unprocessed_days}`,
      { method: "POST" }
    ),
};

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function formatDuration(seconds: number): string {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}
