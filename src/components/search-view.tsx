"use client";

import { useState } from "react";
import { api, type SessionFull } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

interface Match {
  session_id: string;
  display_name: string;
  date: string;
  snippet: string;
}

export function SearchView() {
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [matches, setMatches] = useState<Match[]>([]);
  const [total, setTotal] = useState(0);

  const search = async () => {
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    try {
      const sessions = await api.listSessions();
      setTotal(sessions.length);
      const results: Match[] = [];
      for (const s of sessions) {
        let full: SessionFull;
        try {
          full = (await api.getSessionRaw(s.session_id)) as unknown as SessionFull;
        } catch {
          continue;
        }
        const transcript = full.segments
          ? full.segments.map((seg) => `${seg.text}`).join(" ")
          : "";
        const haystack = [
          transcript,
          full.summary || "",
          full.action_items || "",
          full.decisions || "",
          full.requirements || "",
        ].join("\n");
        const re = new RegExp(escape(q), "i");
        const m = re.exec(haystack);
        if (!m) continue;
        const idx = m.index;
        const before = Math.max(0, idx - 80);
        const after = Math.min(haystack.length, idx + q.length + 80);
        const snippet = (before > 0 ? "…" : "") + haystack.slice(before, after).replace(/\s+/g, " ").trim() + (after < haystack.length ? "…" : "");
        results.push({
          session_id: s.session_id,
          display_name: s.display_name,
          date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
          snippet,
        });
      }
      setMatches(results);
      if (results.length === 0) toast.info("No matches found");
    } catch (e) {
      toast.error(`Search failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="e.g. auth approach, timeline, simplisafe..."
          className="flex-1"
        />
        <Button onClick={search} disabled={searching || !query.trim()}>
          {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          <span className="ml-2">Search</span>
        </Button>
      </div>

      {matches.length > 0 && (
        <p className="text-xs text-muted-foreground">
          {matches.length} matches across {total} sessions
        </p>
      )}

      <Card>
        <CardContent className="p-0">
          {matches.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              Type a query and hit Enter to search every transcript.
            </p>
          ) : (
            matches.map((m, i) => (
              <div key={i} className="border-b last:border-b-0 p-4 hover:bg-muted/40">
                <div className="text-sm font-medium">{m.display_name}</div>
                <div className="text-xs text-muted-foreground mb-2">{m.date}</div>
                <p className="text-sm text-foreground/90 italic">{m.snippet}</p>
              </div>
            ))
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function escape(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
