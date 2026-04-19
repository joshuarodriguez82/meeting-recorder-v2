"use client";

import { useMemo, useState } from "react";
import { type SessionSummary } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";

interface ActionItem {
  done: boolean;
  owner: string;
  description: string;
  due: string;
  client: string;
  meeting: string;
  session_id: string;
  session_date: string;
}

const LINE_RE = /^\s*-\s*\[(?<status>[ xX])\]\s*(?<rest>.+)$/gm;
const OWNER_RE = /\*\*(?<owner>[^*]+)\*\*\s*:\s*(?<desc>.+)/;
const DUE_RE = /\(Due:\s*(?<due>[^)]+)\)/i;

function parseActionItems(text: string, meta: Omit<ActionItem, "done" | "owner" | "description" | "due">): ActionItem[] {
  if (!text) return [];
  const items: ActionItem[] = [];
  for (const m of text.matchAll(LINE_RE)) {
    const status = (m.groups?.status || "").trim().toLowerCase();
    let rest = (m.groups?.rest || "").trim();
    let owner = "", desc = rest;
    const ownerMatch = rest.match(OWNER_RE);
    if (ownerMatch?.groups) {
      owner = ownerMatch.groups.owner.trim().replace(/^\[|\]$/g, "");
      desc = ownerMatch.groups.desc.trim();
    }
    let due = "";
    const dueMatch = desc.match(DUE_RE);
    if (dueMatch?.groups) {
      due = dueMatch.groups.due.trim();
      desc = desc.replace(DUE_RE, "").trim();
    }
    items.push({
      done: status === "x",
      owner,
      description: desc,
      due,
      ...meta,
    });
  }
  return items;
}

interface Props {
  sessions: SessionSummary[];
  onOpenSession: (id: string, tab?: string) => void;
}

export function FollowUpsView({ sessions, onOpenSession }: Props) {
  const [statusFilter, setStatusFilter] = useState("Open");
  const [clientFilter, setClientFilter] = useState("All");
  const [ownerFilter, setOwnerFilter] = useState("All");
  const [search, setSearch] = useState("");

  const allItems = useMemo(() => {
    const out: ActionItem[] = [];
    for (const s of sessions) {
      if (!s.action_items) continue;
      const parsed = parseActionItems(s.action_items, {
        client: s.client,
        meeting: s.display_name,
        session_id: s.session_id,
        session_date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
      });
      out.push(...parsed);
    }
    return out;
  }, [sessions]);

  const clients = useMemo(
    () => ["All", ...Array.from(new Set(allItems.map((i) => i.client).filter(Boolean))).sort()],
    [allItems]
  );
  const owners = useMemo(
    () => ["All", ...Array.from(new Set(allItems.map((i) => i.owner).filter(Boolean))).sort()],
    [allItems]
  );

  const filtered = allItems.filter((i) => {
    if (statusFilter === "Open" && i.done) return false;
    if (statusFilter === "Done" && !i.done) return false;
    if (clientFilter !== "All" && i.client !== clientFilter) return false;
    if (ownerFilter !== "All" && i.owner !== ownerFilter) return false;
    if (search) {
      const q = search.toLowerCase();
      const blob = [i.description, i.owner, i.meeting, i.client, i.due].join(" ").toLowerCase();
      if (!blob.includes(q)) return false;
    }
    return true;
  });

  const openCount = allItems.filter((i) => !i.done).length;

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <div className="flex flex-wrap gap-3">
        <Select value={statusFilter} onValueChange={(v) => v && setStatusFilter(v)}>
          <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="Open">Open</SelectItem>
            <SelectItem value="Done">Done</SelectItem>
            <SelectItem value="All">All</SelectItem>
          </SelectContent>
        </Select>
        <Select value={clientFilter} onValueChange={(v) => v && setClientFilter(v)}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            {clients.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
          </SelectContent>
        </Select>
        <Select value={ownerFilter} onValueChange={(v) => v && setOwnerFilter(v)}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            {owners.map((o) => <SelectItem key={o} value={o}>{o}</SelectItem>)}
          </SelectContent>
        </Select>
        <Input
          placeholder="Search..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 max-w-md"
        />
      </div>

      <p className="text-xs text-muted-foreground">
        {filtered.length} shown · {openCount} open of {allItems.length} total
      </p>

      <Card>
        <CardContent className="p-0">
          {filtered.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              No action items yet. Extract them from a processed session.
            </p>
          ) : (
            filtered.map((i, idx) => (
              <button
                key={idx}
                onClick={() => onOpenSession(i.session_id, "actions")}
                className="w-full text-left flex items-start gap-3 border-b last:border-b-0 p-4 hover:bg-muted/40 transition-colors"
              >
                <span className={`mt-0.5 text-lg ${i.done ? "text-green-600" : "text-muted-foreground"}`}>
                  {i.done ? "✓" : "○"}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm break-words">
                    {i.owner && <span className="font-medium">[{i.owner}] </span>}
                    {i.description}
                    {i.due && <span className="text-muted-foreground"> (Due: {i.due})</span>}
                  </div>
                  <div className="text-xs text-muted-foreground mt-1 flex gap-2 items-center flex-wrap">
                    {i.client && <Badge variant="outline" className="text-[10px]">{i.client}</Badge>}
                    <span className="text-primary">{i.meeting}</span>
                    <span>·</span>
                    <span>{i.session_date}</span>
                  </div>
                </div>
              </button>
            ))
          )}
        </CardContent>
      </Card>
    </div>
  );
}
