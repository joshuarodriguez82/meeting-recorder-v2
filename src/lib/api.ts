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

/**
 * Per-launch shared secret between the Tauri shell, the Python sidecar,
 * and this frontend (lib.rs::generate_backend_token). 127.0.0.1 is not
 * an auth boundary — any browser tab on the machine can reach the
 * sidecar's port — so every request must present this token. Resolved
 * once and cached, same lifecycle as the port. Empty string outside
 * Tauri (plain `npm run dev`), where the manually-started backend has
 * no MEETING_RECORDER_TOKEN env var and runs with auth disabled.
 */
let _authTokenPromise: Promise<string> | null = null;

function getAuthToken(): Promise<string> {
  if (_authTokenPromise) return _authTokenPromise;
  _authTokenPromise = (async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke<string>("get_backend_token");
    } catch {
      return "";
    }
  })();
  return _authTokenPromise;
}

/**
 * `?token=...` (or `&token=...`) suffix for endpoints consumed without
 * request headers: EventSource and <audio>/<img> `src` URLs. Everything
 * that goes through fetch should use the Authorization header instead —
 * query strings end up in uvicorn's access log on the user's machine.
 */
async function authQuery(hasQuery = false): Promise<string> {
  const token = await getAuthToken();
  if (!token) return "";
  return `${hasQuery ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

/** Authorization header for the fetch paths (request + the SSE POSTs). */
async function authHeaders(): Promise<Record<string, string>> {
  const token = await getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Open an http(s) URL in the user's real browser. A plain
 * <a target="_blank"> does nothing in the Tauri webview, so the
 * "Join meeting" link (and any external link) needs this. Falls back
 * to window.open outside Tauri (plain `npm run dev`).
 */
/**
 * Tell the Tauri shell whether a recording is in progress.
 *
 * The shell's backend watchdog kills an unresponsive backend so it can
 * be replaced — correct when idle, catastrophic mid-recording, because
 * the meeting can't be re-run. The Rust side can't ask the backend
 * (it's unreachable precisely when the watchdog is deciding), so the
 * UI pushes the state down. Best-effort: outside Tauri this is a no-op.
 */
export async function setRecordingActive(active: boolean): Promise<void> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("set_recording_active", { active });
  } catch {
    /* not running under Tauri, or the command isn't available */
  }
}

export async function openExternal(url: string): Promise<void> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("open_external", { url });
  } catch {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

/**
 * Open an OS Settings / Control Panel applet by short tag. Only known
 * panels listed in the Rust allowlist (currently just "sound") are
 * honoured — there's no path-passing API surface so the WebView can't
 * shellexec anything. Used by the Record-view audio-sync-risk banner
 * to deep-link the user into Sound Control Panel (Windows) / Sound
 * preferences (macOS) to fix a default-format mismatch.
 */
export async function openSystemSettings(panel: "sound"): Promise<void> {
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("open_system_settings", { panel });
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
  // Auto-screenshot cadence during recording. 0 = off (manual button
  // only). When > 0 the record view fires capture_screenshot on a
  // setInterval while a session is active so summaries include
  // periodic visual context without the user remembering to click.
  auto_screenshot_interval_minutes: number;
  // Auto-stop watchdog. All in minutes except hard_cap_hours; 0 disables
  // each individual trigger. The hard cap is the always-on safety net
  // — defaults to 4 hours so users who forget the recording is running
  // don't end up with overnight files.
  silence_warn_min: number;
  silence_stop_min: number;
  overrun_warn_min: number;
  overrun_stop_min: number;
  hard_cap_hours: number;
  // Calendar-driven auto-start. When true, a backend loop polls the
  // user's calendar every 30s and starts a recording at the scheduled
  // start time of any non-all-day event with a Teams/Zoom/Meet link.
  // Manual recordings always win — auto-start is suppressed while a
  // recording is already in progress.
  auto_record_enabled: boolean;
  // Live in-call co-pilot panel. When true, the Record view polls
  // /recording/copilot/tick every ~45s and renders three short bullet
  // lists alongside the live transcript. Defaults false — costs LLM
  // calls during every recording, so users opt in.
  live_copilot_enabled: boolean;
  // Optional separate LLM for the live co-pilot. Empty strings (the
  // default) mean "reuse the main provider/model/key". Set
  // live_ai_provider="openai" + base_url to point ticks at local
  // Ollama or a free OpenRouter model while the post-meeting summary
  // keeps running on the main (paid) provider.
  live_ai_provider: string;
  live_claude_model: string;
  live_openai_api_key: string;
  live_openai_base_url: string;
  live_anthropic_api_key: string;
  // Active co-pilot persona + meeting-type modifier names. Resolved
  // through the mode / meeting-type libraries (separate from the
  // summary-template library). Default: SA + General.
  live_copilot_mode: string;
  live_copilot_meeting_type: string;
  // Polling intervals (seconds). Wide tick uses the full window
  // (~10 min); hot tick uses ~90s only and biases empty. Hot=0
  // disables the hot tier entirely.
  live_copilot_wide_interval_sec: number;
  live_copilot_hot_interval_sec: number;
  // Free-text the SA pins per-engagement — appended to every co-pilot
  // tick prompt as authoritative role / topic framing. Lets the user
  // tighten suggestions without code changes ("focus on Genesys
  // migration", "client is healthcare, PHI compliance matters").
  copilot_custom_context: string;
  // Opt-in toggle for the "Today" daily-briefing tab. OFF by default —
  // depends on the user running an M365 Copilot scheduled prompt and
  // pasting its output in. When false the Today nav item is hidden and
  // the app lands on Record instead.
  today_view_enabled: boolean;
  // Auto pre-meeting brief: generate + notify before each meeting.
  // OFF by default (one LLM call/meeting). Lead = minutes before start.
  auto_prep_brief_enabled: boolean;
  auto_prep_brief_lead_min: number;
  // Cloud Mirror: root network folder for background per-client session
  // exports (Google Drive / NAS). Empty = off. The recordings folder
  // itself must stay on a LOCAL disk — this is the safe path to cloud.
  cloud_mirror_dir: string;
  // Session Archive: roaming folder for session JSONs (transcript,
  // summary, action items — never audio) so one library shows up on
  // every machine pointed at the same synced folder (iCloud/OneDrive/
  // Drive). Empty = off. Field report 2026-08-07 — this used to only
  // be settable via the SESSION_ARCHIVE_DIR env var; it's a first-class
  // Settings field now with its own card in Settings.
  session_archive_dir: string;
  // Speech-boundary (VAD) chunking for the live transcript instead of
  // fixed 15s windows — the difference between text appearing ~1-3s
  // after someone finishes talking vs ~15s. Default true; false falls
  // back to the legacy fixed-window path (field report 2026-08-10,
  // Zoom notetaker parity).
  live_vad_enabled: boolean;
  // Device the speaker-identification (pyannote diarization) pipeline
  // loads on. "auto" (default) preserves existing behavior: prefer your
  // GPU (CUDA, then Apple Silicon MPS), else CPU. "cpu" forces CPU —
  // workaround for a field-reported crash where the transcription model
  // and the diarization model both try to use the GPU at the same time,
  // a few seconds after a recording stops. "cuda" forces GPU, falling
  // back to CPU with a warning if no GPU is present.
  diarization_device: "auto" | "cpu" | "cuda" | string;
}

// A single co-pilot mode (persona) or meeting-type (modifier). Shape
// mirrors TemplateEntry on the summary side so the UI editor pattern
// is reusable.
export interface CoPilotPromptEntry {
  name: string;
  prompt: string;
  is_default: boolean;
  default_prompt: string | null;
}

export interface CoPilotTickResponse {
  clarifying_questions: string[];
  risks: string[];
  follow_ups: string[];
  segment_count: number;
  generated_at: string;
  // Set when the model call failed (so the panel can explain the quiet
  // instead of looking like an empty meeting). "timeout" = model too
  // slow (often a local model under load), "unreachable" = can't connect
  // (e.g. Ollama not running), "error" = other. Absent on success.
  error?: "timeout" | "unreachable" | "error" | null;
  error_detail?: string | null;
  hot?: boolean;
}

export interface AudioDevice {
  index: number;
  name: string;
  max_input_channels?: number;
  max_output_channels?: number;
  channels?: number;
  default_samplerate: number;
}

export interface AudioFormat {
  sample_rate: number;
  bits_per_sample: number;
  channels: number;
}

export interface AudioSyncRisk {
  // ok=false implies level="warn" and the banner renders.
  ok: boolean;
  // "ok" — both devices use the same shared-mode mix format.
  // "warn" — mismatch detected; drift on long recordings expected.
  // "unknown" — non-Windows or pycaw missing; banner stays hidden.
  level: "ok" | "warn" | "unknown";
  reason: string | null;
  mic_format: AudioFormat | null;
  loopback_format: AudioFormat | null;
  fix_hint: string | null;
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
  // Compact speaker_id -> display_name map for sessions where the user
  // has renamed speakers. Used by Follow-ups (and other list views) to
  // resolve owner labels like "SPEAKER_03" back to the real person.
  // Empty / missing when no speakers have been renamed.
  speakers?: Record<string, string>;
  // Audio integrity — set when the WAV duration meaningfully disagrees
  // with the recording window (ended_at - started_at). UI surfaces a
  // warning chip on the session so the user knows the audio is
  // incomplete before they trust the recording. null = healthy.
  audio_integrity_warning?: string | null;
  audio_actual_duration_s?: number | null;
  audio_expected_duration_s?: number | null;
  // Set when backend auto-processing exhausted its retries. UI badges the
  // session so a processing failure is visible instead of the session
  // silently sitting unprocessed. Cleared on a successful (re)process.
  processing_error?: string | null;
  // Read-only sync-integrity finding: mic/system-audio drift or dropped
  // frames vs wall-clock, measured at stop. Informational (no audio
  // altered). null = clean.
  sync_warning?: string | null;
}

/**
 * RFC 7807 Problem Details — what the backend returns on every error
 * path now (see backend/server.py). `type` is a stable URI identifying
 * the error class so callers can branch on it without string-matching
 * `detail`. Extensions like `errors` (validation) and
 * `exception_class` (unhandled) ride alongside the standard fields.
 */
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  // Class-specific extensions are permitted by RFC 7807 §3.2; surface
  // them through index access rather than enumerating each one.
  [extension: string]: unknown;
}

/** Error type the api.* helpers throw on non-2xx responses. Carries
 * the parsed Problem document so callers that want to show field-level
 * validation errors or branch on `problem.type` can do so. */
export class ApiError extends Error {
  status: number;
  problem?: ProblemDetail;
  constructor(message: string, status: number, problem?: ProblemDetail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const baseUrl = await getBaseUrl();
  const res = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(await authHeaders()),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const ct = res.headers.get("content-type") || "";
    const bodyText = await res.text().catch(() => "");
    if (ct.includes("application/problem+json") && bodyText) {
      try {
        const problem = JSON.parse(bodyText) as ProblemDetail;
        const detail = problem.detail || "";
        const msg = detail
          ? `${problem.title}: ${detail}`
          : problem.title || `${res.status}`;
        throw new ApiError(msg, res.status, problem);
      } catch (e) {
        if (e instanceof ApiError) throw e;
        // JSON.parse failed — fall through to the plain-text path.
      }
    }
    throw new ApiError(
      `${res.status}: ${bodyText || res.statusText}`, res.status);
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
  // Set by AutoRecordService when it (rather than the user) started the
  // current recording — the frontend uses it for the "Auto-recording:
  // <subject>" toast/native notification and the persistent
  // recording-badge label. Null on manual recordings.
  auto_record_subject?: string | null;
  // One-shot reason emitted when AutoRecordService had to skip a
  // meeting (e.g. no saved mic device). Backend clears it after one
  // read, so observing a non-null value means "show this to the user
  // once and move on."
  auto_record_skip_reason?: string | null;
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
  // Absolute paths to screenshots captured during the meeting. Persisted
  // with the session and fed to the summarizer as visual context.
  screenshots?: string[];
  // Saved Live Co-Pilot ticks — one entry per coaching pass the panel
  // made during the recording. Persisted with the session so the
  // bullets the model produced mid-call survive past the meeting.
  copilot_ticks?: CoPilotTickResponse[];
  // Audio integrity — same fields as on SessionSummary, see above.
  audio_integrity_warning?: string | null;
  audio_actual_duration_s?: number | null;
  audio_expected_duration_s?: number | null;
  processing_error?: string | null;
  sync_warning?: string | null;
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

export interface ClientExportStatus {
  client: string;
  folder: string;
  folder_present: boolean;
  total: number;
  exportable: number;
  mirrored: number;
  pending: { session_id: string; display_name: string; missing: string[] }[];
}

export interface DocumentSkip {
  file: string;
  reason: string;
}

export interface KnowledgeReindexReport {
  indexed: number;
  unchanged: number;
  skipped: DocumentSkip[];
  total_chunks: number;
  removed_stale: number;
}

export interface ClientKnowledgeStatus {
  client: string;
  knowledge_folder: string;
  folder_present: boolean;
  indexed_documents: number;
  total_chunks: number;
}

// Session Archive: roaming-library status. Mirrors ClientExportStatus's
// shape (folder / folder_present / counts) since it's the same
// convergence pattern, one level simpler — one artifact (the session
// JSON) per session instead of five independently-earned ones.
// Per-file roaming status for client_configs.json / summary_templates.json
// (field report 2026-08-07: these lived alongside the recordings dir but
// were never part of the session-JSON archive copy, so a client that
// exists only in client_configs.json — no tagged meeting yet — plus its
// Designated/Knowledge Folder settings, plus custom summary templates,
// never made it to the second machine). Mirrors
// backend/services/shared_state_sync.py's status() dict shape exactly.
export interface SharedStateFileStatus {
  local_present: boolean;
  archive_present: boolean;
  local_mtime: number | null;
  archive_mtime: number | null;
  direction: "push" | "pull" | "in-sync" | "absent";
  reason: string | null;
  // client_configs.json only: per-machine folder paths (export_folder /
  // knowledge_folder) that the last reconcile sweep cleared because they
  // were structurally foreign to this platform (e.g. a Windows drive
  // letter roamed onto a Mac). Field report 2026-08-07. Absent/empty on
  // summary_templates.json, which carries no filesystem paths.
  sanitized_cleared?: { client: string; field: string; old_value: string }[];
}

export interface ArchiveStatus {
  folder: string;
  folder_present: boolean;
  sessions_in_archive: number;
  sessions_local: number;
  pending: number;
  // Keyed by filename ("client_configs.json", "summary_templates.json").
  shared_state?: Record<string, SharedStateFileStatus>;
}

// GET /sessions/diagnostics — mirrors SessionService.scan_report() plus
// the two fields the endpoint adds (primary_dir, visible_in_app).
// Field report 2026-08-10: a user with 74 session files on disk saw 24
// in the app, and the only way anyone found out why was a PowerShell
// script talked through over an evening — the backend had this exact
// answer the whole time and nothing in the UI surfaced it.
export interface SessionsDiagnosticsRoot {
  path: string;
  session_files: number;
  // True only for a root that failed mid-scan (see unreachable_roots
  // for the error text); healthy roots always carry `false`.
  unreachable: boolean;
}

export interface SessionsDiagnosticsSkip {
  path: string;
  reason: string;
}

export interface SessionsDiagnosticsUnreachableRoot {
  path: string;
  error: string;
}

export interface SessionsDiagnostics {
  roots: SessionsDiagnosticsRoot[];
  // Session-file candidates found across every root, before any were
  // skipped for being unreadable/un-hydrated/non-dict.
  total: number;
  skipped: number;
  // Capped server-side at 50 entries — see scan_report()'s docstring.
  skipped_detail: SessionsDiagnosticsSkip[];
  unreachable_roots: SessionsDiagnosticsUnreachableRoot[];
  // The active RECORDINGS_DIR — the one root writes go to.
  primary_dir: string;
  // What list_sessions() actually returns right now (post-dedupe,
  // post-skip) — i.e. what the Sessions list shows the user.
  visible_in_app: number;
}

export const api = {
  // Resolve the cached backend base URL. Exposed for non-`request`
  // callers that need the URL directly — most notably the live
  // transcript SSE EventSource, which can't go through `request`.
  getBaseUrl,
  // Auth-token suffix for header-less consumers (EventSource, <audio>/
  // <img> src). Pass hasQuery=true when the path already contains `?`.
  authQuery,
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
    // Conference room mode: skip system-audio loopback entirely (mic
    // captures everyone in the room, nobody is on speakers). Backend
    // ignores output_device_index when this is true.
    conference_room_mode?: boolean;
  }) =>
    request<{ session_id: string }>("/recording/start", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  stopRecording: () =>
    request<{ session_id: string; audio_path: string }>("/recording/stop", {
      method: "POST",
    }),
  // Live Co-Pilot tick. Backend reads the last ~10 min of live transcript
  // segments and asks the LLM for three short bullet lists. 403 means
  // the user hasn't turned the feature on; 409 means no active recording.
  copilotTick: () =>
    request<CoPilotTickResponse>("/recording/copilot/tick", {
      method: "POST",
    }),
  // Pulls every persisted tick on the active session so the panel can
  // rehydrate after a reload — otherwise the bullets vanish until the
  // next 45s tick fires.
  copilotHistory: () =>
    request<{ ticks: CoPilotTickResponse[] }>("/recording/copilot/history"),

  // Co-Pilot mode + meeting-type libraries. Editable persona/modifier
  // prompts the user picks from at recording time; same edit/reset/
  // delete semantics as the summary template library.
  getCopilotModes: () =>
    request<CoPilotPromptEntry[]>("/copilot/modes"),
  putCopilotMode: (name: string, prompt: string) =>
    request<CoPilotPromptEntry>(`/copilot/modes/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ prompt }),
    }),
  deleteCopilotMode: (name: string) =>
    request<{ ok: boolean }>(`/copilot/modes/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  resetCopilotMode: (name: string) =>
    request<CoPilotPromptEntry>(
      `/copilot/modes/${encodeURIComponent(name)}/reset`, { method: "POST" }),

  getCopilotMeetingTypes: () =>
    request<CoPilotPromptEntry[]>("/copilot/meeting-types"),
  putCopilotMeetingType: (name: string, prompt: string) =>
    request<CoPilotPromptEntry>(
      `/copilot/meeting-types/${encodeURIComponent(name)}`, {
        method: "PUT",
        body: JSON.stringify({ prompt }),
      }),
  deleteCopilotMeetingType: (name: string) =>
    request<{ ok: boolean }>(
      `/copilot/meeting-types/${encodeURIComponent(name)}`, {
        method: "DELETE",
      }),
  resetCopilotMeetingType: (name: string) =>
    request<CoPilotPromptEntry>(
      `/copilot/meeting-types/${encodeURIComponent(name)}/reset`, {
        method: "POST",
      }),

  // Lightweight setter — flips active mode and/or meeting type without
  // rebuilding RecordingService (the full POST /settings would orphan
  // active capture threads). Lets the panel change them mid-recording.
  setCopilotActive: (mode?: string, meetingType?: string) =>
    request<{ mode: string; meeting_type: string }>(
      "/settings/copilot-active", {
        method: "POST",
        body: JSON.stringify({ mode, meeting_type: meetingType }),
      }),

  // Promote a single co-pilot bullet to an artifact on the active
  // session. Idempotent on exact-text — repeated clicks don't duplicate.
  saveCopilotSuggestion: (
    kind: "follow_up" | "decision" | "note", text: string,
  ) =>
    request<{ ok: boolean; kind: string }>(
      "/recording/copilot/save", {
        method: "POST",
        body: JSON.stringify({ kind, text }),
      }),

  // Hot variant of copilotTick — narrower window, tighter prompt,
  // biased toward empty. Frontend can poll this every ~15s alongside
  // the wide tick for just-in-time coaching. Same response shape.
  copilotHotTick: () =>
    request<CoPilotTickResponse>("/recording/copilot/hot-tick", {
      method: "POST",
    }),
  // Full live-transcript segment history for the active recording.
  // Lets the LiveTranscriptPanel rehydrate after a tab switch instead
  // of starting empty and only catching segments published from that
  // moment forward. Returns 409 when no recording is active.
  transcriptHistory: () =>
    request<{ segments: Array<{ start: number; end: number; text: string; speaker?: string }> }>(
      "/recording/transcript/history",
    ),
  // Lightweight setter for live_copilot_enabled only. Unlike the full
  // POST /settings, this endpoint works while a recording is in
  // progress so the user can flip the panel on/off from the Record bar.
  setLiveCopilotEnabled: (enabled: boolean) =>
    request<{ live_copilot_enabled: boolean }>("/settings/live-copilot", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  loadModels: () =>
    request<{ loading: boolean }>("/models/load", { method: "POST" }),

  // Screenshots — destination dir comes from the backend (it owns the
  // per-session folder + bookkeeping); the actual capture happens in
  // the Tauri/Rust shell, then we register the saved path here.
  getScreenshotDir: () =>
    request<{ dir: string; session_id: string | null }>(
      "/recording/screenshot/dir"),
  attachScreenshot: (path: string) =>
    request<{ ok: boolean; count: number }>("/recording/screenshot", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  // "Never auto-record this meeting" list. Two layers: exact `subjects`
  // (a recurring series stays blocked on every occurrence) and
  // case-insensitive substring `patterns` (e.g. "canceled" catches
  // "Canceled: Weekly Sync" and any other meeting whose title contains
  // the word). The backend returns both on every mutation.
  getAutoRecordBlocklist: () =>
    request<{ subjects: string[]; patterns: string[] }>(
      "/auto-record/blocklist"),
  addAutoRecordBlocklist: (subject: string) =>
    request<{ ok: boolean; subjects: string[]; patterns: string[] }>(
      "/auto-record/blocklist", {
        method: "POST",
        body: JSON.stringify({ subject }),
      }),
  removeAutoRecordBlocklist: (subject: string) =>
    request<{ ok: boolean; subjects: string[]; patterns: string[] }>(
      "/auto-record/blocklist", {
        method: "DELETE",
        body: JSON.stringify({ subject }),
      }),
  addAutoRecordBlocklistPattern: (pattern: string) =>
    request<{ ok: boolean; subjects: string[]; patterns: string[] }>(
      "/auto-record/blocklist/patterns", {
        method: "POST",
        body: JSON.stringify({ subject: pattern }),
      }),
  removeAutoRecordBlocklistPattern: (pattern: string) =>
    request<{ ok: boolean; subjects: string[]; patterns: string[] }>(
      "/auto-record/blocklist/patterns", {
        method: "DELETE",
        body: JSON.stringify({ subject: pattern }),
      }),

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
          headers: {
            "Content-Type": "application/json",
            ...(await authHeaders()),
          },
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
          headers: {
            "Content-Type": "application/json",
            ...(await authHeaders()),
          },
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

  prepBrief: (subject: string, client: string, project: string, userContext = "") =>
    request<{ brief: string; related_count: number }>("/prep-brief", {
      method: "POST",
      body: JSON.stringify({
        subject, client, project, user_context: userContext,
      }),
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
    body?: string;
    user_context?: string;
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

  // Session Archive: roaming-library status + "Sync now". Status is
  // polled after Settings loads and after every sync; sync is
  // idempotent — a fully archived library queues 0.
  getArchiveStatus: () => request<ArchiveStatus>("/sessions/archive-status"),
  syncArchive: () =>
    request<ArchiveStatus & { queued: number }>(
      "/sessions/archive/sync", { method: "POST" }),

  // Audio devices
  getAudioDevices: () =>
    request<{ input: AudioDevice[]; output: AudioDevice[] }>("/audio/devices"),

  // Mic↔loopback format mismatch check. Backed by WASAPI on Windows
  // (pycaw); returns level="unknown" on macOS / Linux where the OS
  // handles format conversion transparently. The Record-view banner
  // only renders when level === "warn".
  getAudioSyncRisk: (mic: string, loopback: string) =>
    request<AudioSyncRisk>(
      "/audio/sync-risk?mic=" + encodeURIComponent(mic) +
      "&loopback=" + encodeURIComponent(loopback)),

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
  // Lazy per-meeting detail (agenda/body, attendees, parsed join link).
  // Fetched only when the user opens a meeting so the bulk list stays
  // fast — see the backend endpoint comment.
  getMeetingDetail: (subject: string, start: string) =>
    request<{ attendees: string[]; body: string; join_url: string | null }>(
      `/calendar/meeting-detail?subject=${encodeURIComponent(subject)}`
      + `&start=${encodeURIComponent(start)}`
    ),
  isCalendarAvailable: () =>
    request<{ available: boolean }>("/calendar/available"),

  // Auto-record loop status. `enabled` mirrors Settings.auto_record_enabled;
  // `running` confirms the backend loop is actually live (they can briefly
  // disagree during a settings toggle). `next_event` is null when nothing
  // qualifies in the next 24h.
  getAutoRecordStatus: () =>
    request<{
      enabled: boolean;
      running: boolean;
      next_event: {
        subject: string;
        start: string;
        end: string;
        location: string;
      } | null;
    }>("/recording/auto-status"),

  // Sessions
  listSessions: () => request<SessionSummary[]>("/sessions"),
  deleteSession: (id: string) =>
    request<{ ok: boolean }>(`/sessions/${id}`, { method: "DELETE" }),
  // Where the app is looking for sessions, how many files each root
  // holds, and what it had to skip — see SessionsDiagnostics.
  getSessionsDiagnostics: () =>
    request<SessionsDiagnostics>("/sessions/diagnostics"),

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

  // Ghost sessions: session_*.json files whose audio_path target doesn't
  // exist on disk. Accumulate when the backend crashes mid-recording or
  // mid-finalize — v2.11.1's JSON-first writes leave the stub behind, but
  // the WAV never landed. Backend auto-purges stubs older than 14 days at
  // startup; the UI cleanup button removes whatever's left.
  listGhostSessions: () =>
    request<{
      count: number;
      auto_purge_age_days: number;
      items: {
        session_id: string;
        display_name: string;
        json_path: string;
        json_mtime_iso: string;
        age_days: number;
        audio_path: string;
      }[];
    }>("/ghost-sessions"),
  deleteGhostSessions: (body: { session_ids?: string[]; min_age_days?: number }) =>
    request<{ deleted: string[]; errors: { session_id: string; error: string }[] }>(
      "/ghost-sessions",
      { method: "DELETE", body: JSON.stringify(body) }
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

  // Live free-model roster (OpenRouter). Empty list => UI keeps its
  // bundled fallback. Never throws into the caller's happy path.
  getFreeModels: (provider: string) =>
    request<{ models: { value: string; label: string }[] }>(
      `/models/free?provider=${encodeURIComponent(provider)}`),

  // Live model list for the configured (or query-overridden) AI
  // provider. Polls the provider's own /v1/models (or native
  // equivalent) so new model releases appear in the Settings dropdown
  // without an app update. The UI uses this on Settings open; falls
  // back to its hardcoded GEMINI_MODELS / GROQ_MODELS / etc. lists
  // when source !== "live". Cached server-side for 5 min so opening
  // settings repeatedly doesn't pound the provider.
  getAvailableModels: (
    provider?: string, baseUrl?: string, scope?: "main" | "live",
  ) => {
    // scope="live" routes the backend to read live_* settings keys
    // (live_anthropic_api_key, live_openai_api_key, live_openai_base_url)
    // so the Live Co-Pilot Settings card can populate its own model
    // dropdown without colliding with the main provider's keys.
    const params = new URLSearchParams();
    if (provider) params.set("provider", provider);
    if (baseUrl) params.set("base_url", baseUrl);
    if (scope && scope !== "main") params.set("scope", scope);
    const qs = params.toString();
    return request<{
      models: { value: string; label: string }[];
      source: "live" | "cache" | "empty";
      provider: string;
      error?: string;
      age_seconds?: number;
    }>(`/providers/available-models${qs ? `?${qs}` : ""}`);
  },

  // Per-client configs (designated export folder, knowledge folder, etc.)
  getClientConfigs: () =>
    request<Record<string, {
      export_folder: string;
      knowledge_folder?: string;
      display_name?: string;
    }>>("/clients/config"),
  // Both fields are optional — the endpoint merges onto the existing
  // stored config, so the Designated Folder card and the Knowledge
  // Folder card can each PUT only the field they own without wiping
  // the other one out.
  setClientConfig: (name: string, cfg: { export_folder?: string; knowledge_folder?: string }) =>
    request<{
      ok: boolean; export_folder: string; knowledge_folder: string;
      // Setting a folder backfills everything already tagged to this
      // client, so the response reports what it queued.
      queued?: number; mirrored?: number; exportable?: number;
    }>(
      `/clients/config/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify(cfg) }
    ),

  // Designated-Folder mirror status: how many of this client's meetings
  // have their artifacts on disk in the folder, and which don't.
  getClientExportStatus: (name: string) =>
    request<ClientExportStatus>(
      `/clients/${encodeURIComponent(name)}/export-status`),
  // Idempotent "Sync now" — queues an export for anything missing.
  reconcileClientExports: (name: string) =>
    request<ClientExportStatus & { queued: number }>(
      `/clients/${encodeURIComponent(name)}/reconcile`, { method: "POST" }
    ),

  // Knowledge Folder: per-client document search + Q&A index.
  getClientKnowledge: (name: string) =>
    request<ClientKnowledgeStatus>(
      `/clients/${encodeURIComponent(name)}/knowledge`),
  reindexClientKnowledge: (name: string) =>
    request<KnowledgeReindexReport>(
      `/clients/${encodeURIComponent(name)}/knowledge/reindex`,
      { method: "POST" }
    ),

  exportSession: (id: string) =>
    request<{ ok: boolean; target_dir: string; paths: string[] }>(
      `/sessions/${id}/export`, { method: "POST" }
    ),

  // Cross-meeting analytics for the Insights view. Computed
  // server-side from session JSONs + commitments/item-status
  // sidecars. The result is small (a few KB at most), so we don't
  // bother with caching on the client.
  getInsights: (params: { since?: string; until?: string; client?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.since) q.set("since", params.since);
    if (params.until) q.set("until", params.until);
    if (params.client) q.set("client", params.client);
    const qs = q.toString();
    return request<InsightsSummary>(
      `/insights/summary${qs ? `?${qs}` : ""}`);
  },

  // Engagement register: structured records rolled up across every
  // session for a client (optionally one project), deduped.
  engagementRegister: (client: string, project = "") => {
    const q = new URLSearchParams();
    if (project) q.set("project", project);
    const qs = q.toString();
    return request<{ ok: boolean; register: EngagementRegister }>(
      `/engagements/${encodeURIComponent(client)}/register${qs ? `?${qs}` : ""}`,
    );
  },
  // Render the register to a stable, hand-editable .xlsx in the
  // client's export folder. Returns the path (or a dated conflict
  // copy + warning if the workbook was open/locked).
  engagementExport: (client: string, project = "") => {
    const q = new URLSearchParams();
    if (project) q.set("project", project);
    const qs = q.toString();
    return request<{ ok: boolean; path: string; warning: string | null }>(
      `/engagements/${encodeURIComponent(client)}/export${qs ? `?${qs}` : ""}`,
      { method: "POST" },
    );
  },

  // Manual engagement overlay — status, exec sponsor, next milestone,
  // free-form notes. Layered on top of the auto-rolled register.
  putEngagementOverlay: (
    client: string,
    overlay: { project?: string; status?: string; exec_sponsor?: string;
               next_milestone?: string; notes?: string },
  ) =>
    request<{ ok: boolean; overlay: EngagementOverlay }>(
      `/engagements/${encodeURIComponent(client)}/overlay`, {
        method: "PUT",
        body: JSON.stringify(overlay),
      }),

  engagementKnownStatuses: () =>
    request<{ statuses: string[] }>("/engagements/known-statuses"),

  // Daily Briefing (Today view). User pastes their M365 Copilot
  // scheduled-prompt output; backend LLM-parses it and stores one
  // briefing per calendar date. Re-import preserves any action items
  // already checked off this morning.
  getTodayBriefing: () => request<DailyBriefing | {}>("/briefing/today"),
  getBriefingByDate: (date: string) =>
    request<DailyBriefing | {}>(`/briefing/${encodeURIComponent(date)}`),
  importBriefing: (text: string, date?: string) =>
    request<DailyBriefing>("/briefing/import", {
      method: "POST",
      body: JSON.stringify({ text, ...(date ? { date } : {}) }),
    }),
  setBriefingActionDone: (date: string, actionId: string, done: boolean) =>
    request<DailyBriefing>(
      `/briefing/${encodeURIComponent(date)}/actions/${encodeURIComponent(actionId)}`,
      { method: "PATCH", body: JSON.stringify({ done }) }),
  // Outlook Web sync — drives the user's installed Chrome (channel='chrome')
  // against the persistent profile at <recordings_dir>/web-session/ to
  // scrape today's calendar from outlook.office.com, then runs the
  // resulting text through the same LLM parser the manual import uses.
  //
  // signInToOutlookWeb POSTs and BLOCKS until the user closes the
  // headed Chrome window (up to ~10 minutes; the backend bails after
  // that deadline). Callers should show a "Sign-in window opened —
  // close it when done" hint and not race a second sign-in or sync
  // while this is in flight (backend returns 409 if another sync/
  // signin overlaps).
  //
  // syncBriefingFromOutlookWeb returns the stored DailyBriefing or
  // throws ApiError with status===423 when the session expired —
  // callers should catch that case and prompt sign-in.
  signInToOutlookWeb: () =>
    request<{ ok: true }>("/briefing/signin", { method: "POST" }),
  syncBriefingFromOutlookWeb: () =>
    request<DailyBriefing>("/briefing/sync", { method: "POST" }),

  // Domain terminology glossary — biases transcription toward the user's
  // jargon and corrects known mis-hears. Seeded server-side with a
  // curated SA / CCaaS / cloud / sales vocabulary.
  getTerminology: () => request<Terminology>("/terminology"),
  putTerminology: (t: Terminology) =>
    request<Terminology>("/terminology", {
      method: "PUT",
      body: JSON.stringify(t),
    }),
  resetTerminology: () =>
    request<Terminology>("/terminology/reset", { method: "POST" }),

  // Diagnostics — system health checks + a backend.log tail. Powers
  // Settings → Diagnostics so failures (Ollama down, dir not writable,
  // no mic) are visible without reading logs by hand.
  getDiagnostics: () => request<Diagnostics>("/diagnostics"),
  // Fires a 1-token chat completion against the configured AI provider
  // so the user can validate the key + base URL + model BEFORE the
  // next summarize/extract fails opaquely. Returns ok + latency on
  // success, or ok=false with a verbatim error string. ~10s timeout
  // server-side so a stuck endpoint can't hang the UI.
  testLLMConnection: (scope: "main" | "live" = "main") =>
    request<{
      ok: boolean;
      provider: string;
      model: string;
      scope?: "main" | "live";
      latency_ms: number;
      reply?: string;
      error?: string;
    }>("/diagnostics/llm-test", {
      method: "POST",
      body: JSON.stringify({ scope }),
    }),

  // Auto pre-meeting briefs — generated backend-side before meetings.
  getAutoPrepBriefs: () => request<AutoPrepBrief[]>("/prep-brief/auto"),
  getPendingAutoPrepBriefs: () =>
    request<AutoPrepBrief[]>("/prep-brief/auto/pending"),
  markAutoPrepBriefNotified: (key: string) =>
    request<{ ok: boolean }>(
      `/prep-brief/auto/${encodeURIComponent(key)}/notified`,
      { method: "POST" }),
};

export interface AutoPrepBrief {
  key: string;
  subject: string;
  start_iso: string;
  markdown: string;
  related_count?: number;
  minutes_before?: number;
  generated_at?: string;
  notified?: boolean;
}

export interface DiagnosticCheck {
  id: string;
  label: string;
  status: "ok" | "warn" | "error" | "info";
  detail: string;
}
export interface Diagnostics {
  checks: DiagnosticCheck[];
  log_tail: string;
}

export interface Terminology {
  // Canonical terms fed into Whisper's initial_prompt to bias decoding.
  terms: string[];
  // Known mis-hears → canonical replacement (applied post-transcription,
  // case-insensitive, word-boundary). Keys are lowercased server-side.
  corrections: Record<string, string>;
}

// --- Daily Briefing types ---
export interface BriefingTopPriority {
  title: string;
  detail: string;
  why: string;
}
export interface BriefingAction {
  id: string;
  title: string;
  detail: string;
  who: string;
  due: string;
  source: string;
  done_at: string | null;
}
export interface BriefingAgendaItem {
  id: string;
  title: string;
  time: string;
  duration: string;
  role: string; // host | attendee | optional | ""
  meeting_type: string; // discovery | sow | status | technical | demo | internal | general | ""
  client: string;
  attendees: string[];
  notes: string;
  status: "scheduled" | "cancelled" | "now" | "done";
}
export interface BriefingFyi {
  id: string;
  title: string;
  detail: string;
  category: string; // market | client | internal | personal | ""
}
export interface DailyBriefing {
  date: string;
  imported_at: string;
  source: string;
  greeting: string;
  top_priority: BriefingTopPriority | null;
  needs_response: BriefingAction[];
  agenda: BriefingAgendaItem[];
  schedule_notes: string[];
  fyi: BriefingFyi[];
  raw_text: string;
}

export interface EngagementOccurrence {
  session_id: string;
  display_name: string;
  at: string;
}
export interface EngagementRecord {
  id: string;
  status: string;
  source: string;
  occurrences: EngagementOccurrence[];
  // record-type-specific fields (text/title/decided/owner/…)
  [k: string]: unknown;
}
// Per-engagement manual overlay — fields the user pins by hand that
// can't be auto-rolled from meeting data. Merged into the register
// response by the backend.
export interface EngagementOverlay {
  status: string;
  exec_sponsor: string;
  next_milestone: string;
  notes: string;
  updated_at: string;
}

export interface EngagementRegister {
  client: string;
  project: string;
  generated_at: string;
  session_count: number;
  // ISO timestamps of the first and most recent meeting that fed this
  // register. Empty string when no sessions exist yet.
  first_meeting_at: string;
  last_meeting_at: string;
  counts: {
    open_requirements: number;
    decisions: number;
    open_action_items: number;
    open_questions: number;
    outstanding_commitments: number;
    total_commitments: number;
  };
  commitments: {
    outstanding: number;
    delivered: number;
    dismissed: number;
    total: number;
  };
  requirements: EngagementRecord[];
  decisions: EngagementRecord[];
  action_items: EngagementRecord[];
  open_questions: EngagementRecord[];
  // Manual overlay merged in by the backend. Always present (with
  // empty fields when nothing has been set yet) so the UI doesn't
  // have to null-check.
  overlay: EngagementOverlay;
}

export interface InsightsRow {
  label: string;
  seconds: number;
}
export interface InsightsTopic {
  phrase: string;
  count: number;
}
export interface StaleCommitment {
  commitment_id: string;
  session_id: string;
  owner: string;
  side: string;
  description: string;
  due_date_iso: string;
  is_overdue: boolean;
  session_display_name: string;
  session_client: string;
  session_started_at: string;
}
export interface OpenDecision {
  session_id: string;
  session_display_name: string;
  session_client: string;
  session_started_at: string;
  title: string;
  decided: string;
  item_hash: string;
}
export interface OpenActionItem {
  session_id: string;
  session_display_name: string;
  session_client: string;
  session_started_at: string;
  owner: string;
  description: string;
  due: string;
  item_hash: string;
}
export interface InsightsSummary {
  window: {
    since: string | null;
    until: string | null;
    client: string | null;
    session_count: number;
    total_seconds: number;
  };
  time_allocation: {
    by_client: InsightsRow[];
    by_template: InsightsRow[];
    by_project: InsightsRow[];
  };
  topics: Record<string, InsightsTopic[]>;
  open_loops: {
    stale_commitments: StaleCommitment[];
    un_implemented_decisions: OpenDecision[];
    unchecked_action_items: OpenActionItem[];
    counts: {
      stale_commitments: number;
      un_implemented_decisions: number;
      unchecked_action_items: number;
    };
  };
}

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
