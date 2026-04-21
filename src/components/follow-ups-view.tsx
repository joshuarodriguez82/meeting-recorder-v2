"use client";

import { useMemo, useState } from "react";
import { type SessionSummary } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";

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
            <FollowUpGroups items={filtered} onOpenSession={onOpenSession} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * Collapse follow-ups into one card per (meeting, owner). Five tasks for
 * the same person in one meeting = one expandable card, not five separate
 * rows — so the view still tells you at-a-glance who owes what, without
 * drowning the screen in duplicates.
 */
function FollowUpGroups({
  items, onOpenSession,
}: {
  items: ActionItem[];
  onOpenSession: (id: string, tab?: string) => void;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const groups = useMemo(() => {
    const map = new Map<string, {
      key: string;
      owner: string;
      session_id: string;
      meeting: string;
      client: string;
      session_date: string;
      items: ActionItem[];
    }>();
    for (const it of items) {
      const ownerLabel = it.owner || "Unassigned";
      const key = `${it.session_id}|${ownerLabel}`;
      let g = map.get(key);
      if (!g) {
        g = {
          key,
          owner: ownerLabel,
          session_id: it.session_id,
          meeting: it.meeting,
          client: it.client,
          session_date: it.session_date,
          items: [],
        };
        map.set(key, g);
      }
      g.items.push(it);
    }
    // Sort groups: newest meeting first, then owner alphabetical
    return Array.from(map.values()).sort((a, b) => {
      if (a.session_date !== b.session_date) {
        return (b.session_date || "").localeCompare(a.session_date || "");
      }
      return a.owner.localeCompare(b.owner);
    });
  }, [items]);

  return (
    <div>
      {groups.map((g) => {
        const isOpen = expanded[g.key] ?? (groups.length <= 3);
        const openCount = g.items.filter((i) => !i.done).length;
        const doneCount = g.items.length - openCount;
        return (
          <div
            key={g.key}
            className="border-b last:border-b-0"
          >
            <button
              onClick={() =>
                setExpanded((prev) => ({ ...prev, [g.key]: !isOpen }))
              }
              className="w-full text-left flex items-center gap-3 p-4 hover:bg-muted/40 transition-colors"
            >
              {isOpen ? (
                <ChevronDown className="h-4 w-4 text-muted-foreground shrink-0" />
              ) : (
                <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
              )}
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium">
                  {g.owner}
                  <span className="text-muted-foreground font-normal">
                    {" "}· {openCount} open
                    {doneCount > 0 && ` · ${doneCount} done`}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground mt-1 flex items-center gap-2 flex-wrap">
                  {g.client && (
                    <Badge variant="outline" className="text-[10px]">{g.client}</Badge>
                  )}
                  <span className="text-primary truncate">{g.meeting}</span>
                  <span>·</span>
                  <span>{g.session_date}</span>
                </div>
              </div>
              <span
                role="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onOpenSession(g.session_id, "actions");
                }}
                className="h-7 w-7 inline-flex items-center justify-center rounded-md hover:bg-accent text-muted-foreground hover:text-foreground shrink-0"
                title="Open meeting"
              >
                <ExternalLink className="h-3.5 w-3.5" />
              </span>
            </button>
            {isOpen && (
              <div className="px-6 pb-3 space-y-1.5">
                {g.items.map((it, idx) => (
                  <div
                    key={idx}
                    className="flex items-start gap-3 text-sm py-1.5"
                  >
                    <span
                      className={`text-lg shrink-0 ${it.done ? "text-green-600" : "text-muted-foreground"}`}
                    >
                      {it.done ? "✓" : "○"}
                    </span>
                    <div className="flex-1 min-w-0 break-words">
                      {it.description}
                      {it.due && (
                        <span className="text-muted-foreground">
                          {" "}(Due: {it.due})
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
