"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api, formatDuration, type InsightsSummary, type InsightsRow,
  type StaleCommitment, type OpenDecision, type OpenActionItem,
} from "@/lib/api";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Clock, TrendingUp, AlertCircle, ChevronRight, Loader2,
} from "lucide-react";

interface Props {
  onOpenSession: (id: string, tab?: string) => void;
  existingClients: string[];
}

// Window presets — relative to "now" so the user doesn't have to
// fiddle with date pickers for the 90% case. Mirrors how every other
// dashboard tool works.
const WINDOWS = [
  { id: "30d", label: "Last 30 days", days: 30 },
  { id: "90d", label: "Last 90 days", days: 90 },
  { id: "ytd", label: "Year to date", days: 0 },
  { id: "all", label: "All time", days: -1 },
] as const;

type WindowId = typeof WINDOWS[number]["id"];

function windowToSince(id: WindowId): string | undefined {
  if (id === "all") return undefined;
  const now = new Date();
  if (id === "ytd") {
    return new Date(now.getFullYear(), 0, 1).toISOString();
  }
  const days = WINDOWS.find((w) => w.id === id)?.days || 30;
  const d = new Date(now.getTime() - days * 24 * 60 * 60 * 1000);
  return d.toISOString();
}

export function InsightsView({ onOpenSession, existingClients }: Props) {
  const [windowId, setWindowId] = useState<WindowId>("90d");
  const [clientFilter, setClientFilter] = useState<string>("__all__");
  const [data, setData] = useState<InsightsSummary | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.getInsights({
      since: windowToSince(windowId),
      client: clientFilter === "__all__" ? undefined : clientFilter,
    })
      .then((res) => { if (!cancelled) setData(res); })
      .catch((e) => {
        if (!cancelled) toast.error(`Insights failed: ${e instanceof Error ? e.message : e}`);
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [windowId, clientFilter]);

  return (
    <div className="space-y-4">
      {/* Controls + window summary */}
      <Card>
        <CardContent className="p-4 flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Window</span>
            <Select value={windowId} onValueChange={(v) => setWindowId(v as WindowId)}>
              <SelectTrigger className="w-[150px] h-8 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                {WINDOWS.map((w) => (
                  <SelectItem key={w.id} value={w.id}>{w.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Client</span>
            <Select value={clientFilter} onValueChange={(v) => setClientFilter(v ?? "__all__")}>
              <SelectTrigger className="w-[200px] h-8 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">All clients</SelectItem>
                {existingClients.map((c) => (
                  <SelectItem key={c} value={c}>{c}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="ml-auto flex items-center gap-4 text-xs text-muted-foreground">
            {loading ? (
              <span className="flex items-center gap-1.5">
                <Loader2 className="h-3 w-3 animate-spin" />
                Loading…
              </span>
            ) : data ? (
              <>
                <span><strong className="text-foreground">{data.window.session_count}</strong> meetings</span>
                <span><strong className="text-foreground">{formatDuration(data.window.total_seconds)}</strong> total</span>
              </>
            ) : null}
          </div>
        </CardContent>
      </Card>

      {/* Section 1: Time allocation */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <Clock className="h-4 w-4 text-primary" />
            Time Allocation
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <TimeBars
            title={clientFilter === "__all__" ? "Hours by Client" : `Projects under ${clientFilter}`}
            rows={clientFilter === "__all__"
              ? (data?.time_allocation.by_client || [])
              : (data?.time_allocation.by_project || [])}
            emptyText="No meetings in this window."
          />
          <TimeBars
            title="By Template"
            rows={data?.time_allocation.by_template || []}
            emptyText="No template usage in this window."
          />
        </CardContent>
      </Card>

      {/* Section 2: Recurring topics */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <TrendingUp className="h-4 w-4 text-primary" />
            Recurring Topics
          </CardTitle>
        </CardHeader>
        <CardContent>
          <TopicCloud topics={data?.topics || {}} clientFilter={clientFilter} />
        </CardContent>
      </Card>

      {/* Section 3: Open loops health */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <AlertCircle className="h-4 w-4 text-primary" />
            Open Loops
            {data && (
              <span className="ml-2 text-xs text-muted-foreground font-normal">
                {data.open_loops.counts.stale_commitments + data.open_loops.counts.un_implemented_decisions + data.open_loops.counts.unchecked_action_items} items need attention
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <StaleCommitmentsBlock
            rows={data?.open_loops.stale_commitments || []}
            onOpenSession={onOpenSession}
          />
          <OpenDecisionsBlock
            rows={data?.open_loops.un_implemented_decisions || []}
            onOpenSession={onOpenSession}
          />
          <UncheckedActionsBlock
            rows={data?.open_loops.unchecked_action_items || []}
            onOpenSession={onOpenSession}
          />
        </CardContent>
      </Card>
    </div>
  );
}

// ── Time bars ────────────────────────────────────────────────────────

function TimeBars({ title, rows, emptyText }: { title: string; rows: InsightsRow[]; emptyText: string }) {
  const max = useMemo(
    () => rows.reduce((m, r) => Math.max(m, r.seconds), 0),
    [rows]);
  if (!rows.length) {
    return (
      <div>
        <div className="text-xs uppercase tracking-wider text-muted-foreground mb-3">{title}</div>
        <div className="text-sm text-muted-foreground italic">{emptyText}</div>
      </div>
    );
  }
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-muted-foreground mb-3">{title}</div>
      <div className="space-y-1.5">
        {rows.map((r) => {
          const pct = max > 0 ? Math.max(2, (r.seconds / max) * 100) : 0;
          return (
            <div key={r.label} className="flex items-center gap-3 text-sm">
              <div className="w-40 truncate" title={r.label}>{r.label}</div>
              <div className="flex-1 h-5 bg-muted rounded relative overflow-hidden">
                <div
                  className="h-full bg-primary/70 rounded"
                  style={{ width: `${pct}%` }}
                />
              </div>
              <div className="w-20 text-right tabular-nums text-muted-foreground">
                {formatDuration(r.seconds)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Topic cloud ──────────────────────────────────────────────────────

function TopicCloud({
  topics, clientFilter,
}: {
  topics: Record<string, { phrase: string; count: number }[]>;
  clientFilter: string;
}) {
  const entries = useMemo(() => {
    const ents = Object.entries(topics);
    // When scoped to one client, only show that client's bucket.
    if (clientFilter !== "__all__") {
      return ents.filter(([c]) => c === clientFilter);
    }
    // All-clients view: sort by total count desc, cap at top 6 clients
    // so the page doesn't become a wall of bubbles.
    return ents
      .map(([c, rows]) => ({ client: c, rows, total: rows.reduce((a, r) => a + r.count, 0) }))
      .sort((a, b) => b.total - a.total)
      .slice(0, 6)
      .map(({ client, rows }) => [client, rows] as [string, { phrase: string; count: number }[]]);
  }, [topics, clientFilter]);
  if (!entries.length) {
    return <div className="text-sm text-muted-foreground italic">Not enough meeting summaries yet for topic clustering. Process a few sessions first.</div>;
  }
  return (
    <div className="space-y-4">
      {entries.map(([clientName, rows]) => {
        const max = rows.reduce((m, r) => Math.max(m, r.count), 0);
        return (
          <div key={clientName}>
            <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">{clientName}</div>
            <div className="flex flex-wrap gap-2">
              {rows.map((r) => {
                // Scale font size by recency-weighted count, but clamp
                // so the cloud doesn't look comically lopsided.
                const ratio = max > 0 ? r.count / max : 0;
                const size = 12 + Math.round(ratio * 4);
                return (
                  <span
                    key={r.phrase}
                    className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-muted text-foreground/90"
                    style={{ fontSize: `${size}px` }}
                    title={`${r.count} meetings`}
                  >
                    {r.phrase}
                    <span className="text-[10px] text-muted-foreground tabular-nums">×{r.count}</span>
                  </span>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Open-loop blocks ─────────────────────────────────────────────────

function SectionHeader({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center gap-2 mb-2">
      <div className="text-xs uppercase tracking-wider text-muted-foreground">{label}</div>
      <Badge variant={count > 0 ? "secondary" : "outline"} className="text-[10px]">
        {count}
      </Badge>
    </div>
  );
}

function StaleCommitmentsBlock({
  rows, onOpenSession,
}: { rows: StaleCommitment[]; onOpenSession: (id: string, tab?: string) => void }) {
  return (
    <div>
      <SectionHeader label="Stale commitments (overdue or >30 days)" count={rows.length} />
      {!rows.length ? (
        <div className="text-sm text-muted-foreground italic">All commitments are fresh. Nice.</div>
      ) : (
        <div className="divide-y rounded border">
          {rows.slice(0, 20).map((c) => (
            <button
              key={c.commitment_id}
              type="button"
              onClick={() => onOpenSession(c.session_id, "commitments")}
              className="w-full text-left p-3 hover:bg-muted/50 flex items-start gap-3"
            >
              <div className="flex-1 min-w-0">
                <div className="text-sm">
                  <span className="font-medium">{c.owner || "Unknown"}</span>
                  <span className="text-muted-foreground"> · </span>
                  <span className="text-muted-foreground">{c.description}</span>
                </div>
                <div className="text-[11px] text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5">
                  <span>{c.session_client || "—"}</span>
                  <span>· {c.session_display_name}</span>
                  {c.due_date_iso && (
                    <Badge variant={c.is_overdue ? "destructive" : "outline"} className="text-[10px]">
                      Due {c.due_date_iso}{c.is_overdue ? " · overdue" : ""}
                    </Badge>
                  )}
                </div>
              </div>
              <ChevronRight className="h-4 w-4 text-muted-foreground self-center shrink-0" />
            </button>
          ))}
          {rows.length > 20 && (
            <div className="p-2 text-center text-xs text-muted-foreground">
              +{rows.length - 20} more — open Commitments view for the full list
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function OpenDecisionsBlock({
  rows, onOpenSession,
}: { rows: OpenDecision[]; onOpenSession: (id: string, tab?: string) => void }) {
  return (
    <div>
      <SectionHeader label="Decisions un-implemented (>30 days)" count={rows.length} />
      {!rows.length ? (
        <div className="text-sm text-muted-foreground italic">No old decisions still flagged active.</div>
      ) : (
        <div className="divide-y rounded border">
          {rows.slice(0, 20).map((d) => (
            <button
              key={`${d.session_id}-${d.item_hash}`}
              type="button"
              onClick={() => onOpenSession(d.session_id, "decisions")}
              className="w-full text-left p-3 hover:bg-muted/50 flex items-start gap-3"
            >
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">{d.title}</div>
                {d.decided && (
                  <div className="text-xs text-muted-foreground mt-0.5 line-clamp-1">{d.decided}</div>
                )}
                <div className="text-[11px] text-muted-foreground mt-1">
                  {d.session_client || "—"} · {d.session_display_name}
                </div>
              </div>
              <ChevronRight className="h-4 w-4 text-muted-foreground self-center shrink-0" />
            </button>
          ))}
          {rows.length > 20 && (
            <div className="p-2 text-center text-xs text-muted-foreground">
              +{rows.length - 20} more
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function UncheckedActionsBlock({
  rows, onOpenSession,
}: { rows: OpenActionItem[]; onOpenSession: (id: string, tab?: string) => void }) {
  return (
    <div>
      <SectionHeader label="Unchecked follow-ups (>30 days)" count={rows.length} />
      {!rows.length ? (
        <div className="text-sm text-muted-foreground italic">All older follow-ups are checked off.</div>
      ) : (
        <div className="divide-y rounded border">
          {rows.slice(0, 20).map((a) => (
            <button
              key={`${a.session_id}-${a.item_hash}`}
              type="button"
              onClick={() => onOpenSession(a.session_id, "follow_ups")}
              className="w-full text-left p-3 hover:bg-muted/50 flex items-start gap-3"
            >
              <div className="flex-1 min-w-0">
                <div className="text-sm">
                  {a.owner && <span className="font-medium">{a.owner}: </span>}
                  <span className="text-muted-foreground">{a.description}</span>
                </div>
                <div className="text-[11px] text-muted-foreground mt-1 flex flex-wrap items-center gap-x-3">
                  <span>{a.session_client || "—"}</span>
                  <span>· {a.session_display_name}</span>
                  {a.due && (
                    <Badge variant="outline" className="text-[10px]">Due {a.due}</Badge>
                  )}
                </div>
              </div>
              <ChevronRight className="h-4 w-4 text-muted-foreground self-center shrink-0" />
            </button>
          ))}
          {rows.length > 20 && (
            <div className="p-2 text-center text-xs text-muted-foreground">
              +{rows.length - 20} more
            </div>
          )}
        </div>
      )}
    </div>
  );
}
