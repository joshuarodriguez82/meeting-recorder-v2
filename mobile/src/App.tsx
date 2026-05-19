import { useCallback, useEffect, useRef, useState } from "react";
import { Recorder, isNative, type PermissionState } from "./native/recorder";
import {
  store,
  type Defaults,
  type FolderGrant,
  type PendingItem,
} from "./lib/store";
import { buildSessionJson, newSessionId } from "./lib/session";
import {
  describePending,
  drainQueue,
  listSynced,
  loadClientsProjects,
  type SyncedRow,
} from "./lib/sync";

type Tab = "record" | "recordings" | "settings";

function fmt(ms: number): string {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const p = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(ss)}` : `${p(m)}:${p(ss)}`;
}

export function App() {
  const [tab, setTab] = useState<Tab>("record");
  const [folder, setFolder] = useState<FolderGrant | null>(store.getFolder());
  const [defaults, setDefaults] = useState<Defaults>(store.getDefaults());
  const [perm, setPerm] = useState<PermissionState | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  // Capture strategy. "auto" runs the call-audio ladder (tries to grab
  // the real call stream first); "mic" forces plain mic/speakerphone.
  // Persisted so the user's choice sticks.
  const [captureMode, setCaptureMode] = useState<"auto" | "mic">(
    () => (localStorage.getItem("captureMode") as "auto" | "mic") || "auto",
  );
  const pickCaptureMode = (m: "auto" | "mic") => {
    localStorage.setItem("captureMode", m);
    setCaptureMode(m);
  };
  // Which AudioSource the device actually allowed this recording. This
  // is the ground truth — if it says VOICE_CALL, your Pixel let us tap
  // the call directly; anything else means acoustic capture.
  const [audioSource, setAudioSource] = useState<string | null>(null);
  // Accessibility-service status — the non-root path to capturing the
  // far side of a call on a stock Pixel (same mechanism Talker ACR
  // uses). null until first probed.
  const [acc, setAcc] = useState<{
    enabled: boolean;
    autoRecordCalls: boolean;
  } | null>(null);
  const recStart = useRef(0);
  const sessionIdRef = useRef<string>("");
  const timer = useRef<number | null>(null);

  const [name, setName] = useState("");
  const [client, setClient] = useState(defaults.client);
  const [project, setProject] = useState(defaults.project);
  const [notes, setNotes] = useState("");

  const [pendingDesc, setPendingDesc] = useState(describePending(store.getQueue()));
  const [synced, setSynced] = useState<SyncedRow[]>([]);
  const [clients, setClients] = useState<string[]>([]);
  const [projByClient, setProjByClient] = useState<Record<string, string[]>>({});

  const flash = useCallback((m: string) => {
    setToast(m);
    window.setTimeout(() => setToast(null), 4200);
  }, []);

  // --- permissions + restore live recording after backgrounding ------
  useEffect(() => {
    Recorder.checkPermissions().then(setPerm).catch(() => {});
    Recorder.getStatus()
      .then((st) => {
        if (st.recording) {
          recStart.current = Date.now() - st.elapsedMs;
          setRecording(true);
          setElapsed(st.elapsedMs);
          setAudioSource(st.audioSource);
        }
      })
      .catch(() => {});
  }, []);

  // Accessibility status + adopt any auto-recorded calls. Auto calls
  // are captured by the AccessibilityService while the WebView is dead,
  // so on every resume we sweep the cache for orphan recordings, give
  // them a session JSON, and run them through the normal sync queue.
  const adoptPending = useCallback(async () => {
    try {
      const st = await Recorder.accessibilityStatus();
      setAcc(st);
    } catch { /* not native / no plugin */ }
    try {
      const { captures } = await Recorder.pendingCaptures();
      if (!captures.length) return;
      for (const c of captures) {
        const now = new Date();
        const baseName = `session_${c.sessionId}`;
        const json = buildSessionJson({
          sessionId: c.sessionId,
          displayName: `Call ${now.toLocaleString()}`,
          startedAt: now,
          endedAt: now,
          client: defaults.client,
          project: defaults.project,
          notes: "Auto-recorded call (Meeting Recorder).",
        });
        store.enqueue({
          sessionId: c.sessionId,
          baseName,
          audioPath: c.path,
          json,
          displayName: `Call ${now.toLocaleString()}`,
          createdAt: Date.now(),
          attempts: 0,
          lastError: null,
        });
      }
      setPendingDesc(describePending(store.getQueue()));
      flash(`Found ${captures.length} auto-recorded call(s) — syncing…`);
      await sync(false);
      await refreshSynced();
    } catch { /* nothing to adopt */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaults.client, defaults.project, flash]);

  useEffect(() => {
    adoptPending();
    const onVis = () => {
      if (document.visibilityState === "visible") adoptPending();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [adoptPending]);

  useEffect(() => {
    if (!recording) {
      if (timer.current) window.clearInterval(timer.current);
      return;
    }
    timer.current = window.setInterval(
      () => setElapsed(Date.now() - recStart.current),
      500,
    );
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [recording]);

  const refreshSynced = useCallback(async () => {
    setPendingDesc(describePending(store.getQueue()));
    if (!folder) return;
    try {
      setSynced(await listSynced(folder.treeUri));
      const cp = await loadClientsProjects(folder.treeUri);
      setClients(cp.clients);
      setProjByClient(cp.projectsByClient);
    } catch {
      /* folder may be syncing; non-fatal */
    }
  }, [folder]);

  const sync = useCallback(
    async (announce = false) => {
      if (!folder) return;
      const r = await drainQueue(folder.treeUri);
      setPendingDesc(describePending(store.getQueue()));
      if (announce) {
        if (r.failed.length) flash(`Sync: ${r.failed[0].error}`);
        else if (r.synced.length) flash(`Synced ${r.synced.length} recording(s)`);
        else flash("Nothing to sync");
      }
      await refreshSynced();
    },
    [folder, flash, refreshSynced],
  );

  // Drain on launch, when returning to the foreground, and every 60s
  // while open — covers "app was killed mid-write" and "grant came back".
  useEffect(() => {
    void sync(false);
    void refreshSynced();
    const onVis = () => {
      if (document.visibilityState === "visible") void sync(false);
    };
    document.addEventListener("visibilitychange", onVis);
    const iv = window.setInterval(() => void sync(false), 60000);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      window.clearInterval(iv);
    };
  }, [sync, refreshSynced]);

  // --- recording -----------------------------------------------------
  async function start() {
    if (!folder) {
      flash("Pick your OneDrive folder in Settings first");
      setTab("settings");
      return;
    }
    let p = perm ?? (await Recorder.requestPermissions());
    if (p.microphone !== "granted") {
      p = await Recorder.requestPermissions();
      setPerm(p);
    }
    if (p.microphone !== "granted") {
      flash("Microphone permission is required to record");
      return;
    }
    const sessionId = newSessionId();
    try {
      await Recorder.startRecording({ sessionId, captureMode });
      sessionIdRef.current = sessionId;
      recStart.current = Date.now();
      setElapsed(0);
      setRecording(true);
      // Surface what the device actually permitted.
      Recorder.getStatus()
        .then((s) => {
          setAudioSource(s.audioSource);
          if (s.audioSource === "VOICE_CALL") {
            flash("Capturing the call directly (both sides) — VOICE_CALL");
          } else if (captureMode === "auto" && s.audioSource) {
            flash(
              `Call tap blocked by this device — capturing via ${s.audioSource}. ` +
                "Put the call on speakerphone to get the other side.",
            );
          }
        })
        .catch(() => {});
    } catch (e) {
      flash(`Could not start: ${e instanceof Error ? e.message : e}`);
    }
  }

  async function stop() {
    try {
      const startedAt = new Date(recStart.current);
      const res = await Recorder.stopRecording();
      setRecording(false);
      const endedAt = new Date();
      // Same id from start → the JSON's session_id, the written
      // session_<id>.m4a, and session_<id>.json all agree, which is how
      // the desktop pairs audio to its session record.
      const sessionId = sessionIdRef.current || newSessionId();
      sessionIdRef.current = "";
      const baseName = `session_${sessionId}`;
      const json = buildSessionJson({
        sessionId,
        displayName: name,
        startedAt,
        endedAt,
        client,
        project,
        notes,
      });
      store.enqueue({
        sessionId,
        baseName,
        audioPath: res.path,
        json,
        displayName: name || baseName,
        createdAt: Date.now(),
        attempts: 0,
        lastError: null,
      });
      setName("");
      setNotes("");
      setAudioSource(null);
      flash(
        `Saved ${fmt(res.durationMs)} (via ${res.audioSource ?? "?"}) — ` +
          "syncing to OneDrive…",
      );
      await sync(false);
      await refreshSynced();
    } catch (e) {
      flash(`Stop failed: ${e instanceof Error ? e.message : e}`);
      setRecording(false);
    }
  }

  async function pickFolder() {
    try {
      const g = await Recorder.pickFolder();
      store.setFolder(g);
      setFolder(g);
      flash(`Folder set: ${g.label}`);
      await refreshSynced();
    } catch {
      flash("Folder selection cancelled");
    }
  }

  // Hand a queued recording to the OneDrive app via the OS share sheet.
  // The .m4a stays in cache (shareSession doesn't delete it) so a
  // cancelled share can be retried; the user clears the item with
  // "Sent — remove" once OneDrive confirms the upload.
  async function sendToOneDrive(q: PendingItem) {
    try {
      await Recorder.shareSession({
        audioPath: q.audioPath,
        json: q.json,
        baseName: q.baseName,
      });
      flash("In OneDrive, pick the MeetingRecorder folder the desktop watches.");
    } catch (e) {
      flash(`Send failed: ${e instanceof Error ? e.message : e}`);
    }
  }

  function removeFromQueue(sessionId: string) {
    store.dequeue(sessionId);
    setPendingDesc(describePending(store.getQueue()));
    void refreshSynced();
  }

  function saveDefaults(patch: Partial<Defaults>) {
    const d = { ...defaults, ...patch };
    setDefaults(d);
    store.setDefaults(d);
  }

  const projOptions = projByClient[client] ?? [];

  return (
    <div className="app">
      <div className="body">
        {tab === "record" && (
          <>
            <h1>Record</h1>
            {!folder && (
              <div className="card">
                <p className="hint warn">
                  No sync folder set. Recordings can't reach your PC until
                  you point this at your OneDrive Meeting Recorder folder.
                </p>
                <button className="ghost" onClick={() => setTab("settings")}>
                  Go to Settings
                </button>
              </div>
            )}
            <div className="card">
              <div className="timer">{fmt(elapsed)}</div>
              <button
                className={`record-btn ${recording ? "live" : ""}`}
                onClick={recording ? stop : start}
              >
                {recording ? "Stop & Save" : "Start Recording"}
              </button>
              {recording && audioSource && (
                <p
                  className={`pill ${audioSource === "VOICE_CALL" ? "ok" : "warn"}`}
                  style={{ marginTop: 12 }}
                >
                  {audioSource === "VOICE_CALL"
                    ? "Capturing the call directly — both sides, even off speaker"
                    : `Capturing via ${audioSource} — far side only on speakerphone`}
                </p>
              )}
              <p className="hint" style={{ marginTop: 12 }}>
                Capture mode tries to tap the call audio directly first
                (VOICE_CALL). Most stock Pixels block that for a sideloaded
                app, so it falls back to the mic — put the call on{" "}
                <strong>speaker</strong> and the phone captures both sides.
                The pill above shows exactly what your device allowed.
              </p>
            </div>

            <h2>Tag this recording</h2>
            <div className="card">
              <label>Name</label>
              <input
                value={name}
                placeholder="e.g. Acme discovery call"
                onChange={(e) => setName(e.target.value)}
              />
              <label>Client</label>
              <input
                list="clients"
                value={client}
                placeholder="Optional"
                onChange={(e) => setClient(e.target.value)}
              />
              <datalist id="clients">
                {clients.map((c) => (
                  <option key={c} value={c} />
                ))}
              </datalist>
              <label>Project</label>
              <input
                list="projects"
                value={project}
                placeholder="Optional"
                onChange={(e) => setProject(e.target.value)}
              />
              <datalist id="projects">
                {projOptions.map((p) => (
                  <option key={p} value={p} />
                ))}
              </datalist>
              <label>Notes (fed into the AI summary on the PC)</label>
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>
          </>
        )}

        {tab === "recordings" && (
          <>
            <h1>Recordings</h1>
            <div className="card">
              <div className="row">
                <span className="hint">{pendingDesc}</span>
                <button className="ghost" onClick={() => void sync(true)}>
                  Sync now
                </button>
              </div>
            </div>
            {store.getQueue().map((q) => (
              <div className="card" key={q.sessionId}>
                <div className="list-item">
                  <div className="title">{q.displayName}</div>
                  <div className="meta">
                    {q.lastError ? (
                      <span className="pill warn">
                        retrying — {q.lastError}
                      </span>
                    ) : (
                      <span className="pill warn">not on PC yet</span>
                    )}
                  </div>
                </div>
                <div className="row" style={{ marginTop: 10, gap: 8 }}>
                  <button
                    className="ghost"
                    onClick={() => void sendToOneDrive(q)}
                  >
                    Send to OneDrive
                  </button>
                  <button
                    className="ghost"
                    onClick={() => removeFromQueue(q.sessionId)}
                  >
                    Sent — remove
                  </button>
                </div>
              </div>
            ))}
            <div className="card">
              {synced.length === 0 && (
                <p className="hint">
                  Nothing in the synced folder yet. Synced recordings — and
                  whether the PC has processed them — show up here.
                </p>
              )}
              {synced.map((s) => (
                <div className="list-item" key={s.sessionId}>
                  <div className="title">{s.displayName}</div>
                  <div className="meta">
                    {s.startedAt?.replace("T", " ")}{" "}
                    {s.client ? `· ${s.client}` : ""}{" "}
                    {s.processed ? (
                      <span className="pill ok">processed on PC</span>
                    ) : (
                      <span className="pill">awaiting PC</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}

        {tab === "settings" && (
          <>
            <h1>Settings</h1>

            <h2>Sync folder</h2>
            <div className="card">
              <p className="hint">
                Android's OneDrive app exposes no folder this picker can
                write into, so the reliable path is the{" "}
                <strong>Send to OneDrive</strong> button on each recording
                in the Recordings tab — it hands the
                <code> session_&lt;id&gt;.m4a</code> + JSON to your OneDrive
                app, which uploads them to the same folder the desktop
                watches. Picking a folder here is optional: it only auto-
                writes if you select a folder a sync app actually mirrors
                to OneDrive (e.g. an Autosync target).
              </p>
              <p className="hint" style={{ marginTop: 8 }}>
                Status:{" "}
                {folder ? (
                  <span className="pill ok">{folder.label}</span>
                ) : (
                  <span className="pill warn">not set</span>
                )}
              </p>
              <button
                className="ghost"
                style={{ marginTop: 12 }}
                onClick={pickFolder}
              >
                {folder ? "Change folder" : "Pick OneDrive folder"}
              </button>
            </div>

            <h2>Defaults</h2>
            <div className="card">
              <label>Default client</label>
              <input
                value={defaults.client}
                onChange={(e) => {
                  saveDefaults({ client: e.target.value });
                  setClient(e.target.value);
                }}
              />
              <label>Default project</label>
              <input
                value={defaults.project}
                onChange={(e) => {
                  saveDefaults({ project: e.target.value });
                  setProject(e.target.value);
                }}
              />
              <div className="toggle" style={{ marginTop: 16 }}>
                <span>
                  Auto-record from phone calendar
                  <br />
                  <span className="hint">
                    Calendar-driven auto-start lands in the next build —
                    the desktop already auto-records.
                  </span>
                </span>
                <input
                  type="checkbox"
                  style={{ width: 22 }}
                  disabled
                  checked={false}
                  readOnly
                />
              </div>
            </div>

            <h2>Permissions</h2>
            <div className="card">
              <p className="hint">
                Microphone:{" "}
                <span
                  className={`pill ${
                    perm?.microphone === "granted" ? "ok" : "warn"
                  }`}
                >
                  {perm?.microphone ?? "unknown"}
                </span>
              </p>
              <p className="hint" style={{ marginTop: 8 }}>
                Notifications:{" "}
                <span
                  className={`pill ${
                    perm?.notifications === "granted" ? "ok" : "warn"
                  }`}
                >
                  {perm?.notifications ?? "unknown"}
                </span>{" "}
                — required so Android keeps the recording alive with the
                screen off.
              </p>
              <button
                className="ghost"
                style={{ marginTop: 12 }}
                onClick={async () =>
                  setPerm(await Recorder.requestPermissions())
                }
              >
                Request permissions
              </button>
            </div>

            <h2>Capture mode</h2>
            <div className="card">
              <label>
                <input
                  type="radio"
                  name="capmode"
                  checked={captureMode === "auto"}
                  onChange={() => pickCaptureMode("auto")}
                />{" "}
                <strong>Try to capture the call (recommended)</strong>
              </label>
              <p className="hint" style={{ margin: "4px 0 10px 20px" }}>
                Attempts the real call stream first (VOICE_CALL → recognition
                → unprocessed), then falls back to mic. On a rooted phone or a
                device that allows it, this records both sides even off
                speaker. On a stock Pixel the call tap is blocked and it uses
                the mic — use speakerphone for the far side.
              </p>
              <label>
                <input
                  type="radio"
                  name="capmode"
                  checked={captureMode === "mic"}
                  onChange={() => pickCaptureMode("mic")}
                />{" "}
                <strong>Mic only (speakerphone)</strong>
              </label>
              <p className="hint" style={{ margin: "4px 0 0 20px" }}>
                Skip the call-tap attempts entirely. Reliable on any device;
                far side requires speakerphone.
              </p>
            </div>

            <h2>Automatic call recording (beta)</h2>
            <div className="card">
              <p className="hint" style={{ marginTop: 0 }}>
                Capturing the <strong>other person</strong> on a regular
                cellular call (off speaker) needs an Accessibility service —
                the same mechanism Talker/Cube ACR use. Enable it and calls
                record automatically, hands-free.
              </p>
              <p className="hint">
                Accessibility service:{" "}
                <span className={`pill ${acc?.enabled ? "ok" : "warn"}`}>
                  {acc == null ? "checking…" : acc.enabled ? "on" : "off"}
                </span>
              </p>
              <button
                className="ghost"
                onClick={async () => {
                  try {
                    await Recorder.openAccessibilitySettings();
                    flash(
                      "Turn ON 'Meeting Recorder call capture'. On Android 13+ " +
                        "also tap ⋮ → Allow restricted settings first.",
                    );
                  } catch (e) {
                    flash(`Couldn't open settings: ${e instanceof Error ? e.message : e}`);
                  }
                }}
              >
                Open Accessibility settings
              </button>
              <p className="hint" style={{ marginTop: 10 }}>
                <strong>Android 13+:</strong> if the toggle is greyed out, open
                this app's info → ⋮ menu → <strong>Allow restricted
                settings</strong>, then enable it.
              </p>
              <p className="hint" style={{ marginTop: 10 }}>
                Phone permission:{" "}
                <span className={`pill ${perm?.phone === "granted" ? "ok" : "warn"}`}>
                  {perm?.phone ?? "unknown"}
                </span>{" "}
                — lets it auto start/stop on a call.
              </p>
              {acc && (
                <label style={{ display: "block", marginTop: 10 }}>
                  <input
                    type="checkbox"
                    checked={acc.autoRecordCalls}
                    onChange={async (e) => {
                      const on = e.target.checked;
                      await Recorder.setAutoRecordCalls({ enabled: on });
                      setAcc({ ...acc, autoRecordCalls: on });
                    }}
                  />{" "}
                  Auto-record every phone call
                </label>
              )}
              <p className="hint" style={{ marginTop: 10 }}>
                After a call, the recording syncs to OneDrive automatically
                next time this app is open. The recording screen&apos;s pill
                shows whether your device gave us the real call stream
                (VOICE_CALL) or fell back to mic.
              </p>
            </div>

            <h2>About</h2>
            <div className="card">
              <p className="hint">
                Meeting Recorder mobile companion ·{" "}
                {isNative ? "Android" : "browser preview"}. Records on the
                phone and drops the audio in your OneDrive folder for the
                desktop to process. Direct call-audio capture depends on your
                device/OS — the app tries it and shows you what it got.
              </p>
            </div>
          </>
        )}
      </div>

      {toast && <div className="toast">{toast}</div>}

      <div className="tabbar">
        {(["record", "recordings", "settings"] as Tab[]).map((t) => (
          <button
            key={t}
            className={tab === t ? "active" : ""}
            onClick={() => setTab(t)}
          >
            {t === "record" ? "● Record" : t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
    </div>
  );
}
