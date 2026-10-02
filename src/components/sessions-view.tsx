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
  Mic, Captions, Sparkles, ListChecks, Target, FileText, Video,
  type LucideIcon,
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
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  IMPORT_EXTENSIONS, TRANSCRIPT_EXTENSIONS, fileNameOf, groupImportFiles,
  isImportable, isTranscript, isVideo, itemName, type ImportItem,
} from "@/lib/media-import";

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

/** " (running for Xm Ys)" (or " (queued for Xm Ys)" when `verb` is
 * overridden) for the Finalizing/Queued chip, or "" if the timestamp is
 * missing/unparseable — the chip still reads fine without a duration,
 * it just loses the specific number. */
function _finalizeElapsedSuffix(
  startedAt?: string | null, verb: string = "running",
): string {
  if (!startedAt) return "";
  const startedMs = new Date(startedAt).getTime();
  if (Number.isNaN(startedMs)) return "";
  const elapsedS = Math.max(0, Math.round((Date.now() - startedMs) / 1000));
  return ` (${verb} for ${formatDuration(elapsedS)})`;
}

export function StatusIcons({ session }: { session: SessionSummary }) {
  const stages: { done: boolean; icon: LucideIcon; doneLabel: string; pendingLabel: string }[] = [
    {
      done: session.audio_exists, icon: Mic,
      doneLabel: "Audio file exists",
      pendingLabel: "Audio — no file on disk",
    },
    {
      done: session.has_transcript, icon: Captions,
      doneLabel: "Transcribed + speakers identified",
      pendingLabel: "Transcript — not generated yet",
    },
    {
      done: session.has_summary, icon: Sparkles,
      doneLabel: "Summary generated",
      pendingLabel: "Summary — not generated yet",
    },
    {
      done: session.has_action_items, icon: ListChecks,
      doneLabel: "Action items extracted",
      pendingLabel: "Action items — not generated yet",
    },
    {
      done: session.has_decisions, icon: Target,
      doneLabel: "Decisions extracted",
      pendingLabel: "Decisions — not generated yet",
    },
    {
      done: session.has_requirements, icon: FileText,
      doneLabel: "Requirements extracted",
      pendingLabel: "Requirements — not generated yet",
    },
  ];
  const doneCount = stages.filter((s) => s.done).length;
  return (
    <TooltipProvider>
      {/* Tight gap + fixed-width slots so the six icons read as one
          progress unit rather than six separate controls. No chip
          backgrounds — those were what made it look like a button row. */}
      <div
        className="flex items-center gap-0.5 shrink-0"
        role="img"
        aria-label={`Processing progress: ${doneCount} of ${stages.length} stages complete`}
      >
        {stages.map((s, idx) => {
          const Icon = s.icon;
          return (
            <Tooltip key={idx}>
              <TooltipTrigger
                render={
                  <span
                    aria-hidden
                    className={
                      "inline-flex h-6 w-6 items-center justify-center leading-none cursor-default transition-opacity "
                      + (s.done
                        // Present: full-strength icon in the app's own
                        // accent color, so a completed stage reads as
                        // "on" rather than just "less transparent".
                        ? "text-primary opacity-100"
                        // Absent: still occupies its slot, but faded to
                        // a faint ghost so the gap in the pipeline is
                        // obvious without shouting.
                        : "text-muted-foreground opacity-30")
                    }
                  >
                    <Icon className="h-3.5 w-3.5" strokeWidth={2.25} />
                  </span>
                }
              />
              <TooltipContent>{s.done ? s.doneLabel : s.pendingLabel}</TooltipContent>
            </Tooltip>
          );
        })}
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

  // The backend's declared response shape says every one of these
  // fields is always present, but a half-failed init (some endpoints
  // 200, others 500 mid-startup) can ship a partial object anyway — the
  // exact case that used to crash this whole tab on `diag.roots.length`.
  // Every array/number read below goes through one of these safe
  // accessors instead of trusting the type, and `partial` tracks
  // whether anything was actually missing so the summary line can say
  // "unavailable" rather than quietly reporting a fabricated 0 — a
  // failed-to-load field must never read the same as a genuinely empty
  // one.
  const rootsKnown = Array.isArray(diag.roots);
  const roots = diag.roots ?? [];
  const unreachableKnown = Array.isArray(diag.unreachable_roots);
  const unreachableRoots = diag.unreachable_roots ?? [];
  const skippedDetailKnown = Array.isArray(diag.skipped_detail);
  const skippedDetail = diag.skipped_detail ?? [];
  const totalKnown = typeof diag.total === "number";
  const visibleKnown = typeof diag.visible_in_app === "number";
  const skippedKnown = typeof diag.skipped === "number";
  const primaryDirKnown = typeof diag.primary_dir === "string" && diag.primary_dir.length > 0;

  const partial = !rootsKnown || !unreachableKnown || !skippedDetailKnown
    || !totalKnown || !visibleKnown || !skippedKnown;

  const folderCount = roots.length;
  // Same rule the example in the spec uses: the file count found on
  // disk vs. what the Sessions list actually shows. Skips (unreadable
  // files) and dedupe-across-roots (same session_id in two folders)
  // both show up here even though they have different causes — the
  // point is the two numbers no longer silently agreeing.
  const mismatched = totalKnown && visibleKnown && diag.total !== diag.visible_in_app;
  const hasUnreachable = unreachableRoots.length > 0;
  const hasSkips = skippedKnown && diag.skipped > 0;
  const isEmpty = visibleKnown && diag.visible_in_app === 0;
  const needsAttention = partial || mismatched || hasSkips || hasUnreachable || isEmpty;

  const fileWord = diag.total === 1 ? "file" : "files";
  const folderWord = folderCount === 1 ? "folder" : "folders";
  const summaryText = partial
    ? "Session diagnostics response was incomplete — some folder/file counts are unavailable"
    : isEmpty
      ? `No sessions showing — looked in ${folderCount} ${folderWord}`
      : `${diag.total} session ${fileWord} found across ${folderCount} ${folderWord} · ${diag.visible_in_app} shown`;

  const open = expanded || isEmpty || partial;

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
    unreachableRoots.map((r) => [r.path, r.error]),
  );
  // Same "always visible, never collapsible away" treatment as isEmpty:
  // a malformed response is an anomaly, not a state the user should be
  // able to dismiss without seeing it.
  const forcedOpen = isEmpty || partial;

  return (
    <div
      className={`rounded-2xl border border-amber-500/25 bg-amber-500/10 text-amber-800 dark:text-amber-300 ${forcedOpen ? "p-3" : ""}`}
    >
      <div className="flex items-center justify-between gap-3 px-3.5 py-2.5">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          disabled={forcedOpen}
          className="flex items-center gap-1.5 text-left text-xs font-medium min-w-0 disabled:cursor-default"
        >
          {!forcedOpen && (open
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
          {partial && (
            <div>
              The backend&apos;s diagnostics response was missing some
              expected fields — this usually means the backend is still
              finishing startup or hit an error partway through. What
              could be read is shown below; anything marked
              &quot;unavailable&quot; failed to load and is not
              necessarily empty.
            </div>
          )}

          <div className="space-y-1">
            <div className="font-medium">Folders scanned</div>
            <div className="space-y-1">
              {roots.map((r) => (
                <div key={r.path} className="flex items-start gap-1.5 font-mono break-all">
                  <span className="flex-1">{r.path}</span>
                  <span className="shrink-0 font-sans text-amber-700 dark:text-amber-400">
                    {r.unreachable
                      ? `couldn't be read${unreachableByPath.get(r.path) ? ` (${unreachableByPath.get(r.path)})` : ""}`
                      : `${r.session_files} file${r.session_files === 1 ? "" : "s"}`}
                  </span>
                </div>
              ))}
              {roots.length === 0 && (
                <div className="italic">
                  {rootsKnown ? "No folders were reachable at all." : "Folder list unavailable."}
                </div>
              )}
            </div>
            <div className="text-amber-700 dark:text-amber-400">
              Primary (write) folder:{" "}
              <span className="font-mono break-all">
                {primaryDirKnown ? diag.primary_dir : "unavailable"}
              </span>
            </div>
          </div>

          {hasSkips && (
            <div className="space-y-1">
              <div className="font-medium">
                {diag.skipped} file{diag.skipped === 1 ? "" : "s"} skipped
              </div>
              <div className="space-y-0.5">
                {skippedDetail.slice(0, 8).map((d, i) => (
                  <div key={i} className="font-mono break-all text-[11px]">
                    {d.path} — <span className="font-sans">{d.reason}</span>
                  </div>
                ))}
                {!skippedDetailKnown && (
                  <div className="italic">Skip details unavailable.</div>
                )}
                {skippedDetailKnown && diag.skipped > skippedDetail.slice(0, 8).length && (
                  <div className="italic">
                    …and {diag.skipped - skippedDetail.slice(0, 8).length} more.
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
  // The import list lives here so files dropped on the tab land in it
  // directly, whether or not the window is already open.
  const [importItems, setImportItems] = useState<ImportItem[]>([]);
  const [dragging, setDragging] = useState(false);

  // Drop a video or audio file anywhere on this tab to import it. The
  // desktop webview hands dropped files to the app as paths through
  // its own event (an HTML drop handler never sees them), and the
  // backend reads the file from that path — nothing is uploaded.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    (async () => {
      try {
        const { getCurrentWebview } = await import("@tauri-apps/api/webview");
        const off = await getCurrentWebview().onDragDropEvent((event) => {
          const p = event.payload;
          if (p.type === "enter" || p.type === "over") {
            setDragging(true);
          } else if (p.type === "leave") {
            setDragging(false);
          } else if (p.type === "drop") {
            setDragging(false);
            const paths = p.paths ?? [];
            if (paths.some((x) => isImportable(x) || isTranscript(x))) {
              noticeRejected(paths);
              setImportItems((prev) => groupImportFiles(paths, prev).items);
              setImportOpen(true);
            } else if (paths.length) {
              toast.error("Those files can't be imported", {
                description: "Drop videos, audio files, or Teams / Zoom transcripts (.vtt, .srt, .docx).",
              });
            }
          }
        });
        if (cancelled) off(); else unlisten = off;
      } catch {
        // Not running inside the desktop app (e.g. a browser preview):
        // Browse… and a pasted path still work.
      }
    })();
    return () => { cancelled = true; unlisten?.(); };
  }, []);

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
            Import Recording
          </Button>
          {unprocessed.length > 0 && (
            <Button onClick={bulkProcess} disabled={bulkRunning}>
              {bulkRunning ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
              Bulk Process ({unprocessed.length})
            </Button>
          )}
        </div>
      </div>

      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm">
          <div className="rounded-lg border-2 border-dashed border-primary px-8 py-6 text-center">
            <Upload className="mx-auto h-6 w-6 text-primary" />
            <p className="mt-2 text-sm font-medium">Drop to import</p>
            <p className="text-xs text-muted-foreground">Videos, audio, and Teams / Zoom transcripts</p>
          </div>
        </div>
      )}

      <ImportSessionDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        sessions={sessions}
        items={importItems}
        setItems={setImportItems}
        onImported={(ids) => {
          onReload();
          // One meeting: open it. Several: they're in the list.
          if (ids.length === 1) onOpenSession(ids[0]);
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
                  {s.finalize_status === "queued" && (
                    <div
                      className="inline-flex items-start gap-1.5 max-w-full rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300 text-[11px] px-2.5 py-1 mt-2"
                      title="At most one finalize job runs at a time so it never competes with a live recording for CPU — this one is waiting for another finalize to finish. This is normal — no data has been lost."
                    >
                      <Loader2 className="h-3 w-3 animate-spin shrink-0 mt-0.5" aria-hidden />
                      <span className="flex-1">
                        Queued{_finalizeElapsedSuffix(s.finalize_started_at, "queued")}
                        {" "}— waiting behind another finalize job.
                      </span>
                    </div>
                  )}
                  {s.finalize_status === "finalizing" && (
                    <div
                      className="inline-flex items-start gap-1.5 max-w-full rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300 text-[11px] px-2.5 py-1 mt-2"
                      title={
                        s.finalize_aec_requested
                          ? "The post-stop finalize step (WAV merge + echo cancellation) is still running. This is normal — no data has been lost. AI actions become available once it finishes."
                          : "The post-stop finalize step (WAV merge) is still running. This is normal — no data has been lost. AI actions become available once it finishes."
                      }
                    >
                      <Loader2 className="h-3 w-3 animate-spin shrink-0 mt-0.5" aria-hidden />
                      <span className="flex-1">
                        Finalizing{_finalizeElapsedSuffix(s.finalize_started_at)}
                        {/* Named echo cancellation UNCONDITIONALLY until
                            2026-08-20, so a user with the setting OFF was
                            told it was running — while the event log for
                            that very session recorded aec_requested:
                            false. AEC was off and correctly not running;
                            only the sentence was wrong.

                            Describing work that is not happening is the
                            same defect as describing a result that was
                            never established, pointing the other way. It
                            also cost the user real time: "several
                            minutes" is true of AEC and not of a merge
                            that takes seconds, so a normal 12-second
                            finalize read as something being stuck. */}
                        {s.finalize_aec_requested
                          ? " — echo cancellation can take several minutes."
                          : " — merging the audio tracks; usually a few seconds."}
                      </span>
                    </div>
                  )}
                  {s.finalize_status === "failed" && (
                    <div
                      className="inline-flex items-start gap-1.5 max-w-full rounded-full border border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300 text-[11px] px-2.5 py-1 mt-2"
                      title="The post-stop finalize step failed — the audio may not be usable. Open the session for details."
                    >
                      <span aria-hidden className="font-bold leading-none mt-0.5">⚠</span>
                      <span className="flex-1">
                        Finalize failed{s.finalize_error ? `: ${s.finalize_error}` : ""}
                      </span>
                    </div>
                  )}
                  {/* First, and red: this one changes what the rest of
                      the session means — the transcript, summary and
                      action items are one side of the conversation. */}
                  {s.capture_warning && (
                    <div
                      className="inline-flex items-start gap-1.5 max-w-full rounded-full border border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300 text-[11px] px-2.5 py-1 mt-2"
                      title="System audio never reached this recording, so only your microphone was captured."
                    >
                      <span aria-hidden className="font-bold leading-none mt-0.5">⚠</span>
                      <span className="flex-1">{s.capture_warning}</span>
                    </div>
                  )}
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

/** Say which of `paths` can't be imported. Kept out of the state
 *  update itself, which React may run twice. */
function noticeRejected(paths: string[]): void {
  const rejected = paths.filter((p) => !isImportable(p) && !isTranscript(p));
  if (!rejected.length) return;
  toast.error(
    rejected.length === 1
      ? `Can't import ${fileNameOf(rejected[0])}`
      : `Can't import ${rejected.length} of those files`,
    { description: "Use a video, an audio file, or a Teams / Zoom transcript (.vtt, .srt, .docx)." });
}

function ImportSessionDialog({
  open, onOpenChange, onImported, sessions, items, setItems,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** Called once, with every meeting that was imported. */
  onImported: (sessionIds: string[]) => void;
  sessions: SessionSummary[];
  /** The meetings to import; files dropped on the tab are added here. */
  items: ImportItem[];
  setItems: React.Dispatch<React.SetStateAction<ImportItem[]>>;
}) {
  const [pasted, setPasted] = useState("");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [template, setTemplate] = useState("General");
  const [templates, setTemplates] = useState<string[]>(["General"]);
  const [processNow, setProcessNow] = useState(true);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");

  const addPaths = (paths: string[]) => {
    noticeRejected(paths);
    setItems((prev) => groupImportFiles(paths, prev).items);
  };

  useEffect(() => {
    if (!open) return;
    api.getTemplates()
      .then((t) => { if (t.length) setTemplates(t.map((x) => x.name)); })
      .catch(() => { /* keep "General" */ });
  }, [open]);

  // Same suggestions the Record tab offers: every client already used,
  // and the projects tagged under the chosen client.
  const existingClients = Array.from(new Set(
    sessions.map((s) => (s.client || "").trim()).filter(Boolean))).sort();
  const existingProjects = Array.from(new Set(
    sessions
      .filter((s) => !client.trim()
        || (s.client || "").trim().toLowerCase() === client.trim().toLowerCase())
      .map((s) => (s.project || "").trim())
      .filter(Boolean))).sort();

  const update = (i: number, patch: Partial<ImportItem>) =>
    setItems((prev) => prev.map((it, j) => (j === i ? { ...it, ...patch } : it)));
  const remove = (i: number) =>
    setItems((prev) => prev.filter((_, j) => j !== i));

  const pick = async (kind: "any" | "transcript"): Promise<string[]> => {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const picked = await open({
        multiple: kind === "any",
        directory: false,
        title: kind === "any"
          ? "Choose recordings and transcripts to import"
          : "Choose the meeting's transcript",
        filters: kind === "any"
          ? [
            { name: "Recordings and transcripts",
              extensions: [...IMPORT_EXTENSIONS, ...TRANSCRIPT_EXTENSIONS] },
            { name: "Video or audio", extensions: [...IMPORT_EXTENSIONS] },
            { name: "Transcripts (Teams / Zoom)", extensions: [...TRANSCRIPT_EXTENSIONS] },
          ]
          : [{ name: "Transcripts (Teams / Zoom)", extensions: [...TRANSCRIPT_EXTENSIONS] }],
      });
      if (Array.isArray(picked)) return picked.filter(Boolean);
      return typeof picked === "string" && picked ? [picked] : [];
    } catch (e) {
      toast.error(`File picker unavailable: ${(e as Error).message ?? e}`);
      return [];
    }
  };

  const reset = () => {
    setItems([]); setPasted(""); setClient(""); setProject("");
    setTemplate("General"); setProcessNow(true); setProgress("");
  };

  const handleImport = async () => {
    if (!items.length) return;
    setBusy(true);
    const imported: string[] = [];
    const failed: { item: ImportItem; error: string }[] = [];
    const notes: string[] = [];
    let slides = false;
    for (const [n, item] of items.entries()) {
      setProgress(items.length > 1
        ? `Importing ${n + 1} of ${items.length}: ${itemName(item)}`
        : isVideo(item.recording) ? "Extracting audio…" : "Importing…");
      try {
        const res = await api.importSession({
          file_path: item.recording,
          transcript_path: item.transcript,
          display_name: item.name.trim(),
          client: client.trim(),
          project: project.trim(),
          template,
          process: processNow,
        });
        imported.push(res.session_id);
        slides = slides || !!res.slides;
        for (const note of res.notes ?? []) {
          notes.push(items.length > 1 ? `${itemName(item)}: ${note}` : note);
        }
      } catch (e) {
        failed.push({ item, error: e instanceof Error ? e.message : String(e) });
      }
    }
    setBusy(false);
    setProgress("");

    if (imported.length) {
      // A client typed here for the first time becomes a real client,
      // as it does when tagged on the Record tab.
      const c = client.trim();
      if (c && !existingClients.some((x) => x.toLowerCase() === c.toLowerCase())) {
        api.setClientConfig(c, { export_folder: "" }).catch(() => {});
      }
      const what = imported.length === 1 ? "Imported" : `Imported ${imported.length} meetings`;
      const parts = [
        processNow
          ? (imported.length === 1
            ? "Transcript, speakers, summary and action items fill in in the background; a long meeting can take several minutes."
            : "They process one after another in the background.")
          : "Process them from each meeting when you're ready.",
        slides ? "Slides and shared screens from the video go in the Screenshots tab." : "",
        ...notes,
      ].filter(Boolean);
      toast.success(what, { description: parts.join(" ") });
      onImported(imported);
    }
    if (failed.length) {
      toast.error(
        failed.length === 1
          ? `Couldn't import ${itemName(failed[0].item)}`
          : `${failed.length} meetings couldn't be imported`,
        { description: failed.map((f) => `${itemName(f.item)}: ${f.error}`).join("\n") });
      // Keep only what failed, to fix and retry.
      setItems(failed.map((f) => f.item));
    } else {
      onOpenChange(false);
      reset();
    }
  };

  const count = items.length;
  return (
    <Dialog open={open} onOpenChange={(v) => { if (!busy) onOpenChange(v); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Import recordings</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <p className="text-xs text-muted-foreground">
            For meetings recorded somewhere else: Teams or Zoom videos,
            audio files, and their transcripts. Each becomes a session like
            any other. Add the meeting&apos;s Teams / Zoom transcript (.vtt or
            .docx) to get everyone&apos;s real names; a transcript can also be
            imported on its own.
          </p>

          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <Label>Meetings</Label>
              <Button type="button" variant="outline" size="sm" disabled={busy}
                onClick={async () => addPaths(await pick("any"))}>
                Add files…
              </Button>
            </div>
            {count === 0 ? (
              <div className="rounded-md border border-dashed p-6 text-center text-xs text-muted-foreground">
                <Upload className="mx-auto mb-2 h-5 w-5" />
                Drop files anywhere on the Sessions tab, or use Add files.
                A video and a transcript with the same name are paired.
              </div>
            ) : (
              <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                {items.map((item, i) => (
                  <div key={`${item.recording}|${item.transcript}`}
                    className="space-y-1.5 rounded-md border p-2">
                    <div className="flex items-center gap-2">
                      <Input
                        value={item.name}
                        onChange={(e) => update(i, { name: e.target.value })}
                        placeholder={itemName({ ...item, name: "" })}
                        aria-label="Meeting name"
                        autoComplete="off"
                        className="h-8 flex-1"
                        disabled={busy}
                      />
                      <Button type="button" variant="ghost" size="sm" disabled={busy}
                        aria-label="Remove from the list" onClick={() => remove(i)}>
                        <X className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                      <span className="inline-flex items-center gap-1">
                        {item.recording
                          ? (isVideo(item.recording)
                            ? <Video className="h-3 w-3" />
                            : <Mic className="h-3 w-3" />)
                          : <Mic className="h-3 w-3 opacity-40" />}
                        {item.recording ? fileNameOf(item.recording) : "No recording — transcript only"}
                      </span>
                      <span className="inline-flex items-center gap-1">
                        <Captions className={`h-3 w-3 ${item.transcript ? "" : "opacity-40"}`} />
                        {item.transcript ? (
                          <>
                            {fileNameOf(item.transcript)}
                            {item.recording && (
                              <button type="button" disabled={busy}
                                className="underline-offset-2 hover:underline"
                                onClick={() => update(i, { transcript: "" })}>
                                remove
                              </button>
                            )}
                          </>
                        ) : (
                          <button type="button" disabled={busy}
                            className="underline-offset-2 hover:underline"
                            onClick={async () => {
                              const [t] = await pick("transcript");
                              if (t) update(i, { transcript: t });
                            }}>
                            Add transcript (for real names)
                          </button>
                        )}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div className="flex gap-2">
              <Input
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && pasted.trim()) {
                    addPaths([pasted.trim()]); setPasted("");
                  }
                }}
                placeholder="Or paste a file path and press Enter"
                autoComplete="off"
                className="h-8 flex-1 text-xs"
                disabled={busy}
              />
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label htmlFor="import-client">Client</Label>
              <Input
                id="import-client"
                list="import-clients-list"
                value={client}
                onChange={(e) => setClient(e.target.value)}
                placeholder="Type new or pick existing"
                autoComplete="off"
                disabled={busy}
              />
              <datalist id="import-clients-list">
                {existingClients.map((c) => <option key={c} value={c} />)}
              </datalist>
            </div>
            <div className="space-y-2">
              <Label htmlFor="import-project">Project</Label>
              <Input
                id="import-project"
                list="import-projects-list"
                value={project}
                onChange={(e) => setProject(e.target.value)}
                placeholder="Type new or pick existing"
                autoComplete="off"
                disabled={busy}
              />
              <datalist id="import-projects-list">
                {existingProjects.map((p) => <option key={p} value={p} />)}
              </datalist>
            </div>
          </div>
          {count > 1 && (
            <p className="-mt-1 text-[11px] text-muted-foreground">
              Client, project and template apply to every meeting in the list.
            </p>
          )}
          <div className="space-y-2">
            <Label>Summary template</Label>
            <Select value={template} onValueChange={(v) => v && setTemplate(v)} disabled={busy}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {templates.map((t) => (
                  <SelectItem key={t} value={t}>{t}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-start justify-between gap-3 rounded-md border p-3">
            <div className="space-y-0.5">
              <Label htmlFor="import-process">Transcribe and summarize now</Label>
              <p className="text-[11px] text-muted-foreground">
                Runs in the background, the same as after a recording:
                transcript (or the one you added), speakers, summary,
                action items and exports. A video&apos;s slides are added
                first so the summary can use them.
              </p>
            </div>
            <Switch
              id="import-process"
              checked={processNow}
              onCheckedChange={setProcessNow}
              disabled={busy}
            />
          </div>
        </div>
        <DialogFooter className="items-center gap-2">
          {busy && progress && (
            <span className="mr-auto truncate text-xs text-muted-foreground">{progress}</span>
          )}
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={handleImport} disabled={!count || busy}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : null}
            {count > 1 ? `Import ${count} meetings` : "Import"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
