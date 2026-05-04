"use client";

import { useState } from "react";
import { api, type SessionFull } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Search, Sparkles, Type } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

// Two search modes share this view:
//
//   "fulltext"  Keyword / regex match against display_name + summary +
//               action items + decisions + transcripts. Fast, exact.
//               Existing behaviour from before #2 landed.
//
//   "semantic"  Vector similarity against MiniLM embeddings of every
//               processed session's transcript chunks. Finds chunks
//               that mean the same thing, even if they share no words
//               with the query. Backed by /search/semantic; needs
//               sentence-transformers installed in the venv and at
//               least one indexed session (auto-indexes after every
//               /process; older sessions need a one-time backfill in
//               Settings → Semantic Index).

type Mode = "fulltext" | "semantic";

interface FulltextMatch {
  kind: "fulltext";
  session_id: string;
  display_name: string;
  date: string;
  snippet: string;
}

interface SemanticMatch {
  kind: "semantic";
  session_id: string;
  display_name: string;
  date: string;
  // Cosine similarity 0-1 — surfaced as a badge so the user can tell
  // a strong match (~0.7+) from a "best we could find" weak one.
  similarity: number;
  // Chunk timestamp range so click-to-jump can scrub the audio to the
  // exact moment, once we wire that up.
  start_s: number;
  end_s: number;
  text: string;
}

type Match = FulltextMatch | SemanticMatch;

export function SearchView({ onOpenSession }: { onOpenSession: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("fulltext");
  const [searching, setSearching] = useState(false);
  const [matches, setMatches] = useState<Match[]>([]);
  const [total, setTotal] = useState(0);

  const search = async () => {
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setMatches([]);
    try {
      if (mode === "semantic") {
        await runSemantic(q);
      } else {
        await runFulltext(q);
      }
    } catch (e) {
      toast.error(`Search failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSearching(false);
    }
  };

  const runSemantic = async (q: string) => {
    const res = await api.semanticSearch(q, 15);
    const results: SemanticMatch[] = res.results.map((r) => ({
      kind: "semantic",
      session_id: r.session_id,
      display_name: r.display_name || "Untitled",
      date: r.started_at ? new Date(r.started_at).toLocaleDateString() : "",
      similarity: r.similarity,
      start_s: r.start_s,
      end_s: r.end_s,
      text: r.text,
    }));
    setMatches(results);
    // We don't know "total sessions" from the semantic endpoint, but
    // the index status endpoint does. Fetch in parallel for the count.
    api.searchIndexStatus()
      .then((s) => setTotal(s.indexed_sessions))
      .catch(() => setTotal(0));
    if (results.length === 0) {
      toast.info(
        "No semantic matches. Try different wording, or check that "
        + "your sessions are indexed (Settings → Semantic Index)."
      );
    }
  };

  const runFulltext = async (q: string) => {
    const sessions = await api.listSessions();
    setTotal(sessions.length);

    const re = new RegExp(escape(q), "i");
    const results: FulltextMatch[] = [];
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
          kind: "fulltext",
          session_id: s.session_id,
          display_name: s.display_name,
          date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
          snippet: makeSnippet(metaHay, m.index, q.length),
        });
      } else if (s.has_transcript) {
        needsTranscriptCheck.push(s);
      }
    }

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
            kind: "fulltext",
            session_id: s.session_id,
            display_name: s.display_name,
            date: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
            snippet: makeSnippet(transcript, m.index, q.length),
          } as FulltextMatch;
        } catch {
          return null;
        }
      })
    );
    for (const r of transcriptResults) if (r) results.push(r);
    setMatches(results);
    if (results.length === 0) toast.info("No matches found");
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
      {/* Mode picker — one row, two buttons. Could be a Tabs/Select but
          two options don't justify the chrome. */}
      <div className="flex items-center gap-1 rounded-lg border bg-muted/30 p-1 w-fit">
        <ModeButton
          active={mode === "fulltext"}
          onClick={() => setMode("fulltext")}
          icon={<Type className="h-3.5 w-3.5 mr-1.5" />}
          label="Keyword"
        />
        <ModeButton
          active={mode === "semantic"}
          onClick={() => setMode("semantic")}
          icon={<Sparkles className="h-3.5 w-3.5 mr-1.5" />}
          label="Semantic"
        />
      </div>

      <div className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder={
            mode === "semantic"
              ? "e.g. how should we approach pricing for ACME"
              : "e.g. auth approach, timeline, [scrubbed]..."
          }
          className="flex-1"
        />
        <Button onClick={search} disabled={searching || !query.trim()}>
          {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          <span className="ml-2">Search</span>
        </Button>
      </div>

      <p className="text-[11px] text-muted-foreground">
        {mode === "semantic" ? (
          <>
            Semantic search ranks transcript chunks by meaning, not keyword.
            Slower (~1s) but finds matches that share zero words with your
            query. Only sessions that have been indexed are searchable.
          </>
        ) : (
          <>
            Keyword search runs a regex over titles, summaries, action
            items, decisions, requirements, and transcripts. Fast and exact.
          </>
        )}
      </p>

      {matches.length > 0 && (
        <p className="text-xs text-muted-foreground">
          {matches.length} {mode === "semantic" ? "chunks" : "matches"}
          {total > 0 ? ` across ${total} sessions` : ""}
        </p>
      )}

      <Card>
        <CardContent className="p-0">
          {matches.length === 0 ? (
            <p className="text-sm text-muted-foreground py-8 text-center">
              {mode === "semantic"
                ? "Type a query and hit Enter to search by meaning across every indexed session."
                : "Type a query and hit Enter to search every transcript."}
            </p>
          ) : (
            matches.map((m, i) => (
              <button
                key={`${m.session_id}-${i}`}
                onClick={() => onOpenSession(m.session_id)}
                className="w-full text-left border-b last:border-b-0 p-4 hover:bg-muted/40 transition-colors min-w-0"
              >
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm font-medium text-primary truncate">{m.display_name}</span>
                  {m.kind === "semantic" && (
                    <span
                      className={
                        "rounded-full px-2 py-0.5 text-[10px] font-medium "
                        + similarityColor(m.similarity)
                      }
                      title="Cosine similarity. >0.7 strong, 0.5–0.7 plausible, <0.5 weak."
                    >
                      {Math.round(m.similarity * 100)}% match
                    </span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground mb-2">
                  {m.date}
                  {m.kind === "semantic" && (
                    <> · {formatTimeRange(m.start_s, m.end_s)}</>
                  )}
                </div>
                <p className="text-sm text-foreground/90 italic break-words">
                  {m.kind === "semantic" ? m.text : m.snippet}
                </p>
              </button>
            ))
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function ModeButton({
  active, onClick, icon, label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "flex items-center px-3 py-1.5 rounded-md text-xs font-medium transition-colors "
        + (active
          ? "bg-background shadow-sm text-foreground"
          : "text-muted-foreground hover:text-foreground")
      }
    >
      {icon}
      {label}
    </button>
  );
}

function similarityColor(sim: number): string {
  // Color the badge by confidence. Calibrated against MiniLM's empirical
  // distribution on transcript chunks: ≥0.7 is genuinely topical, 0.5-0.7
  // is plausible, below 0.5 is grasping at straws.
  if (sim >= 0.7) return "bg-primary/15 text-primary";
  if (sim >= 0.5) return "bg-amber-500/15 text-amber-700 dark:text-amber-400";
  return "bg-muted text-muted-foreground";
}

function formatTimeRange(start: number, end: number): string {
  return `${formatTime(start)}–${formatTime(end)}`;
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function escape(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
