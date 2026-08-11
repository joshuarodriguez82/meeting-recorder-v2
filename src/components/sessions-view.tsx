"use client";

import { useEffect, useRef, useState } from "react";
import {
  api, formatDuration, type SessionSummary, type SessionsDiagnostics,
} from "@/lib/api";
import { confirmDialog } from "@/lib/confirm";
import { toast } from "sonner";
import {
  Loader2, Trash2, FolderOpen, Upload, Pencil, Check, X,
  RotateCcw, ChevronDown, ChevronRight, ClipboardCopy,
} from "lucide-react";
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

/**
 * Six-slot pipeline-progress cluster for a session row.
 *
 * These are STATUS INDICATORS, not actions. Every one of the six slots
 * always renders, always in the same order, so a slot's position is
 * meaningful and the clusters line up in a column down the list —
 * "3 of 6 present" is readable at a glance instead of countable.
 *
 * WHY THIS MATTERS: a partially-processed session is the visible
 * signature of a backend crash mid-pipeline (audio captured, transcript
 * written, summary never generated). The user has to be able to spot
 * "this one didn't finish" without opening it, which is exactly why
 * absent stages stay in place as faded glyphs instead of vanishing —
 * and why this cluster must never be collapsed behind an overflow menu.
 *
 * Design review 2026-08-11: the previous treatment rendered only the
 * TRUE stages, each in a bordered circular chip. Variable-length rows
 * meant nothing lined up, and the chips read as a row of buttons.
 */
export function StatusIcons({ session }: { session: SessionSummary }) {
  const stages = [
    {
      done: session.audio_exists, emoji: "🎤",
      doneLabel: "Audio file exists",
      pendingLabel: "Audio — no file on disk",
    },
    {
      done: session.has_transcript, emoji: "⚙",
      doneLabel: "Transcribed + speakers identified",
      pendingLabel: "Transcript — not generated yet",
    },
    {
      done: session.has_summary, emoji: "✨",
      doneLabel: "Summary generated",
      pendingLabel: "Summary — not generated yet",
    },
    {
      done: session.has_action_items, emoji: "📋",
      doneLabel: "Action items extracted",
      pendingLabel: "Action items — not generated yet",
    },
    {
      done: session.has_decisions, emoji: "🎯",
      doneLabel: "Decisions extracted",
      pendingLabel: "Decisions — not generated yet",
    },
    {
      done: session.has_requirements, emoji: "📝",
      doneLabel: "Requirements extracted",
      pendingLabel: "Requirements — not generated yet",
    },
  ];
  const doneCount = stages.filter((s) => s.done).length;
  return (
    <TooltipProvider>
      {/* Tight gap + fixed-width slots so the six glyphs read as one
          progress unit rather than six separate controls. No chip
          backgrounds — those were what made it look like a button row. */}
      <div
        className="flex items-center gap-0.5 shrink-0"
        role="img"
        aria-label={`Processing progress: ${doneCount} of ${stages.length} stages complete`}
      >
        {stages.map((s, idx) => (
          <Tooltip key={idx}>
            <TooltipTrigger
              render={
                <span
                  aria-hidden
                  className={
                    "inline-flex h-6 w-6 items-center justify-center text-[15px] leading-none cursor-default transition-opacity "
                    + (s.done
                      // Present: full-strength glyph. Bumped a couple of
                      // px and un-chipped so it holds contrast against a
                      // white card instead of washing out.
                      ? "opacity-100"
                      // Absent: still occupies its slot, but desaturated
                      // to a faint monochrome ghost so the gap in the
                      // pipeline is obvious without shouting.
                      : "opacity-25 grayscale contrast-50")
                  }
                >
                  {s.emoji}
                </span>
              }
            />
            <TooltipContent>{s.done ? s.doneLabel : s.pendingLabel}</TooltipContent>
          </Tooltip>
        ))}
      </div>
    </TooltipProvider>
  );
}

/**
 * Where the app is looking for sessions and why the count is what it
 * is. GET /sessions/diagnostics has carried this data since it was
 * added for exactly this reason, but nothing in the UI ever read it —
 * field report 2026-08-10: a user with 74 session files on disk saw 24
 * in the app, and diagnosing it took a whole evening of PowerShell
 * scripts sent over chat because the backend already knew the answer
 * and had no way to show it. This renders a compact always-visible
 * summary line, and escalates to an expandable amber panel the moment
 * the numbers disagree or anything was skipped/unreachable — the exact
 * situation that made the count look like silent data loss.
 */
function SessionsDiagnosticsPanel() {
  const [diag, setDiag] = useState<SessionsDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [loadError, setLoadError] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const d = await api.getSessionsDiagnostics();
      setDiag(d);
      setLoadError(false);
    } catch (e) {
      setLoadError(true);
      toast.error(`Session diagnostics failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const copyDetails = () => {
    if (!diag) return;
    navigator.clipboard?.writeText(JSON.stringify(diag, null, 2)).then(
      () => toast.success("Diagnostics copied — paste it into support"),
      () => toast.error("Couldn't copy"),
    );
  };

  if (loadError && !diag) {
    return null; // sessions-list itself still renders; don't block on this
  }
  if (!diag) {
    return (
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground px-1">
        <Loader2 className="h-3 w-3 animate-spin" />
        Checking session folders…
      </div>
    );
  }

  const folderCount = diag.roots.length;
  // Same rule the example in the spec uses: the file count found on
  // disk vs. what the Sessions list actually shows. Skips (unreadable
  // files) and dedupe-across-roots (same session_id in two folders)
  // both show up here even though they have different causes — the
  // point is the two numbers no longer silently agreeing.
  const mismatched = diag.total !== diag.visible_in_app;
  const hasUnreachable = diag.unreachable_roots.length > 0;
  const hasSkips = diag.skipped > 0;
  const isEmpty = diag.visible_in_app === 0;
  const needsAttention = mismatched || hasSkips || hasUnreachable || isEmpty;

  const fileWord = diag.total === 1 ? "file" : "files";
  const folderWord = folderCount === 1 ? "folder" : "folders";
  const summaryText = isEmpty
    ? `No sessions showing — looked in ${folderCount} ${folderWord}`
    : `${diag.total} session ${fileWord} found across ${folderCount} ${folderWord} · ${diag.visible_in_app} shown`;

  const open = expanded || isEmpty;

  if (!needsAttention) {
    return (
      <div className="flex items-center justify-between gap-3 px-1 text-xs text-muted-foreground">
        <span>{summaryText}</span>
        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={refresh}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-muted/60 hover:text-foreground disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
            Refresh
          </button>
        </div>
      </div>
    );
  }

  const unreachableByPath = new Map(
    diag.unreachable_roots.map((r) => [r.path, r.error]),
  );

  return (
    <div
      className={`rounded-2xl border border-amber-500/25 bg-amber-500/10 text-amber-800 dark:text-amber-300 ${isEmpty ? "p-3" : ""}`}
    >
      <div className="flex items-center justify-between gap-3 px-3.5 py-2.5">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          disabled={isEmpty}
          className="flex items-center gap-1.5 text-left text-xs font-medium min-w-0 disabled:cursor-default"
        >
          {!isEmpty && (open
            ? <ChevronDown className="h-3.5 w-3.5 shrink-0" />
            : <ChevronRight className="h-3.5 w-3.5 shrink-0" />)}
          <span aria-hidden className="font-bold">⚠</span>
          <span className="truncate">{summaryText}</span>
        </button>
        <div className="flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={copyDetails}
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] hover:bg-amber-500/15"
            title="Copy full diagnostics JSON for support"
          >
            <ClipboardCopy className="h-3 w-3" />
            Copy details
          </button>
          <button
            type="button"
            onClick={refresh}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] hover:bg-amber-500/15 disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
            Refresh
          </button>
        </div>
      </div>

      {open && (
        <div className="px-3 pb-3 space-y-3 text-xs">
          <div className="space-y-1">
            <div className="font-medium">Folders scanned</div>
            <div className="space-y-1">
              {diag.roots.map((r) => (
                <div key={r.path} className="flex items-start gap-1.5 font-mono break-all">
                  <span className="flex-1">{r.path}</span>
                  <span className="shrink-0 font-sans text-amber-700 dark:text-amber-400">
                    {r.unreachable
                      ? `couldn't be read${unreachableByPath.get(r.path) ? ` (${unreachableByPath.get(r.path)})` : ""}`
                      : `${r.session_files} file${r.session_files === 1 ? "" : "s"}`}
                  </span>
                </div>
              ))}
              {diag.roots.length === 0 && (
                <div className="italic">No folders were reachable at all.</div>
              )}
            </div>
            <div className="text-amber-700 dark:text-amber-400">
              Primary (write) folder: <span className="font-mono break-all">{diag.primary_dir}</span>
            </div>
          </div>

          {hasSkips && (
            <div className="space-y-1">
              <div className="font-medium">
                {diag.skipped} file{diag.skipped === 1 ? "" : "s"} skipped
              </div>
              <div className="space-y-0.5">
                {diag.skipped_detail.slice(0, 8).map((d, i) => (
                  <div key={i} className="font-mono break-all text-[11px]">
                    {d.path} — <span className="font-sans">{d.reason}</span>
                  </div>
                ))}
                {diag.skipped > diag.skipped_detail.slice(0, 8).length && (
                  <div className="italic">
                    …and {diag.skipped - diag.skipped_detail.slice(0, 8).length} more.
                  </div>
                )}
              </div>
            </div>
          )}

          {isEmpty && (
            <div>
              No session files were found in any of the folders above. If
              you expected sessions here, check that the right folder is
              configured (Settings → Recordings folder) or that a synced
              archive folder has finished downloading.
            </div>
          )}
        </div>
      )}
    </div>
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
    if (!(await confirmDialog(`Process ${unprocessed.length} unprocessed sessions?`, { title: "Bulk process" }))) return;
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
    if (!(await confirmDialog(`Delete "${name}"? This removes audio + transcript.`, { title: "Delete session", kind: "warning" }))) return;
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

      <SessionsDiagnosticsPanel />

      {filtered.length === 0 ? (
        <Card>
          <CardContent>
            <p className="text-sm text-muted-foreground py-8 text-center">
              {sessions.length === 0 ? "No sessions yet. Hit Record to create one." : "No matches."}
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {filtered.map((s) => (
            // `group/session-row` drives the hover/focus reveal of the
            // destructive delete control below. Named group (not the
            // card component's own `group/card`) so this row owns it.
            // py-3 trims the stock py-4: design review 2026-08-11 found
            // the cards taller than their content warranted.
            <Card
              key={s.session_id}
              className="group/session-row cursor-pointer py-3"
              onClick={() => onOpenSession(s.session_id)}
            >
              {/* items-start, not items-center: the icon cluster and the
                  delete control now align to the TITLE row instead of
                  floating in the vertical middle, which left dead space
                  under the metadata line on every card. */}
              <CardContent className="flex items-start gap-4">
                <div className="flex-1 min-w-0">
                  <RenamableTitle
                    session={s}
                    onRenamed={onReload}
                  />
                  <div className="text-xs text-muted-foreground flex items-center gap-2 mt-1">
                    <span>
                      {s.started_at ? new Date(s.started_at).toLocaleString() : "—"}
                    </span>
                    <span>·</span>
                    <span>{formatDuration(s.duration_s)}</span>
                    {s.client && (<><span>·</span><span>{s.client}</span></>)}
                    {s.project && (<><span>·</span><span>{s.project}</span></>)}
                  </div>
                  {s.audio_integrity_warning && (
                    <div
                      className="inline-flex items-start gap-1.5 max-w-full rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300 text-[11px] px-2.5 py-1 mt-2"
                      title="The audio file is shorter than the recording window. Click the session to see details."
                    >
                      <span aria-hidden className="font-bold leading-none mt-0.5">⚠</span>
                      <span className="flex-1">{s.audio_integrity_warning}</span>
                    </div>
                  )}
                  {s.processing_error && (
                    <div
                      className="inline-flex items-start gap-1.5 max-w-full rounded-full border border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300 text-[11px] px-2.5 py-1 mt-2"
                      title="Auto-processing failed after retries. Open the session and click Process to retry."
                    >
                      <span aria-hidden className="font-bold leading-none mt-0.5">⚠</span>
                      <span className="flex-1">
                        {s.processing_error} — open the session and click Process to retry.
                      </span>
                    </div>
                  )}
                  {s.sync_warning && (
                    <div
                      className="inline-flex items-start gap-1.5 max-w-full rounded-full border border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300 text-[11px] px-2.5 py-1 mt-2"
                      title="Capture sync measurement — the audio/transcript may be slightly misaligned. Informational; no audio was altered."
                    >
                      <span aria-hidden className="leading-none mt-0.5">ⓘ</span>
                      <span className="flex-1">{s.sync_warning}</span>
                    </div>
                  )}
                </div>
                <StatusIcons session={s} />
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger
                      render={
                        // Destructive + irreversible, so it's revealed on
                        // hover/focus rather than sitting armed on every
                        // row. Deliberately opacity-based, never
                        // `display:none`/`hidden` — the button stays in
                        // the tab order and `group-focus-within` +
                        // `focus-visible` bring it back into view the
                        // moment it takes keyboard focus. `pointer-events`
                        // is gated alongside opacity so an invisible
                        // delete target can never be clicked by accident.
                        // Design review 2026-08-11.
                        <button
                          type="button"
                          onClick={(e) => { e.stopPropagation(); del(s.session_id, s.display_name); }}
                          className="h-8 w-8 inline-flex items-center justify-center rounded-full hover:bg-destructive/10 text-muted-foreground hover:text-destructive cursor-pointer shrink-0 opacity-0 pointer-events-none transition-opacity group-hover/session-row:opacity-100 group-hover/session-row:pointer-events-auto group-focus-within/session-row:opacity-100 group-focus-within/session-row:pointer-events-auto focus-visible:opacity-100 focus-visible:pointer-events-auto"
                          aria-label={`Delete "${s.display_name}"`}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      }
                    />
                    <TooltipContent>Delete session</TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
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
            <Label>Audio / video file</Label>
            <div className="flex gap-2">
              <Input
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="C:\Users\joshu\Downloads\teams-recording.mp4"
                autoFocus
                autoComplete="off"
                className="flex-1"
              />
              <Button
                type="button"
                variant="outline"
                onClick={async () => {
                  try {
                    const { open } = await import("@tauri-apps/plugin-dialog");
                    const picked = await open({
                      multiple: false,
                      directory: false,
                      title: "Choose recording to import",
                      filters: [
                        {
                          name: "Audio / video",
                          extensions: ["wav", "mp3", "m4a", "flac", "mp4", "mov"],
                        },
                      ],
                    });
                    if (typeof picked === "string" && picked) {
                      setPath(picked);
                    }
                  } catch (e) {
                    toast.error(
                      `File picker unavailable: ${(e as Error).message ?? e}`);
                  }
                }}
              >
                Browse…
              </Button>
            </div>
            <p className="text-[11px] text-muted-foreground">
              Accepts .wav, .mp3, .m4a, .flac, .mp4, or .mov. The file is
              copied into your recordings folder — the original stays where
              it is.
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
