"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api, computeItemHash, type DecisionStatus, type ItemStatusDoc,
  type SessionSummary,
} from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";

interface Decision {
  itemHash: string;
  status: DecisionStatus;
  title: string;
  decided: string;
  rationale: string;
  alternatives: string;
  owner: string;
  impact: string;
  client: string;
  meeting: string;
  session_id: string;
  session_date: string;
}

const BLOCK_RE = /##\s*(?:Decision:?\s*)?(?<title>.+?)\n(?<body>(?:[-*].*(?:\n|$))+)/gi;
const BULLET_RE = /^[-*]\s*\*\*(?<key>[^:*]+)(?::|\*\*:)\s*\*?\*?\s*(?<value>.*)$/gm;

interface ParsedRaw {
  title: string;
  decided: string;
  rationale: string;
  alternatives: string;
  owner: string;
  impact: string;
  hashSource: string;
}

function parseRaw(text: string): ParsedRaw[] {
  if (!text || text.toLowerCase().slice(0, 80).includes("no decisions")) return [];
  const out: ParsedRaw[] = [];
  for (const m of text.matchAll(BLOCK_RE)) {
    const title = (m.groups?.title || "").trim().replace(/[*#:]/g, "");
    const body = m.groups?.body || "";
    const fields: Record<string, string> = {};
    for (const bm of body.matchAll(BULLET_RE)) {
      fields[(bm.groups?.key || "").trim().toLowerCase()] = (bm.groups?.value || "").trim();
    }
    // Hash on (title + decided): title alone collides on
    // generic-sounding "Architecture decision" headers, while including
    // the decided text gives a stable identity even if the LLM rewords
    // the rationale slightly on re-extraction.
    const hashSource = `${title}|${fields["decided"] || ""}`;
    out.push({
      title,
      decided: fields["decided"] || "",
      rationale: fields["rationale"] || "",
      alternatives: fields["alternatives considered"] || "",
      owner: fields["owner"] || "",
      impact: fields["impact"] || "",
      hashSource,
    });
  }
  return out;
}

const STATUS_LABEL: Record<DecisionStatus, string> = {
  active: "Active",
  implemented: "Implemented",
  superseded: "Superseded",
};

const STATUS_BADGE: Record<DecisionStatus, "default" | "secondary" | "outline"> = {
  active: "default",
  implemented: "secondary",
  superseded: "outline",
};

interface Props {
  sessions: SessionSummary[];
  onOpenSession: (id: string, tab?: string) => void;
}

export function DecisionsView({ sessions, onOpenSession }: Props) {
  const [statusFilter, setStatusFilter] = useState<"All" | DecisionStatus>("All");
  const [clientFilter, setClientFilter] = useState("All");
  const [search, setSearch] = useState("");
  const [selectedHash, setSelectedHash] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, ItemStatusDoc>>({});

  useEffect(() => {
    let cancelled = false;
    api.listAllItemStatus()
      .then((res) => { if (!cancelled) setOverrides(res.sessions || {}); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const [allDecisions, setAllDecisions] = useState<Decision[]>([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const out: Decision[] = [];
      for (const s of sessions) {
        if (!s.decisions) continue;
        const meta = {
          client: s.client,
          meeting: s.display_name,
          session_id: s.session_id,
          session_date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
        };
        const raws = parseRaw(s.decisions);
        for (const r of raws) {
          const h = await computeItemHash(r.hashSource);
          out.push({
            itemHash: h,
            status: "active",
            title: r.title,
            decided: r.decided,
            rationale: r.rationale,
            alternatives: r.alternatives,
            owner: r.owner,
            impact: r.impact,
            ...meta,
          });
        }
      }
      if (!cancelled) setAllDecisions(out);
    })();
    return () => { cancelled = true; };
  }, [sessions]);

  // Apply overrides on top.
  const decisions = useMemo(() => {
    return allDecisions.map((d) => {
      const ov = overrides[d.session_id]?.decisions?.[d.itemHash];
      if (ov?.status) return { ...d, status: ov.status };
      return d;
    });
  }, [allDecisions, overrides]);

  const clients = useMemo(
    () => ["All", ...Array.from(new Set(decisions.map((d) => d.client).filter(Boolean))).sort()],
    [decisions]
  );

  const filtered = decisions.filter((d) => {
    if (statusFilter !== "All" && d.status !== statusFilter) return false;
    if (clientFilter !== "All" && d.client !== clientFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      const blob = [d.title, d.decided, d.rationale, d.alternatives, d.owner, d.impact, d.meeting].join(" ").toLowerCase();
      if (!blob.includes(q)) return false;
    }
    return true;
  });

  const selected = filtered.find((d) => d.itemHash === selectedHash) ||
    decisions.find((d) => d.itemHash === selectedHash) || null;

  const setStatus = async (decision: Decision, next: DecisionStatus) => {
    setOverrides((prev) => {
      const sess = prev[decision.session_id] ?? { follow_ups: {}, decisions: {} };
      return {
        ...prev,
        [decision.session_id]: {
          follow_ups: sess.follow_ups || {},
          decisions: {
            ...sess.decisions,
            [decision.itemHash]: {
              status: next,
              updated_at: new Date().toISOString(),
            },
          },
        },
      };
    });
    try {
      await api.setDecisionStatus(decision.session_id, decision.itemHash, next);
    } catch (e) {
      // Roll back on failure.
      setOverrides((prev) => {
        const sess = prev[decision.session_id];
        if (!sess) return prev;
        const dc = { ...sess.decisions };
        delete dc[decision.itemHash];
        return {
          ...prev,
          [decision.session_id]: { ...sess, decisions: dc },
        };
      });
      console.warn("setDecisionStatus failed", e);
    }
  };

  const counts = useMemo(() => {
    const c = { active: 0, implemented: 0, superseded: 0 };
    for (const d of decisions) c[d.status]++;
    return c;
  }, [decisions]);

  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <div className="flex flex-wrap gap-3">
        <Select value={statusFilter} onValueChange={(v) => v && setStatusFilter(v as typeof statusFilter)}>
          <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="All">All statuses</SelectItem>
            <SelectItem value="active">Active ({counts.active})</SelectItem>
            <SelectItem value="implemented">Implemented ({counts.implemented})</SelectItem>
            <SelectItem value="superseded">Superseded ({counts.superseded})</SelectItem>
          </SelectContent>
        </Select>
        <Select value={clientFilter} onValueChange={(v) => v && setClientFilter(v)}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            {clients.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
          </SelectContent>
        </Select>
        <Input placeholder="Search decisions..." value={search} onChange={(e) => setSearch(e.target.value)} className="flex-1 max-w-md" />
      </div>

      <p className="text-xs text-muted-foreground">
        {filtered.length} shown of {decisions.length} total
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardContent className="p-0">
            {filtered.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">
                No decisions match. Try a different filter.
              </p>
            ) : (
              filtered.map((d) => (
                <button
                  key={`${d.session_id}|${d.itemHash}`}
                  onClick={() => setSelectedHash(d.itemHash)}
                  className={`w-full text-left flex items-start gap-3 border-b last:border-b-0 p-3 hover:bg-muted/40 transition-colors ${
                    selectedHash === d.itemHash ? "bg-accent" : ""
                  }`}
                >
                  <span className="text-primary mt-0.5">🎯</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{d.title}</div>
                    <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-2">
                      <Badge
                        variant={STATUS_BADGE[d.status]}
                        className="text-[10px]"
                      >
                        {STATUS_LABEL[d.status]}
                      </Badge>
                      {d.client && <Badge variant="outline" className="text-[10px]">{d.client}</Badge>}
                      <span className="truncate">{d.meeting}</span>
                    </div>
                  </div>
                </button>
              ))
            )}
          </CardContent>
        </Card>

        {/* Sticky detail panel — stays visible as the user scrolls the
            list. Without this, clicking a row halfway down the list
            leaves the detail off-screen above and the user has to
            scroll back up to see anything they just selected. Internal
            overflow handles very long details (e.g. a decision with a
            lot of rationale) without spilling past the viewport. */}
        <Card className="md:sticky md:top-0 md:self-start md:max-h-[calc(100vh-8rem)] md:overflow-y-auto">
          <CardContent className="p-6">
            {selected ? (
              <div className="space-y-4">
                <div className="flex items-start justify-between gap-3">
                  <h2 className="text-base font-semibold flex-1 min-w-0">{selected.title}</h2>
                  <Badge variant={STATUS_BADGE[selected.status]}>
                    {STATUS_LABEL[selected.status]}
                  </Badge>
                </div>
                <div>
                  <div className="text-[10px] font-medium uppercase tracking-wider text-primary mb-1">Status</div>
                  <Select
                    value={selected.status}
                    onValueChange={(v) => v && setStatus(selected, v as DecisionStatus)}
                  >
                    <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="active">Active</SelectItem>
                      <SelectItem value="implemented">Implemented</SelectItem>
                      <SelectItem value="superseded">Superseded</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                {selected.decided && (
                  <Field label="Decided">{selected.decided}</Field>
                )}
                {selected.rationale && (
                  <Field label="Rationale">{selected.rationale}</Field>
                )}
                {selected.alternatives && (
                  <Field label="Alternatives considered">{selected.alternatives}</Field>
                )}
                {selected.owner && <Field label="Owner">{selected.owner}</Field>}
                {selected.impact && <Field label="Impact">{selected.impact}</Field>}
                <div className="pt-3 border-t flex items-center justify-between">
                  <div className="text-xs text-muted-foreground">
                    From <span className="font-medium text-foreground">{selected.meeting}</span> ({selected.session_date})
                  </div>
                  <button
                    onClick={() => onOpenSession(selected.session_id, "decisions")}
                    className="text-xs text-primary hover:underline font-medium"
                  >
                    Open meeting →
                  </button>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground text-center py-12">
                Select a decision to see details.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-medium uppercase tracking-wider text-primary mb-1">{label}</div>
      <div className="text-sm break-words">{children}</div>
    </div>
  );
}
