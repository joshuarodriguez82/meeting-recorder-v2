/**
 * Client for the Python FastAPI backend sidecar.
 *
 * Port is dynamic: the Tauri shell picks a free port at startup
 * (lib.rs::pick_free_port), passes it to Python via the
 * MEETING_RECORDER_PORT env var, and exposes it to JS via the
 * `get_backend_port` Tauri command. The first call to getBaseUrl()
 * resolves it once and caches forever. Falls back to 17645 only when
 * running outside Tauri (e.g. plain `npm run dev` against a manually
 * started backend), since `invoke` won't be available there.
 */

let _baseUrlPromise: Promise<string> | null = null;

function getBaseUrl(): Promise<string> {
  if (_baseUrlPromise) return _baseUrlPromise;
  _baseUrlPromise = (async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const port = await invoke<number>("get_backend_port");
      return `http://127.0.0.1:${port}`;
    } catch {
      return "http://127.0.0.1:17645";
    }
  })();
  return _baseUrlPromise;
}

export interface TemplateEntry {
  name: string;
  prompt: string;
  // True for templates that shipped as built-ins (General, Requirements
  // Gathering, Design Review, Sprint Planning, Stakeholder Update). The
  // UI shows a "Reset to default" button only for these, and delete
  // hides rather than erases so they can be restored.
  is_default: boolean;
  // Original prompt for defaults; null for user-created. Used by the
  // Settings UI to offer "Reset" and to indicate when the current prompt
  // has been edited away from its shipped version.
  default_prompt: string | null;
}

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
  // AI provider selection. "anthropic" uses claude_model + anthropic_api_key;
  // "openai" uses claude_model (reused as the model id), openai_api_key,
  // and openai_base_url (for OpenRouter / Ollama / LM Studio / etc.).
  ai_provider: string;
  openai_api_key: string;
  openai_base_url: string;
  // Streaming live-transcription preview while recording. Default true.
  // When false the live panel doesn't render and the backend doesn't
  // spin up the LiveTranscriber thread (saves CPU).
  live_transcription_enabled: boolean;
  // Auto-stop watchdog. All in minutes except hard_cap_hours; 0 disables
  // each individual trigger. The hard cap is the always-on safety net
  // — defaults to 4 hours so users who forget the recording is running
  // don't end up with overnight files.
  silence_warn_min: number;
  silence_stop_min: number;
  overrun_warn_min: number;
  overrun_stop_min: number;
  hard_cap_hours: number;
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
  // Attendee emails from the calendar meeting this session was recorded
  // against (if any). Used for the auto-tag-client-from-attendees
  // heuristic in the Record view.
  attendees: string[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const baseUrl = await getBaseUrl();
  const res = await fetch(`${baseUrl}${path}`, {
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
  current_status?: string;
  // Auto-stop watchdog: zero or more active warnings. Codes are stable
  // per condition (`dead_air`, `meeting_overrun`, `hard_cap_hit`,
  // `dead_air_stop`, `meeting_overrun_stop`) so the frontend can dedupe
  // its native-OS notifications on code, not on message text.
  warnings?: Array<{
    code: string;
    message: string;
    since_seconds: number;
  }>;
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
  notes: string;
  segments: Array<{ speaker_id: string; start: number; end: number; text: string }>;
  speakers: Record<string, Speaker>;
}

export interface Speaker {
  speaker_id: string;
  display_name: string;
  // Set when this session-local speaker is linked to a persistent
  // SpeakerProfile, either via auto-match after diarize or because the
  // user manually renamed them.
  profile_id?: string | null;
  // Cosine similarity (0-1) when this is an unconfirmed auto-match.
  // null after the user confirms or manually renames.
  match_confidence?: number | null;
  // True after the user has accepted the auto-match (or done a manual
  // rename, which counts as acceptance). Drives whether the UI shows
  // the "(87%) confirm?" badge.
  match_confirmed?: boolean;
}

export interface SpeakerProfile {
  profile_id: string;
  display_name: string;
  created_at: string;
  updated_at: string;
  confirmation_count: number;
  session_count: number;
  sessions_seen_in: string[];
}

export interface UnprocessedSession {
  session_id: string;
  display_name: string;
  started_at: string | null;
  duration_s: number;
  client: string;
  project: string;
}

// One commitment row in the cross-meeting tracker. Same shape the
// /commitments endpoint returns — Commitment.to_dict + the synthetic
// fields (is_overdue, session_*) that the aggregator adds for UI use.
export interface Commitment {
  commitment_id: string;
  session_id: string;
  owner: string;
  side: "internal" | "customer" | "unknown";
  description: string;
  quote: string;
  timestamp_seconds: number;
  due_date_iso: string;
  created_at: string;
  status: "awaiting" | "delivered" | "dismissed";
  resolved_at: string;
  resolved_note: string;
  // Synthetic, computed at query time:
  is_overdue: boolean;
  session_display_name: string;
  session_started_at: string;
  session_client: string;
  session_project: string;
}

// Decision lifecycle. "active" is the implicit default for any
// decision that has no override entry in item_status.json.
export type DecisionStatus = "active" | "implemented" | "superseded";

export interface FollowUpStatusEntry {
  done: boolean;
  updated_at: string;
}

export interface DecisionStatusEntry {
  status: DecisionStatus;
  updated_at: string;
}

// Per-session overlay for "is this follow-up done?" / "where is this
// decision in its lifecycle?". Keys are SHA-1 digests of the normalized
// item text — see `computeItemHash` below for the matching frontend
// hashing rules. Backend is in services/item_status_service.py.
export interface ItemStatusDoc {
  follow_ups: Record<string, FollowUpStatusEntry>;
  decisions: Record<string, DecisionStatusEntry>;
}

// Stable hash for a follow-up or decision item. Must match the Python
// _normalize_item_text + sha1 in item_status_service.py exactly,
// otherwise the override won't apply. Uses Web Crypto SubtleCrypto
// which is available in Tauri's webview.
export async function computeItemHash(text: string): Promise<string> {
  const norm = (text || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[\s\.\,\;\:\!\?]+$/g, "");
  const bytes = new TextEncoder().encode(norm);
  const digest = await crypto.subtle.digest("SHA-1", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// One retrieved chunk that the QA endpoint sent the LLM as context.
// Same shape as a semantic search hit, with the addition that the QA
// view renders these in a sources panel under the streamed answer.
export interface QASource {
  session_id: string;
  display_name: string;
  started_at: string;
  client: string;
  project: string;
  start_s: number;
  end_s: number;
  text: string;
  similarity: number;
}

// Bare-bones SSE event parser. The browser EventSource API does this
// for us — but EventSource is GET-only, and the QA endpoint is POST
// because the body can be 100s of bytes (query + filters). So we
// reimplement just enough to walk SSE event blocks.
//
// SSE format per spec: each event is a sequence of "field: value\n"
// lines, terminated by a blank line. Recognised fields here: `event`
// (defaults to "message" if absent) and `data`. Multi-line `data` is
// concatenated with newlines, but our backend never emits that — every
// event has a single data line, so we just take the first.
function parseSSEEvent(raw: string): { eventName: string; data: string } | null {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) continue;       // comment / heartbeat
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (dataLines.length === 0 && eventName === "message") return null;
  return { eventName, data: dataLines.join("\n") };
}

export interface ProcessFullStages {
  transcribe_diarize?: string;
  summary?: string;
  action_items?: string;
  decisions?: string;
  requirements?: string;
  follow_up_drafts?: string;
}

export const api = {
  health: () => request<{ status: string; version: string }>("/health"),
  // Templates
  getTemplates: () => request<TemplateEntry[]>("/templates"),
  upsertTemplate: (name: string, prompt: string) =>
    request<TemplateEntry>(`/templates/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ prompt }),
    }),
  deleteTemplate: (name: string) =>
    request<{ ok: boolean }>(`/templates/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  resetTemplate: (name: string) =>
    request<TemplateEntry>(`/templates/${encodeURIComponent(name)}/reset`, {
      method: "POST",
    }),

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
    // ISO datetime when the calendar meeting is scheduled to end.
    // Optional; only meaningful when the recording was started from a
    // calendar tile. The backend's auto-stop watchdog uses this for
    // the meeting-overrun trigger.
    scheduled_end_iso?: string;
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

  // One-shot pipeline — used by auto_process_after_stop. Runs transcribe,
  // diarize, summary, action items, decisions, requirements sequentially.
  // Each stage succeeds or fails independently; response.stages shows which.
  processFull: (
    id: string,
    opts: { template?: string; follow_up_drafts?: boolean } = {},
  ) =>
    request<{ ok: boolean; stages: ProcessFullStages }>(
      `/sessions/${id}/process_full`,
      {
        method: "POST",
        body: JSON.stringify({
          template: opts.template ?? "General",
          follow_up_drafts: opts.follow_up_drafts ?? false,
        }),
      },
    ),

  followUpDrafts: (id: string, tone = "friendly-professional") =>
    request<{ ok: boolean; drafts_created: number }>(
      `/sessions/${id}/follow_up_drafts`,
      { method: "POST", body: JSON.stringify({ tone }) },
    ),

  // Sessions with audio on disk but no transcript — polled by the UI so
  // we can badge the sidebar and fire a Windows toast when the count rises.
  listUnprocessed: () =>
    request<UnprocessedSession[]>("/sessions/unprocessed"),

  getSessionFull: (id: string) =>
    request<SessionFull>(`/sessions/${id}`),

  patchSession: (id: string, patch: {
    display_name?: string;
    client?: string;
    project?: string;
    template?: string;
    notes?: string;
  }) =>
    request<{ ok: boolean }>(`/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  renameSpeaker: (
    session_id: string,
    speaker_id: string,
    display_name: string,
    save_profile: boolean = true,
  ) =>
    request<{
      ok: boolean;
      speaker: Speaker;
      // What the backend did with the cross-session profile store:
      //   "created" — new profile saved from this voice
      //   "linked"  — linked to an existing same-named profile
      //   "refined" — already-linked profile got refined
      //   "skipped" — couldn't fingerprint (see profile_skip_reason)
      profile_action: "created" | "linked" | "refined" | "skipped";
      profile_skip_reason: string | null;
    }>(
      `/sessions/${session_id}/speakers/${encodeURIComponent(speaker_id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ display_name, save_profile }),
      }
    ),

  confirmSpeakerMatch: (
    session_id: string,
    speaker_id: string,
    profile_id: string,
  ) =>
    request<{ ok: boolean; speaker: Speaker }>(
      `/sessions/${session_id}/speakers/${encodeURIComponent(speaker_id)}/confirm`,
      {
        method: "POST",
        body: JSON.stringify({ profile_id }),
      }
    ),

  rejectSpeakerMatch: (session_id: string, speaker_id: string) =>
    request<{ ok: boolean; speaker: Speaker }>(
      `/sessions/${session_id}/speakers/${encodeURIComponent(speaker_id)}/reject`,
      { method: "POST" }
    ),

  // ── Semantic search ───────────────────────────────────────────────
  semanticSearch: (
    query: string,
    top_k: number = 10,
    client?: string,
    project?: string,
  ) =>
    request<{
      query: string;
      results: Array<{
        session_id: string;
        display_name: string;
        started_at: string;
        client: string;
        project: string;
        start_s: number;
        end_s: number;
        text: string;
        similarity: number;
      }>;
    }>("/search/semantic", {
      method: "POST",
      body: JSON.stringify({ query, top_k, client, project }),
    }),

  searchIndexStatus: () =>
    request<{
      available: boolean;
      total_sessions: number;
      indexed_sessions: number;
      model_id: string;
    }>("/search/index/status"),

  embedSession: (session_id: string) =>
    request<{ embedded: boolean; session_id: string }>(
      `/sessions/${session_id}/embed`,
      { method: "POST" },
    ),

  searchIndexBackfill: (limit: number = 50) =>
    request<{
      embedded: string[];
      embedded_count: number;
      remaining: number;
    }>(`/search/index/backfill?limit=${limit}`, { method: "POST" }),

  // ── Cross-meeting Q&A ─────────────────────────────────────────────
  // POST + SSE so we need fetch + ReadableStream parsing (EventSource
  // only supports GET). Returns an abort handle so the caller can cancel
  // a long-running answer mid-stream when the user clicks Stop or
  // navigates away.
  qaStream: (
    body: { query: string; top_k?: number; client?: string; project?: string },
    handlers: {
      onSources: (sources: QASource[]) => void;
      onText: (text: string) => void;
      onDone: () => void;
      onError: (msg: string) => void;
    },
  ): { abort: () => void } => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(`${await getBaseUrl()}/qa/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        if (!res.ok) {
          const text = await res.text().catch(() => `${res.status}`);
          handlers.onError(text);
          return;
        }
        if (!res.body) {
          handlers.onError("No stream body");
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        // Buffer accumulates raw SSE text until we have at least one
        // full event (terminated by a blank line, per spec).
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          // Walk forward; an event is everything up to the next "\n\n".
          let split: number;
          while ((split = buf.indexOf("\n\n")) !== -1) {
            const raw = buf.slice(0, split);
            buf = buf.slice(split + 2);
            const event = parseSSEEvent(raw);
            if (!event) continue;
            if (event.eventName === "sources") {
              try {
                handlers.onSources(JSON.parse(event.data));
              } catch { /* malformed */ }
            } else if (event.eventName === "done") {
              handlers.onDone();
              return;
            } else if (event.eventName === "error") {
              try {
                handlers.onError(JSON.parse(event.data).error || "Unknown error");
              } catch {
                handlers.onError(event.data || "Unknown error");
              }
              return;
            } else {
              // Default "message" event = text fragment chunk
              try {
                const payload = JSON.parse(event.data);
                if (payload.text) handlers.onText(payload.text);
              } catch { /* heartbeat or comment line */ }
            }
          }
        }
        handlers.onDone();
      } catch (e) {
        if ((e as DOMException)?.name === "AbortError") return;
        handlers.onError(e instanceof Error ? e.message : String(e));
      }
    })();
    return { abort: () => controller.abort() };
  },

  // ── In-call AI search (live transcript as context) ───────────────
  // Same SSE framing as qaStream but no `sources` event — the only
  // context is the inline blob the caller passes in.
  qaInlineStream: (
    body: { query: string; context: string },
    handlers: {
      onText: (text: string) => void;
      onDone: () => void;
      onError: (msg: string) => void;
    },
  ): { abort: () => void } => {
    const controller = new AbortController();
    (async () => {
      try {
        const res = await fetch(`${await getBaseUrl()}/qa/inline-stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        if (!res.ok) {
          const text = await res.text().catch(() => `${res.status}`);
          handlers.onError(text);
          return;
        }
        if (!res.body) {
          handlers.onError("No stream body");
          return;
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let split: number;
          while ((split = buf.indexOf("\n\n")) !== -1) {
            const raw = buf.slice(0, split);
            buf = buf.slice(split + 2);
            const event = parseSSEEvent(raw);
            if (!event) continue;
            if (event.eventName === "done") {
              handlers.onDone();
              return;
            } else if (event.eventName === "error") {
              try {
                handlers.onError(JSON.parse(event.data).error || "Unknown");
              } catch {
                handlers.onError(event.data || "Unknown error");
              }
              return;
            } else {
              try {
                const payload = JSON.parse(event.data);
                if (payload.text) handlers.onText(payload.text);
              } catch { /* heartbeat / comment */ }
            }
          }
        }
        handlers.onDone();
      } catch (e) {
        if ((e as DOMException)?.name === "AbortError") return;
        handlers.onError(e instanceof Error ? e.message : String(e));
      }
    })();
    return { abort: () => controller.abort() };
  },

  // ── Cross-session speaker profiles ────────────────────────────────
  listSpeakerProfiles: () => request<SpeakerProfile[]>("/speaker-profiles"),
  renameSpeakerProfile: (profile_id: string, display_name: string) =>
    request<SpeakerProfile>(
      `/speaker-profiles/${encodeURIComponent(profile_id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ display_name }),
      }
    ),
  deleteSpeakerProfile: (profile_id: string) =>
    request<{ ok: boolean }>(
      `/speaker-profiles/${encodeURIComponent(profile_id)}`,
      { method: "DELETE" }
    ),
  mergeSpeakerProfiles: (profile_ids: string[], display_name: string) =>
    request<SpeakerProfile>("/speaker-profiles/merge", {
      method: "POST",
      body: JSON.stringify({ profile_ids, display_name }),
    }),

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

  // ── Commitments tracker ───────────────────────────────────────────
  listCommitments: (filters: {
    client?: string;
    project?: string;
    status?: string;   // comma-separated; supports "active" + "overdue" synthetic codes
    owner?: string;
    side?: "internal" | "customer" | "unknown";
  } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) {
      if (v) qs.set(k, v);
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return request<{ commitments: Commitment[] }>(`/commitments${suffix}`);
  },
  updateCommitment: (
    commitment_id: string,
    status: "awaiting" | "delivered" | "dismissed",
    note?: string,
  ) =>
    request<Commitment>(
      `/commitments/${encodeURIComponent(commitment_id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ status, note: note || "" }),
      },
    ),
  extractSessionCommitments: (session_id: string) =>
    request<{ ok: boolean; extracted: number }>(
      `/sessions/${encodeURIComponent(session_id)}/extract-commitments`,
      { method: "POST" },
    ),

  // ── Item status overlays (per-session "checked off" state) ────────
  // Follow-ups and decisions are parsed at render time from the LLM's
  // markdown output, so there's no first-class record to flip a flag
  // on. We persist a side-table of {item_hash → status} per session and
  // overlay it at render time.
  listAllItemStatus: () =>
    request<{ sessions: Record<string, ItemStatusDoc> }>("/item-status"),
  getItemStatus: (session_id: string) =>
    request<ItemStatusDoc>(
      `/sessions/${encodeURIComponent(session_id)}/item-status`,
    ),
  setFollowUpDone: (session_id: string, item_hash: string, done: boolean) =>
    request<ItemStatusDoc>(
      `/sessions/${encodeURIComponent(session_id)}/item-status`,
      {
        method: "PATCH",
        body: JSON.stringify({ type: "follow_up", item_hash, done }),
      },
    ),
  setDecisionStatus: (
    session_id: string,
    item_hash: string,
    status: DecisionStatus,
  ) =>
    request<ItemStatusDoc>(
      `/sessions/${encodeURIComponent(session_id)}/item-status`,
      {
        method: "PATCH",
        body: JSON.stringify({ type: "decision", item_hash, status }),
      },
    ),

  // Calendar-tile-driven prep brief: richer prompt + structured response
  // with referenced session metadata for click-to-jump rendering.
  prepBriefFromMeeting: (body: {
    subject: string;
    attendees: string[];
    scheduled_start_iso: string;
    scheduled_end_iso?: string;
    client: string;
    project: string;
  }) =>
    request<{
      markdown: string;
      referenced_sessions: Array<{
        session_id: string;
        display_name: string;
        started_at: string | null;
      }>;
      related_count: number;
      identified_client: string;
      identified_project: string;
      last_meeting_at: string | null;
    }>("/prep-brief/from-meeting", {
      method: "POST",
      body: JSON.stringify(body),
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

  // GPU acceleration
  getGpuStatus: () => request<{
    current: string;
    detected: {
      nvidia: boolean; amd: boolean; intel: boolean;
      gpus: string[]; recommended: string;
      // Set on macOS by the backend so the frontend can swap the CUDA
      // card for an MPS card. Absent on Windows / Linux.
      platform?: string;
      apple_silicon?: boolean;
    };
    task: {
      running: boolean; phase: string; message: string;
      progress_lines: string[];
    };
    python_exe: string;
  }>("/gpu/status"),
  installGpuBackend: (backend: "cpu" | "cuda" | "directml") =>
    request<{ ok: boolean; backend: string }>("/gpu/install", {
      method: "POST",
      body: JSON.stringify({ backend }),
    }),

  // Calendar
  getCalendarToday: () => request<Meeting[]>("/calendar/today"),
  getUpcomingMeetings: (hours: number = 168, refresh = false) =>
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

  // Filesystem helpers
  openFolder: (body: { kind: "recordings" | "client" | "path"; client?: string; path?: string }) =>
    request<{ ok: boolean; path: string }>("/system/open-folder", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  importSession: (body: {
    file_path: string;
    display_name?: string;
    client?: string;
    project?: string;
  }) =>
    request<{ ok: boolean; session_id: string }>("/sessions/import", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Per-client configs (designated export folders, etc.)
  getClientConfigs: () =>
    request<Record<string, { export_folder: string }>>("/clients/config"),
  setClientConfig: (name: string, cfg: { export_folder: string }) =>
    request<{ ok: boolean; export_folder: string }>(
      `/clients/config/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify(cfg) }
    ),

  exportSession: (id: string) =>
    request<{ ok: boolean; target_dir: string; paths: string[] }>(
      `/sessions/${id}/export`, { method: "POST" }
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
