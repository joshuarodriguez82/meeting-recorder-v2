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

export const api = {
  health: () => request<{ status: string; version: string }>("/health"),

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
  isCalendarAvailable: () =>
    request<{ available: boolean }>("/calendar/available"),

  // Sessions
  listSessions: () => request<SessionSummary[]>("/sessions"),
  getSession: (id: string) => request<Record<string, unknown>>(`/sessions/${id}`),
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
