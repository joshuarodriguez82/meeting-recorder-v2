// Durable local state: the SAF folder grant, user defaults, and the
// pending-sync queue. The queue is the reliability backbone — a finished
// recording lands here first and only leaves once its .m4a + .json are
// confirmed written to the synced folder, so a failed SAF write, a full
// disk, a revoked grant, or the app being killed mid-write all recover
// by retrying instead of losing the audio.

const K_FOLDER = "mr.folder";
const K_DEFAULTS = "mr.defaults";
const K_QUEUE = "mr.queue";

export interface FolderGrant {
  treeUri: string;
  label: string;
}

export interface Defaults {
  client: string;
  project: string;
  autoRecord: boolean;
}

export interface PendingItem {
  sessionId: string;
  baseName: string; // "session_AB12CD34"
  audioPath: string; // cache-dir .m4a, kept until write confirmed
  json: string;
  displayName: string;
  createdAt: number;
  attempts: number;
  lastError: string | null;
}

function read<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : fallback;
  } catch {
    return fallback;
  }
}
function write(key: string, value: unknown): void {
  localStorage.setItem(key, JSON.stringify(value));
}

export const store = {
  getFolder: (): FolderGrant | null => read<FolderGrant | null>(K_FOLDER, null),
  setFolder: (f: FolderGrant | null) => write(K_FOLDER, f),

  getDefaults: (): Defaults =>
    read<Defaults>(K_DEFAULTS, { client: "", project: "", autoRecord: false }),
  setDefaults: (d: Defaults) => write(K_DEFAULTS, d),

  getQueue: (): PendingItem[] => read<PendingItem[]>(K_QUEUE, []),
  setQueue: (q: PendingItem[]) => write(K_QUEUE, q),
  enqueue(item: PendingItem) {
    const q = store.getQueue();
    q.push(item);
    store.setQueue(q);
  },
  updateItem(sessionId: string, patch: Partial<PendingItem>) {
    store.setQueue(
      store.getQueue().map((i) =>
        i.sessionId === sessionId ? { ...i, ...patch } : i,
      ),
    );
  },
  dequeue(sessionId: string) {
    store.setQueue(store.getQueue().filter((i) => i.sessionId !== sessionId));
  },
};
