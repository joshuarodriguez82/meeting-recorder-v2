"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api, computeItemHash, type ItemStatusDoc, type SessionSummary,
} from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { ChevronDown, ChevronRight, ExternalLink } from "lucide-react";

interface ActionItem {
  done: boolean;        // effective state — markdown OR override applied
  doneFromMarkdown: boolean;  // raw [x] state, shown for diff purposes if needed later
  itemHash: string;
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
// LLM extractions often label owners with the diarization-internal
// "SPEAKER_03" tag rather than the renamed display_name, even when the
// session has known speakers. We resolve those back to the real name
// at render time using the speakers map shipped on SessionSummary.
// Matches "SPEAKER_03", "Speaker 03", "speaker_03", etc.
const SPEAKER_TAG_RE = /^\s*SPEAKER[_\s]*0*(\d+)\s*$/i;

function resolveOwner(
  owner: string, speakers: Record<string, string> | undefined,
): string {
  if (!owner || !speakers) return owner;
  // Fast path: exact match against any speaker_id we know.
  if (speakers[owner]) return speakers[owner];
  const m = owner.match(SPEAKER_TAG_RE);
  if (!m) return owner;
  // Try every padding the diarization output uses: SPEAKER_00, SPEAKER_3, etc.
  const num = m[1];
  const candidates = [
    `SPEAKER_${num.padStart(2, "0")}`,
    `SPEAKER_${num}`,
    `SPEAKER_${num.padStart(3, "0")}`,
  ];
  for (const c of candidates) {
    if (speakers[c]) return speakers[c];
  }
  return owner;
}

interface ParsedRaw {
  doneFromMarkdown: boolean;
  owner: string;
  description: string;
  due: string;
  hashSource: string;
}

function parseRaw(text: string): ParsedRaw[] {
  if (!text) return [];
  const items: ParsedRaw[] = [];
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
    // The hash is computed on a stable identity for the item: owner +
    // description, not the LLM's [ ]/[x] prefix. That way flipping the
    // checkbox in the markdown doesn't invalidate the override, and a
    // new extraction that drops the [x] character still maps to the
    // same row.
    const hashSource = `${owner.trim()}|${desc.trim()}`;
    items.push({
      doneFromMarkdown: status === "x",
      owner,
      description: desc,
      due,
      hashSource,
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

  // Per-session item-status overlay. Keyed by session_id, then by item
  // hash. We fetch all of these once on mount because there are usually
  // just a few hundred sessions and the file-per-session overhead is
  // bounded — same pattern the commitments tracker uses.
  const [overrides, setOverrides] = useState<Record<string, ItemStatusDoc>>({});

  useEffect(() => {
    let cancelled = false;
    api.listAllItemStatus()
      .then((res) => { if (!cancelled) setOverrides(res.sessions || {}); })
      .catch(() => { /* leave empty; check-off still works locally */ });
    return () => { cancelled = true; };
  }, []);

  // Parse + hash every action item across every session. Hashing is
  // async (Web Crypto) so we stash the resolved items in state.
  const [allItems, setAllItems] = useState<ActionItem[]>([]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const out: ActionItem[] = [];
      for (const s of sessions) {
        if (!s.action_items) continue;
        const meta = {
          client: s.client,
          meeting: s.display_name,
          session_id: s.session_id,
          session_date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
        };
        const raws = parseRaw(s.action_items);
        for (const r of raws) {
          const h = await computeItemHash(r.hashSource);
          out.push({
            done: r.doneFromMarkdown,
            doneFromMarkdown: r.doneFromMarkdown,
            itemHash: h,
            // Resolve "SPEAKER_03" back to the real name when the
            // session has renamed speakers. Falls through unchanged
            // when the owner is already a real name, or when the
            // session has no speaker renames recorded.
            owner: resolveOwner(r.owner, s.speakers),
            description: r.description,
            due: r.due,
            ...meta,
          });
        }
      }
      if (!cancelled) setAllItems(out);
    })();
    return () => { cancelled = true; };
  }, [sessions]);

  // Apply overrides on top. Pure derivation so we never lose the raw
  // markdown signal if the override is removed later.
  const itemsWithOverrides = useMemo(() => {
    return allItems.map((it) => {
      const sessionDoc = overrides[it.session_id];
      const ov = sessionDoc?.follow_ups?.[it.itemHash];
      if (ov) return { ...it, done: !!ov.done };
      return it;
    });
  }, [allItems, overrides]);

  const clients = useMemo(
    () => ["All", ...Array.from(new Set(itemsWithOverrides.map((i) => i.client).filter(Boolean))).sort()],
    [itemsWithOverrides]
  );
  const owners = useMemo(
    () => ["All", ...Array.from(new Set(itemsWithOverrides.map((i) => i.owner).filter(Boolean))).sort()],
    [itemsWithOverrides]
  );

  const filtered = itemsWithOverrides.filter((i) => {
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

  const openCount = itemsWithOverrides.filter((i) => !i.done).length;

  // Optimistic toggle. Flip locally first, send to backend, roll back on
  // failure. Done-state is persisted in the per-session sidecar; the
  // markdown text is left alone so a re-extraction won't fight us.
  const toggleDone = async (item: ActionItem) => {
    const next = !item.done;
    setOverrides((prev) => {
      const sess = prev[item.session_id] ?? { follow_ups: {}, decisions: {} };
      return {
        ...prev,
        [item.session_id]: {
          follow_ups: {
            ...sess.follow_ups,
            [item.itemHash]: {
              done: next,
              updated_at: new Date().toISOString(),
            },
          },
          decisions: sess.decisions || {},
        },
      };
    });
    try {
      await api.setFollowUpDone(item.session_id, item.itemHash, next);
    } catch (e) {
      // Roll back on failure.
      setOverrides((prev) => {
        const sess = prev[item.session_id];
        if (!sess) return prev;
        const fu = { ...sess.follow_ups };
        delete fu[item.itemHash];
        return {
          ...prev,
          [item.session_id]: { ...sess, follow_ups: fu },
        };
      });
      console.warn("setFollowUpDone failed", e);
    }
  };

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  // Compose a unique key — itemHash can collide across sessions if two
  // meetings happen to have identical owner+description follow-ups.
  const keyFor = (i: ActionItem) => `${i.session_id}|${i.itemHash}`;
  const selected = filtered.find((i) => keyFor(i) === selectedKey)
    || itemsWithOverrides.find((i) => keyFor(i) === selectedKey)
    || null;

  return (
    <div className="mx-auto max-w-7xl space-y-4">
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
          placeholder="Search follow-ups..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 max-w-md"
        />
      </div>

      <p className="text-xs text-muted-foreground">
        {filtered.length} shown · {openCount} open of {itemsWithOverrides.length} total
      </p>

      {/* Split-pane layout matches Decisions and Commitments: list of
          rows on the left, detail panel + status dropdown on the right.
          Click a row to populate the detail. Status changes apply
          straight from the detail panel so the user never has to hunt
          for a checkbox buried in a collapsed group. */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardContent className="p-0">
            {filtered.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">
                No follow-ups match. Try a different filter.
              </p>
            ) : (
              filtered.map((i) => {
                const k = keyFor(i);
                return (
                  <button
                    key={k}
                    onClick={() => setSelectedKey(k)}
                    className={`w-full text-left flex items-start gap-3 border-b last:border-b-0 p-3 hover:bg-muted/40 transition-colors ${
                      selectedKey === k ? "bg-accent" : ""
                    }`}
                  >
                    <span className="text-primary mt-0.5">{i.done ? "✓" : "○"}</span>
                    <div className="flex-1 min-w-0">
                      <div className={`text-sm font-medium truncate ${i.done ? "line-through text-muted-foreground" : ""}`}>
                        {i.description || "(no description)"}
                      </div>
                      <div className="text-xs text-muted-foreground mt-0.5 flex items-center gap-2 flex-wrap">
                        <Badge
                          variant={i.done ? "secondary" : "default"}
                          className="text-[10px]"
                        >
                          {i.done ? "Done" : "Open"}
                        </Badge>
                        {i.owner && (
                          <Badge variant="outline" className="text-[10px]">
                            {i.owner}
                          </Badge>
                        )}
                        {i.client && (
                          <Badge variant="outline" className="text-[10px]">
                            {i.client}
                          </Badge>
                        )}
                        <span className="truncate">{i.meeting}</span>
                      </div>
                    </div>
                  </button>
                );
              })
            )}
          </CardContent>
        </Card>

        {/* Sticky detail panel — see decisions-view.tsx for rationale. */}
        <Card className="md:sticky md:top-0 md:self-start md:max-h-[calc(100vh-8rem)] md:overflow-y-auto">
          <CardContent className="p-6">
            {selected ? (
              <div className="space-y-4">
                <div className="flex items-start justify-between gap-3">
                  <h2 className="text-base font-semibold flex-1 min-w-0">
                    {selected.description || "(no description)"}
                  </h2>
                  <Badge variant={selected.done ? "secondary" : "default"}>
                    {selected.done ? "Done" : "Open"}
                  </Badge>
                </div>
                <div>
                  <div className="text-[10px] font-medium uppercase tracking-wider text-primary mb-1">
                    Status
                  </div>
                  <Select
                    value={selected.done ? "done" : "open"}
                    onValueChange={(v) => {
                      if (!v) return;
                      const wantDone = v === "done";
                      if (wantDone !== selected.done) toggleDone(selected);
                    }}
                  >
                    <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="open">Open</SelectItem>
                      <SelectItem value="done">Done</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                {selected.owner && (
                  <FollowUpField label="Owner">{selected.owner}</FollowUpField>
                )}
                {selected.due && (
                  <FollowUpField label="Due">{selected.due}</FollowUpField>
                )}
                <div className="pt-3 border-t flex items-center justify-between">
                  <div className="text-xs text-muted-foreground">
                    From <span className="font-medium text-foreground">{selected.meeting}</span>
                    {selected.session_date ? ` (${selected.session_date})` : ""}
                    {selected.client ? ` · ${selected.client}` : ""}
                  </div>
                  <button
                    onClick={() => onOpenSession(selected.session_id, "actions")}
                    className="text-xs text-primary hover:underline font-medium inline-flex items-center gap-1"
                  >
                    Open meeting <ExternalLink className="h-3 w-3" />
                  </button>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground text-center py-12">
                Select a follow-up to see details and change its status.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function FollowUpField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-medium uppercase tracking-wider text-primary mb-1">
        {label}
      </div>
      <div className="text-sm whitespace-pre-wrap break-words">{children}</div>
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
  items, onOpenSession, onToggle,
}: {
  items: ActionItem[];
  onOpenSession: (id: string, tab?: string) => void;
  onToggle: (item: ActionItem) => void;
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
                    <button
                      onClick={() => onToggle(it)}
                      className={`text-lg shrink-0 leading-none w-6 h-6 inline-flex items-center justify-center rounded hover:bg-accent transition-colors ${
                        it.done ? "text-green-600" : "text-muted-foreground hover:text-foreground"
                      }`}
                      title={it.done ? "Mark not done" : "Mark done"}
                      aria-pressed={it.done}
                    >
                      {it.done ? "✓" : "○"}
                    </button>
                    <div className={`flex-1 min-w-0 break-words ${it.done ? "line-through text-muted-foreground" : ""}`}>
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
