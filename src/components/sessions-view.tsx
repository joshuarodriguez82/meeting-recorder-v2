"use client";

import { useState } from "react";
import { api, formatDuration, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Trash2, FolderOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";

interface Props {
  sessions: SessionSummary[];
  onReload: () => void;
  onOpenSession: (id: string) => void;
}

export function SessionsView({ sessions, onReload }: Props) {
  const [filter, setFilter] = useState("");
  const [bulkRunning, setBulkRunning] = useState(false);

  const filtered = sessions.filter((s) => {
    if (!filter) return true;
    const q = filter.toLowerCase();
    return (
      s.display_name.toLowerCase().includes(q) ||
      s.client.toLowerCase().includes(q) ||
      s.project.toLowerCase().includes(q)
    );
  });

  const unprocessed = sessions.filter((s) => s.audio_exists && !s.has_transcript);

  const bulkProcess = async () => {
    if (!unprocessed.length) return;
    if (!confirm(`Process ${unprocessed.length} unprocessed sessions?`)) return;
    setBulkRunning(true);
    let done = 0, failed = 0;
    for (const s of unprocessed) {
      try {
        await api.processSession(s.session_id);
        done++;
      } catch (e) {
        failed++;
        console.error(`Failed: ${s.session_id}`, e);
      }
    }
    setBulkRunning(false);
    toast.success(`Bulk process complete: ${done} done, ${failed} failed`);
    onReload();
  };

  const del = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}"? This removes audio + transcript.`)) return;
    try {
      await api.deleteSession(id);
      toast.success("Session deleted");
      onReload();
    } catch (e) {
      toast.error(`Delete failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex gap-3">
        <Input
          placeholder="Filter by name, client, project..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="max-w-md"
        />
        {unprocessed.length > 0 && (
          <Button onClick={bulkProcess} disabled={bulkRunning}>
            {bulkRunning ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
            Bulk Process ({unprocessed.length})
          </Button>
        )}
      </div>

      <Card>
        <CardContent className="p-0">
          {filtered.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              {sessions.length === 0 ? "No sessions yet. Hit Record to create one." : "No matches."}
            </p>
          ) : (
            <div>
              {filtered.map((s) => (
                <div
                  key={s.session_id}
                  className="flex items-center gap-4 border-b last:border-b-0 p-4 hover:bg-muted/40 transition-colors"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{s.display_name}</div>
                    <div className="text-xs text-muted-foreground flex items-center gap-2 mt-0.5">
                      <span>
                        {s.started_at ? new Date(s.started_at).toLocaleString() : "—"}
                      </span>
                      <span>·</span>
                      <span>{formatDuration(s.duration_s)}</span>
                      {s.client && (<><span>·</span><span>{s.client}</span></>)}
                      {s.project && (<><span>·</span><span>{s.project}</span></>)}
                    </div>
                  </div>
                  <div className="flex items-center gap-1 flex-wrap justify-end">
                    {s.audio_exists && <Badge variant="outline" className="text-[10px]">🎤</Badge>}
                    {s.has_transcript && <Badge variant="outline" className="text-[10px]">⚙</Badge>}
                    {s.has_summary && <Badge variant="outline" className="text-[10px]">✨</Badge>}
                    {s.has_action_items && <Badge variant="outline" className="text-[10px]">📋</Badge>}
                    {s.has_decisions && <Badge variant="outline" className="text-[10px]">🎯</Badge>}
                    {s.has_requirements && <Badge variant="outline" className="text-[10px]">📝</Badge>}
                  </div>
                  <Button size="icon" variant="ghost" onClick={() => del(s.session_id, s.display_name)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
