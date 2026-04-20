"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Zap, Cpu, Loader2, RefreshCw } from "lucide-react";

type GpuStatus = Awaited<ReturnType<typeof api.getGpuStatus>>;

// DirectML intentionally omitted: torch-directml only publishes wheels for
// Python 3.10 and the bundled runtime is 3.13. Re-add once Microsoft ships
// a compatible wheel — see https://pypi.org/project/torch-directml/#files
const BACKENDS: {
  id: "cpu" | "cuda";
  title: string;
  subtitle: string;
  bytes: string;
  when: string;
}[] = [
  {
    id: "cpu",
    title: "CPU (default, bundled)",
    subtitle: "Runs on any machine. Transcription takes ~5-10% of meeting duration.",
    bytes: "0 MB — already installed",
    when: "Use if you don't have an NVIDIA GPU.",
  },
  {
    id: "cuda",
    title: "NVIDIA (CUDA)",
    subtitle: "10× faster transcription on NVIDIA GTX / RTX / Quadro GPUs.",
    bytes: "~2.2 GB download",
    when: "Recommended if your machine has an NVIDIA GPU with CUDA 12 support.",
  },
];

export function GpuAccelerationCard() {
  const [status, setStatus] = useState<GpuStatus | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);
  // Once an install finishes, the Python process in memory still has the
  // OLD torch imported — `current` keeps reporting the pre-install backend.
  // Remember the target of the last successful install so we can show a
  // "Restart required to activate X" state instead of letting the user
  // click "Use This" again and trigger the install over and over.
  const [pendingRestartTarget, setPendingRestartTarget] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastAttemptRef = useRef<string | null>(null);

  const load = async () => {
    try {
      const s = await api.getGpuStatus();
      setStatus(s);
      // If there's a task running, keep polling so the user sees progress.
      if (s.task.running) {
        setInstalling(s.task.phase === "installing" ? "running" : null);
      } else {
        // Task finished. If it completed successfully and current !=
        // what we asked for, we're in "restart required" land.
        if (
          s.task.phase === "complete" &&
          lastAttemptRef.current &&
          lastAttemptRef.current !== s.current
        ) {
          setPendingRestartTarget(lastAttemptRef.current);
        }
        setInstalling(null);
      }
    } catch (e) {
      console.error("GPU status fetch failed:", e);
    }
  };

  useEffect(() => {
    load();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  useEffect(() => {
    if (installing) {
      pollRef.current = setInterval(load, 2000);
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    };
  }, [installing]);

  const install = async (backend: "cpu" | "cuda" | "directml") => {
    if (status?.current === backend) {
      toast.info(`${labelFor(backend)} is already active`);
      return;
    }
    if (pendingRestartTarget) {
      toast.info("An install is already complete — restart the backend to activate it.");
      return;
    }
    setInstalling(backend);
    lastAttemptRef.current = backend;
    try {
      await api.installGpuBackend(backend);
      toast.info(`Installing ${labelFor(backend)}. This can take a few minutes.`);
    } catch (e) {
      setInstalling(null);
      lastAttemptRef.current = null;
      toast.error(`Install request failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const restartBackend = async () => {
    setRestarting(true);
    try {
      // Tauri command wired in lib.rs — kills the Python child, clears
      // the port, spawns fresh. Python re-imports torch, picks up the
      // newly installed flavour.
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("restart_backend");
      // Poll /health until the new backend is serving.
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        try {
          await api.health();
          break;
        } catch { /* not up yet */ }
      }
      setPendingRestartTarget(null);
      lastAttemptRef.current = null;
      await load();
      toast.success("Backend restarted — new GPU runtime is active.");
    } catch (e) {
      toast.error(`Restart failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setRestarting(false);
    }
  };

  if (!status) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">GPU Acceleration</CardTitle></CardHeader>
        <CardContent><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></CardContent>
      </Card>
    );
  }

  const detected = status.detected;
  const task = status.task;
  const current = status.current;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Zap className="h-4 w-4 text-primary" />
          Transcription Acceleration
          <Badge variant="outline" className="ml-2 text-[10px] uppercase">
            Active: {labelFor(current)}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Detection summary */}
        <div className="rounded-md border bg-muted/40 p-3 text-xs space-y-1">
          <div className="font-medium text-foreground">Detected on this machine:</div>
          {detected.gpus.length === 0 ? (
            <div className="text-muted-foreground">No GPUs detected (CPU only).</div>
          ) : (
            <ul className="space-y-0.5 text-muted-foreground">
              {detected.gpus.map((g, i) => <li key={i}>• {g}</li>)}
            </ul>
          )}
          <div className="pt-1 text-muted-foreground">
            Recommended backend:{" "}
            <span className="font-medium text-foreground">{labelFor(detected.recommended)}</span>
          </div>
        </div>

        {/* Restart banner — shown when an install succeeded but the
            running Python process still has the old torch imported. */}
        {pendingRestartTarget && (
          <div className="rounded-lg border-2 border-primary bg-primary/10 p-3 space-y-2">
            <div className="font-medium text-sm">
              Install complete: {labelFor(pendingRestartTarget)}
            </div>
            <p className="text-xs text-muted-foreground">
              The new runtime is on disk but the backend is still running the
              old one. Click below to restart the backend and activate
              <strong> {labelFor(pendingRestartTarget)}</strong>.
            </p>
            <Button size="sm" onClick={restartBackend} disabled={restarting}>
              {restarting
                ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" />
                : <RefreshCw className="h-3.5 w-3.5 mr-2" />}
              {restarting ? "Restarting…" : "Restart backend now"}
            </Button>
          </div>
        )}

        {/* Backend cards */}
        <div className="space-y-2">
          {BACKENDS.map((b) => {
            const active = current === b.id;
            const recommended = detected.recommended === b.id;
            const thisInstalling = task.running && installing === b.id;
            const isPendingTarget = pendingRestartTarget === b.id;
            const anyInstallBlocked =
              active || task.running || Boolean(pendingRestartTarget);
            return (
              <div
                key={b.id}
                className={`rounded-lg border p-3 ${active ? "border-primary bg-primary/5" : ""}`}
              >
                <div className="flex items-start gap-3">
                  <div className="mt-1">
                    {b.id === "cpu" ? <Cpu className="h-4 w-4 text-primary" />
                      : <Zap className="h-4 w-4 text-primary" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-sm">{b.title}</span>
                      {active && <Badge variant="default" className="text-[10px]">Active</Badge>}
                      {isPendingTarget && (
                        <Badge variant="outline" className="text-[10px] border-primary text-primary">
                          Installed — restart pending
                        </Badge>
                      )}
                      {recommended && !active && !isPendingTarget && (
                        <Badge variant="outline" className="text-[10px] border-primary text-primary">
                          Recommended for you
                        </Badge>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground mt-0.5">{b.subtitle}</p>
                    <p className="text-xs text-muted-foreground mt-0.5">
                      {b.bytes} · {b.when}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant={active ? "outline" : "default"}
                    disabled={anyInstallBlocked}
                    onClick={() => install(b.id)}
                  >
                    {thisInstalling ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : null}
                    {active ? "Active"
                      : isPendingTarget ? "Pending restart"
                      : thisInstalling ? "Installing…"
                      : "Use This"}
                  </Button>
                </div>
              </div>
            );
          })}
        </div>

        {/* Task status/progress */}
        {task.phase !== "idle" && (
          <div className="rounded-md border bg-muted/40 p-3 text-xs space-y-2">
            <div className="flex items-center gap-2">
              {task.running && <Loader2 className="h-3 w-3 animate-spin text-primary" />}
              <span className="font-medium">Status: {task.phase}</span>
            </div>
            <p className="text-muted-foreground">{task.message}</p>
            {task.phase === "complete" && (
              <p className="text-primary font-medium">
                Restart the app to activate the new runtime (close the window and relaunch).
              </p>
            )}
            {task.progress_lines.length > 0 && (
              <details className="text-[10px] text-muted-foreground">
                <summary className="cursor-pointer select-none">
                  Show pip log ({task.progress_lines.length} lines)
                </summary>
                <pre className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-words font-mono">
                  {task.progress_lines.slice(-30).join("\n")}
                </pre>
              </details>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function labelFor(id: string): string {
  switch (id) {
    case "cpu": return "CPU";
    case "cuda": return "NVIDIA (CUDA)";
    case "directml": return "AMD / Intel (DirectML)";
    case "rocm": return "ROCm";
    default: return id;
  }
}
