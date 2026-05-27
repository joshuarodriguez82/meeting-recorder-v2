"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Commitment, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Loader2, RotateCcw, Building2, Clock, AlertTriangle,
  Quote, MessageCircle,
} from "lucide-react";

// Cross-meeting commitment tracker.
//
// Each row = a single thing someone promised to deliver during a
// processed meeting. The backend extracts these via a Claude pass over
// the transcript right after /process completes; the user lands here
// to triage them — mark delivered, dismiss false positives, or restore
// something they marked too aggressively.
//
// The default landing view shows ACTIVE commitments — awaiting +
// overdue — sorted overdue-first. That's the "what should I be
// chasing right now" lens. Status / owner / side filters narrow
// further; client/project filters scope to a single account.
//
// We intentionally don't paginate: even a heavy SA caps out around
// a few hundred active commitments. If that ever becomes wrong we
// add infinite scroll, but for now a single scrollable card is the
// right shape.

interface Props {
  // Open the source session in the detail dialog so the user can
  // verify the verbatim quote against the audio + full transcript.
  onOpenSession: (id: string) => void;
}

type StatusFilter = "active" | "all" | "delivered" | "dismissed";

export function CommitmentsView({ onOpenSession }: Props) {
  const [items, setItems] = useState<Commitment[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Filters. status defaults to "active" because that's the single
  // most-useful starting view; clearing or switching is a one-click
  // affordance below.
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [client, setClient] = useState<string>("");
  const [side, setSide] = useState<"" | "internal" | "customer">("");
  const [search, setSearch] = useState("");

  // Pull the unique client list from sessions for the scope dropdown.
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  useEffect(() => {
    api.listSessions().then(setSessions).catch(() => setSessions([]));
  }, []);
  const clients = useMemo(() => {
    const set = new Set<string>();
    for (const s of sessions) if (s.client) set.add(s.client);
    return Array.from(set).sort();
  }, [sessions]);

  const reload = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listCommitments({
        client: client || undefined,
        side: side || undefined,
        status: statusFilter === "all" ? undefined : statusFilter,
      });
      setItems(res.commitments || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { reload(); }, [statusFilter, client, side]);

  // Commitments are mined in the background after a call auto-processes,
  // so re-fetch when the app window regains focus — otherwise the list
  // sits stale until a filter changes. Ref keeps the handler bound to
  // the latest filters without re-registering the listener each render.
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    let last = 0;
    const refresh = () => {
      const now = Date.now();
      if (now - last < 10_000) return;
      last = now;
      reloadRef.current();
      api.listSessions().then(setSessions).catch(() => {});
    };
    const onVis = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  // Free-text filter applied client-side over the already-fetched
  // list — cheap (max few hundred rows) and avoids backend round-trip
  // on every keystroke.
  const filtered = useMemo(() => {
    if (!items) return null;
    const q = search.trim().toLowerCase();
    if (!q) return items;
    return items.filter((c) =>
      c.owner.toLowerCase().includes(q)
      || c.description.toLowerCase().includes(q)
      || c.quote.toLowerCase().includes(q)
      || (c.session_display_name || "").toLowerCase().includes(q)
    );
  }, [items, search]);

  // Status update handler. Optimistic on the local list — flip the
  // status, render, then send. On failure we toast + reload to
  // resync. Keeps the UI feeling snappy without hand-rolling
  // mutation libraries.
  const updateStatus = async (
    id: string,
    status: "awaiting" | "delivered" | "dismissed",
  ) => {
    const prev = items;
    if (prev) {
      setItems(prev.map((c) =>
        c.commitment_id === id
          ? { ...c, status, resolved_at: status === "awaiting" ? "" : new Date().toISOString() }
          : c
      ));
    }
    try {
      await api.updateCommitment(id, status);
      // Re-pull so any synthetic changes (e.g. a now-delivered item
      // dropping out of the active filter) settle correctly.
      reload();
    } catch (e) {
      toast.error(`Update failed: ${e instanceof Error ? e.message : e}`);
      if (prev) setItems(prev);
    }
  };

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = (filtered || []).find((c) => c.commitment_id === selectedId)
    || (items || []).find((c) => c.commitment_id === selectedId)
    || null;

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex items-center gap-3 flex-wrap text-xs">
        <Select
          value={statusFilter}
          onValueChange={(v) => v && setStatusFilter(v as StatusFilter)}
        >
          <SelectTrigger className="h-8 w-40 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="active">Active (awaiting + overdue)</SelectItem>
            <SelectItem value="delivered">Delivered</SelectItem>
            <SelectItem value="dismissed">Dismissed</SelectItem>
            <SelectItem value="all">All</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={client || "__all__"}
          onValueChange={(v) => setClient(!v || v === "__all__" ? "" : v)}
        >
          <SelectTrigger className="h-8 w-44 text-xs">
            <SelectValue placeholder="All clients" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">All clients</SelectItem>
            {clients.map((c) => (
              <SelectItem key={c} value={c}>{c}</SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={side || "__any__"}
          onValueChange={(v) => setSide(!v || v === "__any__" ? "" : v as "internal" | "customer")}
        >
          <SelectTrigger className="h-8 w-40 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__any__">Any side</SelectItem>
            <SelectItem value="customer">Customer-side only</SelectItem>
            <SelectItem value="internal">Internal-side only</SelectItem>
          </SelectContent>
        </Select>

        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter by owner / text…"
          className="h-8 flex-1 min-w-40 text-xs"
        />

        <Button
          variant="ghost"
          size="sm"
          onClick={reload}
          disabled={loading}
          className="ml-auto h-8"
        >
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
        </Button>
      </div>

      {filtered && (
        <p className="text-xs text-muted-foreground">
          {filtered.length} {statusFilter === "active" ? "active" : statusFilter} commitment{filtered.length === 1 ? "" : "s"}
          {client ? ` · ${client}` : ""}
        </p>
      )}

      {/* Split-pane: list of rows on the left, full detail + status
          control on the right. Matches the Decisions and Follow-ups
          tabs so the three "things to act on" views feel like one
          application instead of three. */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardContent className="p-0">
            {loading && !items && (
              <div className="p-8 text-center text-sm text-muted-foreground flex items-center gap-2 justify-center">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading commitments…
              </div>
            )}
            {error && (
              <p className="p-8 text-center text-sm text-destructive italic">{error}</p>
            )}
            {filtered && filtered.length === 0 && !loading && !error && (
              <p className="p-8 text-center text-sm text-muted-foreground">
                {statusFilter === "active"
                  ? "No active commitments. The tracker auto-mines commitments after every processed meeting; once you've recorded a few you'll see them appear here."
                  : "Nothing matches the current filter."}
              </p>
            )}
            {filtered && filtered.length > 0 && (
              <div className="divide-y">
                {filtered.map((c) => (
                  <button
                    key={c.commitment_id}
                    onClick={() => setSelectedId(c.commitment_id)}
                    className={`w-full text-left p-3 hover:bg-muted/40 transition-colors ${
                      selectedId === c.commitment_id ? "bg-accent" : ""
                    }`}
                  >
                    <div className="flex items-start gap-3">
                      <StatusBadge
                        status={c.status}
                        isOverdue={c.is_overdue && c.status === "awaiting"}
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium truncate">
                          {c.owner} — {c.description}
                        </div>
                        <div className="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-2 flex-wrap">
                          {c.session_client && (
                            <span className="inline-flex items-center gap-1">
                              <Building2 className="h-3 w-3" />
                              {c.session_client}
                            </span>
                          )}
                          <span className="truncate">{c.session_display_name || "Source meeting"}</span>
                        </div>
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            {selected ? (
              <CommitmentDetail
                commitment={selected}
                onMarkDelivered={() => updateStatus(selected.commitment_id, "delivered")}
                onDismiss={() => updateStatus(selected.commitment_id, "dismissed")}
                onReopen={() => updateStatus(selected.commitment_id, "awaiting")}
                onOpenSession={onOpenSession}
              />
            ) : (
              <p className="text-sm text-muted-foreground text-center py-12">
                Select a commitment to see the verbatim quote, due date, and change its status.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function CommitmentDetail({
  commitment, onMarkDelivered, onDismiss, onReopen, onOpenSession,
}: {
  commitment: Commitment;
  onMarkDelivered: () => void;
  onDismiss: () => void;
  onReopen: () => void;
  onOpenSession: (id: string) => void;
}) {
  const c = commitment;
  const due = c.due_date_iso ? new Date(c.due_date_iso) : null;
  const isOverdue = c.is_overdue && c.status === "awaiting";

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <h2 className="text-base font-semibold flex-1 min-w-0">{c.description}</h2>
        <StatusBadge status={c.status} isOverdue={isOverdue} />
      </div>

      <div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-primary mb-1">
          Status
        </div>
        <Select
          value={c.status}
          onValueChange={(v) => {
            if (!v || v === c.status) return;
            if (v === "delivered") onMarkDelivered();
            else if (v === "dismissed") onDismiss();
            else if (v === "awaiting") onReopen();
          }}
        >
          <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="awaiting">Awaiting</SelectItem>
            <SelectItem value="delivered">Delivered</SelectItem>
            <SelectItem value="dismissed">Dismissed</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-1">
        <div className="text-[10px] font-medium uppercase tracking-wider text-primary">Owner</div>
        <div className="text-sm flex items-center gap-2 flex-wrap">
          <span>{c.owner}</span>
          <SideBadge side={c.side} />
        </div>
      </div>

      {due && (
        <div className="space-y-1">
          <div className="text-[10px] font-medium uppercase tracking-wider text-primary">Due</div>
          <div className={`text-sm inline-flex items-center gap-1 ${isOverdue ? "text-destructive font-medium" : ""}`}>
            <Clock className="h-3 w-3" />
            {due.toLocaleDateString()}
            {isOverdue ? ` (${formatRelative(due)})` : ""}
          </div>
        </div>
      )}

      {c.quote && (
        <div className="space-y-1">
          <div className="text-[10px] font-medium uppercase tracking-wider text-primary">Verbatim</div>
          <blockquote className="text-xs italic text-muted-foreground border-l-2 border-muted pl-2">
            <Quote className="inline h-2.5 w-2.5 mr-0.5 -mt-0.5" />
            {c.quote}
          </blockquote>
        </div>
      )}

      <div className="pt-3 border-t flex items-center justify-between text-xs text-muted-foreground flex-wrap gap-2">
        <span>
          From <span className="font-medium text-foreground">{c.session_display_name || "source meeting"}</span>
          {c.session_client ? ` · ${c.session_client}` : ""}
          {c.session_project ? ` / ${c.session_project}` : ""}
        </span>
        <button
          onClick={() => onOpenSession(c.session_id)}
          className="text-xs text-primary hover:underline font-medium inline-flex items-center gap-1"
        >
          <MessageCircle className="h-3 w-3" />
          Open meeting
        </button>
      </div>

      {c.resolved_at && c.status !== "awaiting" && (
        <p className="text-[11px] italic text-muted-foreground">
          {c.status} {formatRelative(new Date(c.resolved_at))}
        </p>
      )}
    </div>
  );
}

function StatusBadge({ status, isOverdue }: { status: string; isOverdue: boolean }) {
  const klass =
    isOverdue
      ? "bg-destructive/15 text-destructive"
      : status === "awaiting"
        ? "bg-amber-500/15 text-amber-700 dark:text-amber-400"
        : status === "delivered"
          ? "bg-primary/15 text-primary"
          : "bg-muted text-muted-foreground";
  const label = isOverdue ? "OVERDUE" : status.toUpperCase();
  return (
    <span
      className={`shrink-0 mt-0.5 inline-flex items-center justify-center rounded-md px-2 py-0.5 text-[9px] font-bold tracking-wider ${klass}`}
      style={{ minWidth: 64 }}
    >
      {isOverdue && <AlertTriangle className="h-2.5 w-2.5 mr-0.5" />}
      {label}
    </span>
  );
}

function SideBadge({ side }: { side: string }) {
  if (side === "unknown") return null;
  return (
    <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
      {side}
    </span>
  );
}

function formatRelative(d: Date): string {
  const now = Date.now();
  const ms = now - d.getTime();
  const sec = Math.abs(Math.floor(ms / 1000));
  const future = ms < 0;
  const min = Math.floor(sec / 60);
  const hr = Math.floor(min / 60);
  const day = Math.floor(hr / 24);
  const wk = Math.floor(day / 7);
  let label = "just now";
  if (sec < 60) label = "just now";
  else if (min < 60) label = `${min}m`;
  else if (hr < 24) label = `${hr}h`;
  else if (day < 14) label = `${day}d`;
  else label = `${wk}w`;
  return future ? `in ${label}` : `${label} ago`;
}
