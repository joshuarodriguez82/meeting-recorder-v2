"use client";

import { useMemo, useState } from "react";
import { type SessionSummary } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";

interface Decision {
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

function parseDecisions(text: string, meta: Omit<Decision, "title" | "decided" | "rationale" | "alternatives" | "owner" | "impact">): Decision[] {
  if (!text || text.toLowerCase().slice(0, 80).includes("no decisions")) return [];
  const out: Decision[] = [];
  for (const m of text.matchAll(BLOCK_RE)) {
    const title = (m.groups?.title || "").trim().replace(/[*#:]/g, "");
    const body = m.groups?.body || "";
    const fields: Record<string, string> = {};
    for (const bm of body.matchAll(BULLET_RE)) {
      fields[(bm.groups?.key || "").trim().toLowerCase()] = (bm.groups?.value || "").trim();
    }
    out.push({
      title,
      decided: fields["decided"] || "",
      rationale: fields["rationale"] || "",
      alternatives: fields["alternatives considered"] || "",
      owner: fields["owner"] || "",
      impact: fields["impact"] || "",
      ...meta,
    });
  }
  return out;
}

interface Props {
  sessions: SessionSummary[];
  onOpenSession: (id: string, tab?: string) => void;
}

export function DecisionsView({ sessions, onOpenSession }: Props) {
  const [clientFilter, setClientFilter] = useState("All");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Decision | null>(null);

  const allDecisions = useMemo(() => {
    const out: Decision[] = [];
    for (const s of sessions) {
      if (!s.decisions) continue;
      out.push(...parseDecisions(s.decisions, {
        client: s.client,
        meeting: s.display_name,
        session_id: s.session_id,
        session_date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
      }));
    }
    return out;
  }, [sessions]);

  const clients = useMemo(
    () => ["All", ...Array.from(new Set(allDecisions.map((d) => d.client).filter(Boolean))).sort()],
    [allDecisions]
  );

  const filtered = allDecisions.filter((d) => {
    if (clientFilter !== "All" && d.client !== clientFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      const blob = [d.title, d.decided, d.rationale, d.alternatives, d.owner, d.impact, d.meeting].join(" ").toLowerCase();
      if (!blob.includes(q)) return false;
    }
    return true;
  });

  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <div className="flex gap-3">
        <Select value={clientFilter} onValueChange={(v) => v && setClientFilter(v)}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            {clients.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
          </SelectContent>
        </Select>
        <Input placeholder="Search decisions..." value={search} onChange={(e) => setSearch(e.target.value)} className="flex-1 max-w-md" />
      </div>

      <p className="text-xs text-muted-foreground">
        {filtered.length} shown of {allDecisions.length} total
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardContent className="p-0">
            {filtered.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">
                No decisions yet. Extract them from a processed session.
              </p>
            ) : (
              filtered.map((d, idx) => (
                <button
                  key={idx}
                  onClick={() => setSelected(d)}
                  className={`w-full text-left flex items-start gap-3 border-b last:border-b-0 p-3 hover:bg-muted/40 transition-colors ${
                    selected === d ? "bg-accent" : ""
                  }`}
                >
                  <span className="text-primary mt-0.5">🎯</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{d.title}</div>
                    <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-2">
                      {d.client && <Badge variant="outline" className="text-[10px]">{d.client}</Badge>}
                      <span className="truncate">{d.meeting}</span>
                    </div>
                  </div>
                </button>
              ))
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            {selected ? (
              <div className="space-y-4">
                <h2 className="text-base font-semibold">{selected.title}</h2>
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
    <div>
      <div className="text-[10px] font-medium uppercase tracking-wider text-primary mb-1">{label}</div>
      <div className="text-sm">{children}</div>
    </div>
  );
}
