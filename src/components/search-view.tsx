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

export function SearchView({ onOpenSession }: { onOpenSession: (id: string) => void }) {
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

      // First pass: check the already-loaded summary/actions/decisions/reqs
      // fields from listSessions (no extra fetch needed). Only fetch full
      // sessions for ones that don't have a match in metadata.
      const re = new RegExp(escape(q), "i");
      const results: Match[] = [];
      const needsTranscriptCheck: typeof sessions = [];

      for (const s of sessions) {
        const metaHay = [
          s.display_name,
          s.client || "",
          s.project || "",
          s.summary || "",
          s.action_items || "",
          s.decisions || "",
          s.requirements || "",
        ].join("\n");
        if (re.test(metaHay)) {
          const m = re.exec(metaHay)!;
          results.push({
            session_id: s.session_id,
            display_name: s.display_name,
            date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
            snippet: makeSnippet(metaHay, m.index, q.length),
          });
        } else if (s.has_transcript) {
          needsTranscriptCheck.push(s);
        }
      }

      // Second pass: parallel fetch transcripts and search them
      const transcriptResults = await Promise.all(
        needsTranscriptCheck.map(async (s) => {
          try {
            const full = (await api.getSessionRaw(s.session_id)) as unknown as SessionFull;
            const transcript = full.segments
              ? full.segments.map((seg) => seg.text).join(" ")
              : "";
            if (!transcript) return null;
            const m = re.exec(transcript);
            if (!m) return null;
            return {
              session_id: s.session_id,
              display_name: s.display_name,
              date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
              snippet: makeSnippet(transcript, m.index, q.length),
            } as Match;
          } catch {
            return null;
          }
        })
      );
      for (const r of transcriptResults) if (r) results.push(r);

      setMatches(results);
      if (results.length === 0) toast.info("No matches found");
    } catch (e) {
      toast.error(`Search failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSearching(false);
    }
  };

  function makeSnippet(text: string, idx: number, qLen: number): string {
    const before = Math.max(0, idx - 80);
    const after = Math.min(text.length, idx + qLen + 80);
    return (before > 0 ? "…" : "")
      + text.slice(before, after).replace(/\s+/g, " ").trim()
      + (after < text.length ? "…" : "");
  }

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
              <button
                key={i}
                onClick={() => onOpenSession(m.session_id)}
                className="w-full text-left border-b last:border-b-0 p-4 hover:bg-muted/40 transition-colors min-w-0"
              >
                <div className="text-sm font-medium text-primary truncate">{m.display_name}</div>
                <div className="text-xs text-muted-foreground mb-2">{m.date}</div>
                <p className="text-sm text-foreground/90 italic break-words">{m.snippet}</p>
              </button>
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
