import { Recorder } from "../native/recorder";
import { store, type PendingItem } from "./store";

// Drains the pending queue into the synced folder. Idempotent and
// safe to call repeatedly (on app resume, after a recording, on a
// timer). The native writeSession() overwrites any same-named file, so
// a retry after a partial write self-heals rather than producing
// "session_X (1).m4a" duplicates.

const MAX_ATTEMPTS = 8;

export interface SyncReport {
  synced: string[];
  failed: { sessionId: string; error: string }[];
  remaining: number;
}

export async function drainQueue(treeUri: string): Promise<SyncReport> {
  const report: SyncReport = { synced: [], failed: [], remaining: 0 };

  // Confirm we still hold the grant before hammering a dead URI.
  try {
    const { ok } = await Recorder.hasFolderAccess({ treeUri });
    if (!ok) {
      const q = store.getQueue();
      report.remaining = q.length;
      report.failed = q.map((i) => ({
        sessionId: i.sessionId,
        error: "folder access lost — re-pick the OneDrive folder in Settings",
      }));
      return report;
    }
  } catch {
    // hasFolderAccess unsupported on web fallback — proceed.
  }

  for (const item of [...store.getQueue()]) {
    if (item.attempts >= MAX_ATTEMPTS) {
      report.failed.push({
        sessionId: item.sessionId,
        error: item.lastError || "gave up after repeated failures",
      });
      continue;
    }
    try {
      await Recorder.writeSession({
        treeUri,
        baseName: item.baseName,
        json: item.json,
        audioPath: item.audioPath,
      });
      store.dequeue(item.sessionId);
      report.synced.push(item.sessionId);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      store.updateItem(item.sessionId, {
        attempts: item.attempts + 1,
        lastError: msg,
      });
      report.failed.push({ sessionId: item.sessionId, error: msg });
    }
  }
  report.remaining = store.getQueue().length;
  return report;
}

// --- Reading the synced folder back ----------------------------------

export interface SyncedRow {
  sessionId: string;
  displayName: string;
  startedAt: string;
  client: string;
  project: string;
  processed: boolean; // desktop has run transcription/summary
}

export async function listSynced(treeUri: string): Promise<SyncedRow[]> {
  const { sessions } = await Recorder.listSyncedSessions({ treeUri });
  const rows: SyncedRow[] = [];
  for (const s of sessions) {
    try {
      const d = JSON.parse(s.json);
      rows.push({
        sessionId: d.session_id ?? s.name,
        displayName: d.display_name ?? s.name,
        startedAt: d.started_at ?? "",
        client: d.client ?? "",
        project: d.project ?? "",
        processed: Boolean(
          (d.segments && d.segments.length) || d.summary,
        ),
      });
    } catch {
      // Skip a half-synced/corrupt JSON rather than break the list.
    }
  }
  rows.sort((a, b) => (b.startedAt || "").localeCompare(a.startedAt || ""));
  return rows;
}

// client_configs.json is keyed on normalized client name with
// { export_folder, display_name }. Known projects aren't in that file —
// derive them from the (client, project) pairs already on synced
// sessions, exactly how the desktop surfaces them.
export async function loadClientsProjects(
  treeUri: string,
): Promise<{ clients: string[]; projectsByClient: Record<string, string[]> }> {
  const clients = new Set<string>();
  const projectsByClient: Record<string, Set<string>> = {};

  try {
    const { content } = await Recorder.readTextFile({
      treeUri,
      name: "client_configs.json",
    });
    if (content) {
      const cfg = JSON.parse(content) as Record<
        string,
        { display_name?: string }
      >;
      for (const v of Object.values(cfg)) {
        if (v?.display_name) clients.add(v.display_name);
      }
    }
  } catch {
    // No client_configs.json yet — first run before any desktop client.
  }

  try {
    for (const row of await listSynced(treeUri)) {
      if (row.client) {
        clients.add(row.client);
        if (row.project) {
          (projectsByClient[row.client] ??= new Set()).add(row.project);
        }
      }
    }
  } catch {
    // ignore — picker still works as free text
  }

  return {
    clients: [...clients].sort((a, b) => a.localeCompare(b)),
    projectsByClient: Object.fromEntries(
      Object.entries(projectsByClient).map(([k, v]) => [k, [...v].sort()]),
    ),
  };
}

export function describePending(q: PendingItem[]): string {
  if (q.length === 0) return "All recordings synced";
  const errd = q.filter((i) => i.lastError).length;
  return errd
    ? `${q.length} waiting to sync (${errd} retrying)`
    : `${q.length} waiting to sync`;
}
