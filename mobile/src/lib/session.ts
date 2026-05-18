// Builds the exact JSON shape the desktop expects. Mirrors
// backend/models/session.py Session.to_dict(); the desktop's from_dict()
// + SessionService.list_sessions() read these keys, so drift here means
// the phone's recordings silently won't appear on the PC.

export interface DesktopSession {
  session_id: string;
  display_name: string;
  started_at: string;
  ended_at: string;
  audio_path: string;
  speakers: Record<string, unknown>;
  segments: unknown[];
  summary: string | null;
  action_items: string | null;
  requirements: string | null;
  template: string;
  client: string;
  project: string;
  attendees: string[];
  decisions: string | null;
  notes: string;
  exported_audio_paths: string[];
}

// 8 uppercase hex chars — matches the desktop's uuid4().hex[:8].upper().
// Critically dotless: SessionService.list_sessions() treats any dot in
// the file stem as a sidecar marker and skips it.
export function newSessionId(): string {
  const b = new Uint8Array(4);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0"))
    .join("")
    .toUpperCase();
}

// The desktop parses timestamps with datetime.fromisoformat and treats
// them as naive local time (see the Insights tz-normalization fixes).
// Emit local wall-clock with NO timezone offset so a recording made at
// 09:30 reads as 09:30 on the PC regardless of either device's tz.
function isoLocalNaive(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

export interface SessionMeta {
  sessionId: string;
  displayName: string;
  startedAt: Date;
  endedAt: Date;
  client: string;
  project: string;
  notes: string;
}

export function buildSessionJson(m: SessionMeta): string {
  const s: DesktopSession = {
    session_id: m.sessionId,
    display_name: m.displayName.trim() || `Mobile session ${m.sessionId}`,
    started_at: isoLocalNaive(m.startedAt),
    ended_at: isoLocalNaive(m.endedAt),
    // Informational only. This phone path never resolves on the PC; the
    // desktop's resolve_audio_path() falls back to the session_<id>.m4a
    // sitting next to this JSON in the synced folder.
    audio_path: `session_${m.sessionId}.m4a`,
    speakers: {},
    segments: [],
    summary: null,
    action_items: null,
    requirements: null,
    template: "General",
    client: m.client.trim(),
    project: m.project.trim(),
    attendees: [],
    decisions: null,
    notes: m.notes.trim(),
    exported_audio_paths: [],
  };
  return JSON.stringify(s, null, 2);
}
