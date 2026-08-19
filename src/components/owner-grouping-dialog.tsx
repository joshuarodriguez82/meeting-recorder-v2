"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type OwnerAlias, type OwnerSuggestionGroup } from "@/lib/api";
import { aggregateRawOwners } from "@/lib/owner-grouping";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Check, X, Users, Split, Loader2, Merge,
} from "lucide-react";
import { toast } from "sonner";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Raw owner strings straight off currently-loaded Follow Ups — used
  // to compute counts/candidates for the manual-merge picker. Not the
  // full cross-session picture (that also includes Commitments, only
  // visible server-side in the suggestion feed) but good enough for
  // "which names can I manually merge right now".
  rawOwnerStrings: string[];
  aliases: OwnerAlias[];
  onAliasesChanged: () => void;
}

export function OwnerGroupingDialog({
  open, onOpenChange, rawOwnerStrings, aliases, onAliasesChanged,
}: Props) {
  const [suggestions, setSuggestions] = useState<OwnerSuggestionGroup[]>([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [canonicalDrafts, setCanonicalDrafts] = useState<Record<string, string>>({});
  const [manualSelection, setManualSelection] = useState<Set<string>>(new Set());
  const [manualCanonical, setManualCanonical] = useState("");

  const loadSuggestions = () => {
    setLoadingSuggestions(true);
    api.getOwnerSuggestions()
      .then((res) => setSuggestions(res.groups || []))
      .catch(() => setSuggestions([]))
      .finally(() => setLoadingSuggestions(false));
  };

  useEffect(() => {
    // Data fetch triggered by the dialog opening; loadSuggestions()'s
    // first line flips the loading flag before the request settles.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (open) loadSuggestions();
  }, [open]);

  // Every tier-2 key already claimed by a confirmed alias — excluded
  // from the manual-merge picker below (it already belongs somewhere).
  const aliasedKeys = useMemo(
    () => new Set(aliases.flatMap((a) => a.members)),
    [aliases]
  );

  const { counts, display } = useMemo(
    () => aggregateRawOwners(rawOwnerStrings), [rawOwnerStrings]
  );
  const ungroupedEntries = useMemo(
    () => Object.keys(counts)
      .filter((k) => !aliasedKeys.has(k))
      .map((k) => ({ key: k, display: display[k] || k, count: counts[k] }))
      .sort((a, b) => b.count - a.count || a.display.localeCompare(b.display)),
    [counts, display, aliasedKeys]
  );

  const acceptSuggestion = async (g: OwnerSuggestionGroup, selectedKeys: string[]) => {
    if (selectedKeys.length < 1) return;
    const canonical = (canonicalDrafts[g.group_id] || g.suggested_canonical).trim();
    if (!canonical) return;
    setBusy(g.group_id);
    try {
      await api.createOwnerAlias(canonical, selectedKeys);
      toast.success(`Merged into "${canonical}"`);
      onAliasesChanged();
      loadSuggestions();
    } catch (e) {
      toast.error("Could not create the merge");
      console.warn(e);
    } finally {
      setBusy(null);
    }
  };

  const rejectSuggestion = async (g: OwnerSuggestionGroup) => {
    setBusy(g.group_id);
    try {
      // Advisory bookkeeping only — record every member paired against
      // the group's anchor so this exact group stops resurfacing.
      const anchor = g.members[0]?.key;
      if (anchor) {
        await Promise.all(
          g.members.slice(1).map((m) => api.rejectOwnerSuggestion(anchor, m.key))
        );
      }
      loadSuggestions();
    } catch (e) {
      toast.error("Could not dismiss the suggestion");
      console.warn(e);
    } finally {
      setBusy(null);
    }
  };

  const removeMember = async (alias: OwnerAlias, key: string) => {
    setBusy(alias.id);
    try {
      await api.updateOwnerAlias(alias.id, { remove_members: [key] });
      toast.success("Split back out");
      onAliasesChanged();
    } catch (e) {
      toast.error("Could not split that entry out");
      console.warn(e);
    } finally {
      setBusy(null);
    }
  };

  const deleteAlias = async (alias: OwnerAlias) => {
    setBusy(alias.id);
    try {
      await api.deleteOwnerAlias(alias.id);
      toast.success(`Ungrouped "${alias.canonical}"`);
      onAliasesChanged();
    } catch (e) {
      toast.error("Could not ungroup");
      console.warn(e);
    } finally {
      setBusy(null);
    }
  };

  const renameAlias = async (alias: OwnerAlias, canonical: string) => {
    if (!canonical.trim() || canonical.trim() === alias.canonical) return;
    try {
      await api.updateOwnerAlias(alias.id, { canonical: canonical.trim() });
      onAliasesChanged();
    } catch (e) {
      toast.error("Could not rename");
      console.warn(e);
    }
  };

  const mergeManualSelection = async () => {
    const keys = Array.from(manualSelection);
    const canonical = manualCanonical.trim()
      || display[keys.sort((a, b) => (counts[b] || 0) - (counts[a] || 0))[0]]
      || keys[0];
    if (keys.length < 2 || !canonical) return;
    setBusy("manual");
    try {
      await api.createOwnerAlias(canonical, keys);
      toast.success(`Merged into "${canonical}"`);
      setManualSelection(new Set());
      setManualCanonical("");
      onAliasesChanged();
    } catch (e) {
      toast.error("Could not create the merge");
      console.warn(e);
    } finally {
      setBusy(null);
    }
  };

  const toggleManual = (key: string) => {
    setManualSelection((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl w-[95vw] max-h-[85vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-6 py-4 border-b">
          <DialogTitle className="text-lg flex items-center gap-2">
            <Users className="h-4 w-4 text-primary" /> Manage owner grouping
          </DialogTitle>
          <p className="text-xs text-muted-foreground">
            Merging groups spelling/format variants of the same person
            under one name for filtering. The original text on each item
            is never changed — only how it&apos;s grouped.
          </p>
        </DialogHeader>

        <ScrollArea className="flex-1 min-h-0">
          <div className="p-6 space-y-8">
            {/* ── Suggested merges ── */}
            <section className="space-y-3">
              <h3 className="text-xs font-medium uppercase tracking-wider text-primary">
                Suggested merges
              </h3>
              {loadingSuggestions ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
                  <Loader2 className="h-4 w-4 animate-spin" /> Looking for likely matches…
                </div>
              ) : suggestions.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No pending suggestions. New ones appear as Follow Ups and
                  Commitments pick up new owner spellings.
                </p>
              ) : (
                <div className="space-y-3">
                  {suggestions.map((g) => (
                    <SuggestionCard
                      key={g.group_id}
                      group={g}
                      canonical={canonicalDrafts[g.group_id] ?? g.suggested_canonical}
                      onCanonicalChange={(v) =>
                        setCanonicalDrafts((prev) => ({ ...prev, [g.group_id]: v }))}
                      busy={busy === g.group_id}
                      onAccept={(keys) => acceptSuggestion(g, keys)}
                      onReject={() => rejectSuggestion(g)}
                    />
                  ))}
                </div>
              )}
            </section>

            {/* ── Existing (confirmed) groups ── */}
            <section className="space-y-3">
              <h3 className="text-xs font-medium uppercase tracking-wider text-primary">
                Confirmed groups
              </h3>
              {aliases.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No groups yet — accept a suggestion above, or merge names
                  manually below.
                </p>
              ) : (
                <div className="space-y-2">
                  {aliases.map((a) => (
                    <div key={a.id} className="rounded-lg border p-3 space-y-2">
                      <div className="flex items-center gap-2">
                        <Input
                          defaultValue={a.canonical}
                          onBlur={(e) => renameAlias(a, e.target.value)}
                          className="h-7 w-48 text-sm font-medium"
                        />
                        <span className="text-xs text-muted-foreground">
                          {a.members.length} spelling{a.members.length === 1 ? "" : "s"}
                        </span>
                        <Button
                          variant="ghost" size="icon-xs"
                          className="ml-auto text-muted-foreground hover:text-destructive"
                          disabled={busy === a.id}
                          onClick={() => deleteAlias(a)}
                          title="Ungroup everything"
                        >
                          <Split className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {a.members.map((m) => (
                          <Badge key={m} variant="outline" className="text-[11px] gap-1 pr-1">
                            {m}
                            <button
                              onClick={() => removeMember(a, m)}
                              disabled={busy === a.id}
                              className="hover:text-destructive"
                              title="Split this spelling back out"
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </Badge>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* ── Manual merge ── */}
            <section className="space-y-3">
              <h3 className="text-xs font-medium uppercase tracking-wider text-primary">
                Manual merge
              </h3>
              {ungroupedEntries.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  Nothing ungrouped in the currently-loaded Follow Ups.
                </p>
              ) : (
                <>
                  <div className="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto rounded-lg border p-2">
                    {ungroupedEntries.map((e) => {
                      const checked = manualSelection.has(e.key);
                      return (
                        <button
                          key={e.key}
                          onClick={() => toggleManual(e.key)}
                          className={`text-xs rounded-full border px-2.5 py-1 transition-colors ${
                            checked
                              ? "bg-primary text-primary-foreground border-primary"
                              : "hover:bg-muted"
                          }`}
                        >
                          {e.display} <span className="opacity-70">({e.count})</span>
                        </button>
                      );
                    })}
                  </div>
                  <div className="flex items-center gap-2">
                    <Input
                      placeholder="Canonical name (e.g. Sam)"
                      value={manualCanonical}
                      onChange={(e) => setManualCanonical(e.target.value)}
                      className="h-8 flex-1 max-w-xs"
                    />
                    <Button
                      size="sm"
                      disabled={manualSelection.size < 2 || busy === "manual"}
                      onClick={mergeManualSelection}
                    >
                      <Merge className="h-3.5 w-3.5" /> Merge selected
                    </Button>
                  </div>
                </>
              )}
            </section>
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

function SuggestionCard({
  group, canonical, onCanonicalChange, busy, onAccept, onReject,
}: {
  group: OwnerSuggestionGroup;
  canonical: string;
  onCanonicalChange: (v: string) => void;
  busy: boolean;
  onAccept: (selectedKeys: string[]) => void;
  onReject: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(group.members.map((m) => m.key))
  );

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };

  return (
    <div className="rounded-lg border p-3 space-y-2">
      <div className="flex items-center gap-2">
        <Input
          value={canonical}
          onChange={(e) => onCanonicalChange(e.target.value)}
          className="h-7 w-48 text-sm font-medium"
        />
        <span className="text-xs text-muted-foreground">
          {group.members.length} possible matches
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost" size="icon-xs"
            disabled={busy}
            onClick={onReject}
            title="Not the same person — dismiss"
          >
            <X className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="secondary" size="sm"
            disabled={busy || selected.size < 1}
            onClick={() => onAccept(Array.from(selected))}
          >
            <Check className="h-3.5 w-3.5" /> Merge
          </Button>
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {group.members.map((m) => {
          const checked = selected.has(m.key);
          return (
            <button
              key={m.key}
              onClick={() => toggle(m.key)}
              className={`text-xs rounded-full border px-2.5 py-1 transition-colors ${
                checked
                  ? "bg-primary text-primary-foreground border-primary"
                  : "hover:bg-muted"
              }`}
            >
              {m.display} <span className="opacity-70">({m.count})</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
