"use client";

import { useState } from "react";
import { api, formatDuration, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Trash2, FolderOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface Props {
  sessions: SessionSummary[];
  onReload: () => void;
  onOpenSession: (id: string) => void;
}

export function StatusIcons({ session }: { session: SessionSummary }) {
  const icons = [
    { show: session.audio_exists, emoji: "🎤", label: "Audio file exists" },
    { show: session.has_transcript, emoji: "⚙", label: "Transcribed + speakers identified" },
    { show: session.has_summary, emoji: "✨", label: "Summary generated" },
    { show: session.has_action_items, emoji: "📋", label: "Action items extracted" },
    { show: session.has_decisions, emoji: "🎯", label: "Decisions extracted" },
    { show: session.has_requirements, emoji: "📝", label: "Requirements extracted" },
  ];
  return (
    <TooltipProvider>
      {icons.map((i, idx) => i.show && (
        <Tooltip key={idx}>
          <TooltipTrigger
            render={<span className="inline-flex items-center rounded-full border px-1.5 py-0.5 text-[11px] cursor-default">{i.emoji}</span>}
          />
          <TooltipContent>{i.label}</TooltipContent>
        </Tooltip>
      ))}
    </TooltipProvider>
  );
}

export function SessionsView({ sessions, onReload, onOpenSession }: Props) {
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
                <button
                  key={s.session_id}
                  onClick={() => onOpenSession(s.session_id)}
                  className="w-full text-left flex items-center gap-4 border-b last:border-b-0 p-4 hover:bg-muted/40 transition-colors"
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
                    <StatusIcons session={s} />
                  </div>
                  <span
                    role="button"
                    onClick={(e) => { e.stopPropagation(); del(s.session_id, s.display_name); }}
                    className="h-7 w-7 inline-flex items-center justify-center rounded-md hover:bg-destructive/10 text-muted-foreground hover:text-destructive cursor-pointer"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </span>
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
