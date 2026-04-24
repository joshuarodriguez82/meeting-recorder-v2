"use client";

import { useEffect, useRef, useState } from "react";
import { api, formatDuration, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Trash2, FolderOpen, Upload, Pencil, Check, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface Props {
  sessions: SessionSummary[];
  onReload: () => void;
  onOpenSession: (id: string) => void;
}

async function openRecordingsFolder(): Promise<void> {
  try {
    await api.openFolder({ kind: "recordings" });
  } catch (e) {
    toast.error(`Could not open folder: ${e instanceof Error ? e.message : e}`);
  }
}

/**
 * Inline rename with a pencil-toggle. Click the pencil to enter edit mode;
 * Enter saves, Escape cancels. Keeps the row clickable when not editing so
 * the usual behaviour (open session) still works.
 */
function RenamableTitle({
  session, onRenamed,
}: {
  session: SessionSummary;
  onRenamed: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(session.display_name);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => { setValue(session.display_name); }, [session.display_name]);
  useEffect(() => { if (editing) inputRef.current?.select(); }, [editing]);

  const save = async () => {
    const next = value.trim();
    if (!next || next === session.display_name) {
      setEditing(false);
      setValue(session.display_name);
      return;
    }
    setSaving(true);
    try {
      await api.patchSession(session.session_id, { display_name: next });
      toast.success("Renamed");
      setEditing(false);
      onRenamed();
    } catch (e) {
      toast.error(`Rename failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setEditing(false);
    setValue(session.display_name);
  };

  if (editing) {
    return (
      <div
        className="flex items-center gap-1 min-w-0"
        onClick={(e) => e.stopPropagation()}
      >
        <Input
          ref={inputRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") cancel();
          }}
          disabled={saving}
          className="h-7 text-sm"
          autoFocus
        />
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="h-7 w-7 inline-flex items-center justify-center rounded-md hover:bg-accent"
          title="Save (Enter)"
        >
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
        </button>
        <button
          type="button"
          onClick={cancel}
          disabled={saving}
          className="h-7 w-7 inline-flex items-center justify-center rounded-md hover:bg-accent"
          title="Cancel (Esc)"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1 min-w-0 group">
      <span className="text-sm font-medium truncate">{session.display_name}</span>
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setEditing(true); }}
        className="h-6 w-6 inline-flex items-center justify-center rounded-md opacity-0 group-hover:opacity-100 hover:bg-accent text-muted-foreground hover:text-foreground transition-opacity shrink-0"
        title="Rename session"
      >
        <Pencil className="h-3 w-3" />
      </button>
    </div>
  );
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
  const [importOpen, setImportOpen] = useState(false);

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
      <div className="flex gap-3 flex-wrap">
        <Input
          placeholder="Filter by name, client, project..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="max-w-md"
        />
        <div className="flex gap-2 ml-auto">
          <Button variant="outline" onClick={openRecordingsFolder}>
            <FolderOpen className="h-4 w-4 mr-2" />
            Open Recordings Folder
          </Button>
          <Button variant="outline" onClick={() => setImportOpen(true)}>
            <Upload className="h-4 w-4 mr-2" />
            Load Session
          </Button>
          {unprocessed.length > 0 && (
            <Button onClick={bulkProcess} disabled={bulkRunning}>
              {bulkRunning ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
              Bulk Process ({unprocessed.length})
            </Button>
          )}
        </div>
      </div>

      <ImportSessionDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onImported={(id) => {
          onReload();
          onOpenSession(id);
        }}
      />

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
                  className="flex items-center gap-4 border-b last:border-b-0 p-4 hover:bg-muted/40 transition-colors cursor-pointer"
                  onClick={() => onOpenSession(s.session_id)}
                >
                  <div className="flex-1 min-w-0">
                    <RenamableTitle
                      session={s}
                      onRenamed={onReload}
                    />
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
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function ImportSessionDialog({
  open, onOpenChange, onImported,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onImported: (sessionId: string) => void;
}) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [busy, setBusy] = useState(false);

  const handleImport = async () => {
    const p = path.trim();
    if (!p) return;
    setBusy(true);
    try {
      const res = await api.importSession({
        file_path: p,
        display_name: name.trim(),
        client: client.trim(),
        project: project.trim(),
      });
      toast.success("Session loaded");
      onOpenChange(false);
      setPath(""); setName(""); setClient(""); setProject("");
      onImported(res.session_id);
    } catch (e) {
      toast.error(`Load failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Load Session from Audio File</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="space-y-2">
            <Label>Audio file path</Label>
            <Input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="C:\Users\joshu\Downloads\teams-recording.m4a"
              autoFocus
              autoComplete="off"
            />
            <p className="text-[11px] text-muted-foreground">
              Accepts .wav, .mp3, .m4a, or .flac. The file is copied into your
              recordings folder — the original stays where it is.
            </p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label>Meeting name (optional)</Label>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Defaults to the filename"
                autoComplete="off"
              />
            </div>
            <div className="space-y-2">
              <Label>Client (optional)</Label>
              <Input
                value={client}
                onChange={(e) => setClient(e.target.value)}
                autoComplete="off"
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label>Project (optional)</Label>
            <Input
              value={project}
              onChange={(e) => setProject(e.target.value)}
              autoComplete="off"
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={handleImport} disabled={!path.trim() || busy}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : null}
            Load
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
