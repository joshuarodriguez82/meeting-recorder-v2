"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Sparkles, Loader2, Check } from "lucide-react";

// Settings card for the semantic-search index.
//
// The index auto-builds for every newly-processed session — but sessions
// that already existed before the feature shipped are missing entries.
// This card surfaces the gap and lets the user backfill in batches.
//
// Why batches: the backend endpoint runs synchronously per request and
// holds a worker thread for the duration. A 100-session backfill on
// CPU MiniLM takes ~30 seconds — fine on its own, but we want incremental
// progress feedback for the user. So we call /search/index/backfill in
// a loop, 25 sessions at a time, until `remaining` hits 0.

export function SemanticIndexSection() {
  const [status, setStatus] = useState<{
    available: boolean;
    total_sessions: number;
    indexed_sessions: number;
    model_id: string;
  } | null>(null);
  const [running, setRunning] = useState(false);

  const refresh = async () => {
    try {
      const s = await api.searchIndexStatus();
      setStatus(s);
    } catch (e) {
      toast.error(`Could not fetch index status: ${e instanceof Error ? e.message : e}`);
    }
  };

  useEffect(() => { refresh(); }, []);

  const startBackfill = async () => {
    if (running) return;
    setRunning(true);
    try {
      // Loop until /search/index/backfill reports 0 remaining. Each
      // iteration also refreshes the displayed counts.
      // Hard cap on iterations (40 × 25 = 1000 sessions) to defend
      // against a runaway loop if the endpoint ever misreports.
      for (let i = 0; i < 40; i++) {
        const res = await api.searchIndexBackfill(25);
        await refresh();
        if (res.remaining === 0) {
          toast.success(
            `Indexed ${res.embedded_count} session${res.embedded_count === 1 ? "" : "s"}. Backfill complete.`
          );
          return;
        }
        // If a batch made zero progress (every candidate failed), bail —
        // looping forever would be worse than a clear error message.
        if (res.embedded_count === 0) {
          toast.warning("Backfill stalled. Check the backend log for details.");
          return;
        }
      }
      toast.warning("Hit the backfill iteration cap. Run again to continue.");
    } catch (e) {
      toast.error(`Backfill failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setRunning(false);
    }
  };

  if (!status) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">Semantic Index</CardTitle></CardHeader>
        <CardContent><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></CardContent>
      </Card>
    );
  }

  const missing = Math.max(0, status.total_sessions - status.indexed_sessions);
  const fullyIndexed = status.total_sessions > 0 && missing === 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          Semantic Index
        </CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          Voice-of-the-meeting search. Each processed session's transcript
          is embedded with a small (22 MB) MiniLM model that runs locally —
          nothing leaves your machine. New sessions auto-index when you
          process them, and any older sessions get auto-indexed in the
          background on backend startup. The button below is only here
          for forcing a re-index after you&apos;ve manually edited
          embeddings.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {!status.available && (
          <p className="text-sm text-amber-600 dark:text-amber-400">
            sentence-transformers isn&apos;t installed in the backend venv.
            Run <code className="text-[11px] bg-muted px-1 py-0.5 rounded">python3.13 setup.py</code>{" "}
            (or <code className="text-[11px] bg-muted px-1 py-0.5 rounded">pip install sentence-transformers</code>{" "}
            in <code className="text-[11px] bg-muted px-1 py-0.5 rounded">backend/.venv</code>) to enable semantic search.
          </p>
        )}

        <div className="rounded-md border bg-muted/40 p-3 text-xs space-y-1">
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">Indexed</span>
            <span className="font-medium">
              {status.indexed_sessions} of {status.total_sessions} sessions
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">Model</span>
            <span className="font-mono text-[10px]">{status.model_id}</span>
          </div>
        </div>

        {status.available && (
          <div className="flex items-center gap-3">
            {fullyIndexed ? (
              <p className="text-sm text-primary flex items-center gap-2">
                <Check className="h-4 w-4" />
                All sessions indexed.
              </p>
            ) : (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={startBackfill}
                  disabled={running || missing === 0}
                >
                  {running ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : null}
                  {running
                    ? "Indexing…"
                    : `Index ${missing} session${missing === 1 ? "" : "s"} now`}
                </Button>
                <p className="text-xs text-muted-foreground">
                  {running
                    ? "Embedding batches of 25 — keep this open until it finishes."
                    : "Background backfill is already running. This button just speeds it up if you want results immediately."}
                </p>
              </>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={refresh}
              disabled={running}
              className="ml-auto"
            >
              Refresh
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
