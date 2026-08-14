"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type SessionFull, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { FileText, Loader2, Search, Sparkles, Type } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

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

// A Knowledge Folder document hit. Distinct shape from SemanticMatch —
// no session_id/date/timestamps, because it isn't a session. Rendering
// this as a degraded SemanticMatch (the old bug) produced an unopenable
// "Untitled" row; a document hit must render — and behave — as a
// document instead.
interface DocumentMatch {
  kind: "document";
  doc_name: string;
  doc_path: string;
  client: string;
  similarity: number;
  text: string;
}

type Match = FulltextMatch | SemanticMatch | DocumentMatch;

export function SearchView({ onOpenSession }: { onOpenSession: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("fulltext");
  const [searching, setSearching] = useState(false);
  const [matches, setMatches] = useState<Match[]>([]);
  const [total, setTotal] = useState(0);

  // Scope filter — mirrors qa-view.tsx's client/project Select pair
  // exactly (same components, same derivation from the session list,
  // same reset-on-client-change behaviour) so Search and Ask feel like
  // one app instead of two half-built ones.
  const [client, setClient] = useState<string>("");
  const [project, setProject] = useState<string>("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  useEffect(() => {
    api.listSessions().then(setSessions).catch(() => setSessions([]));
  }, []);
  const clients = useMemo(() => {
    const set = new Set<string>();
    for (const s of sessions) if (s.client) set.add(s.client);
    return Array.from(set).sort();
  }, [sessions]);
  const projects = useMemo(() => {
    if (!client) return [];
    const set = new Set<string>();
    for (const s of sessions) {
      if (s.client === client && s.project) set.add(s.project);
    }
    return Array.from(set).sort();
  }, [sessions, client]);
  // Reset project when the client changes — a project from one client
  // makes no sense scoped to another. Done inline in the client
  // Select's onValueChange below (not a useEffect keyed on `client`,
  // unlike qa-view.tsx's otherwise-identical pattern) so the reset
  // doesn't trip react-hooks/set-state-in-effect; same outcome, no new
  // lint violation.
  const selectClient = (v: string | null) => {
    setClient(v === "__all__" || !v ? "" : v);
    setProject("");
  };

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
    const res = await api.semanticSearch(q, 15, client || undefined, project || undefined);
    const results: Match[] = res.results.map((r): Match => {
      if (r.source === "document") {
        return {
          kind: "document",
          doc_name: r.doc_name || "Untitled document",
          doc_path: r.doc_path,
          client: r.client,
          similarity: r.similarity,
          text: r.text,
        };
      }
      // r.source is "session" or undefined (older backend predating the
      // field) — both are session hits.
      return {
        kind: "semantic",
        session_id: r.session_id,
        display_name: r.display_name || "Untitled",
        date: r.started_at ? new Date(r.started_at).toLocaleDateString() : "",
        similarity: r.similarity,
        start_s: r.start_s,
        end_s: r.end_s,
        text: r.text,
      };
    });
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
    const allSessions = await api.listSessions();
    // Apply the same client/project scope as semantic mode so the
    // selector means the same thing in both modes — a scope control
    // that silently no-ops in one mode is worse than no control.
    const sessions = allSessions.filter((s) => {
      if (client && s.client !== client) return false;
      if (project && s.project !== project) return false;
      return true;
    });
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
      {/* Top-of-view explainer — Search and Ask are NOT the same.
          Search finds matching transcript chunks. Ask synthesizes an
          answer from those chunks via the LLM. Users have asked which
          to use; this banner answers that question once, in context. */}
      <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
        <strong className="text-foreground">Search finds matching chunks.</strong>{" "}
        Two modes: <strong>Keyword</strong> grep across summaries +
        transcripts, or <strong>Semantic</strong> embedding similarity.
        You get snippets back — go read them yourself.{" "}
        <span className="text-foreground/80">
          Want a written answer instead? Use{" "}
          <strong>Ask</strong> — it pulls the same chunks but pipes them
          through Claude to write a synthesized response with citations.
        </span>
      </div>

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

      {/* Scope filters — same pattern as qa-view.tsx: client Select,
          dependent project Select that only appears once a client is
          chosen, options derived from the session list, reset on
          client change. Applies in both modes so the selector means
          the same thing regardless of which one is active. */}
      <div className="flex items-center gap-3 text-xs flex-wrap">
        <span className="text-muted-foreground">Scope:</span>
        <Select
          value={client || "__all__"}
          onValueChange={selectClient}
        >
          <SelectTrigger className="h-7 w-44 text-xs">
            <SelectValue placeholder="All clients" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">All clients</SelectItem>
            {clients.map((c) => (
              <SelectItem key={c} value={c}>{c}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {client && projects.length > 0 && (
          <Select
            value={project || "__all__"}
            onValueChange={(v) => setProject(v === "__all__" || !v ? "" : v)}
          >
            <SelectTrigger className="h-7 w-44 text-xs">
              <SelectValue placeholder="All projects" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">All projects</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {(client || project) && (
          <button
            onClick={() => { setClient(""); setProject(""); }}
            className="text-[11px] text-muted-foreground hover:text-foreground underline"
          >
            Clear
          </button>
        )}
      </div>

      <div className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder={
            mode === "semantic"
              ? "e.g. how should we approach pricing for ACME"
              : "e.g. auth approach, timeline, simplisafe..."
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
            query. Only sessions and Knowledge Folder documents that have
            been indexed are searchable.
          </>
        ) : (
          <>
            Keyword search runs a regex over titles, summaries, action
            items, decisions, requirements, and transcripts —{" "}
            <strong>sessions only</strong>, Knowledge Folder documents
            aren&apos;t included. Fast and exact.
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
            matches.map((m, i) =>
              m.kind === "document" ? (
                <DocumentRow key={`doc-${m.doc_path}-${i}`} match={m} />
              ) : (
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
              )
            )
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// Renders a Knowledge Folder document hit. Deliberately NOT a
// clickable button that calls onOpenSession — a document has no
// session to open, and the old code's attempt to force it through
// that path is exactly the bug this fixes (see the module comment on
// DocumentMatch). There's no safe existing endpoint to open an
// arbitrary file path from here (api.openFolder's "path" kind is built
// for folders and auto-creates missing paths as directories — reusing
// it on a possibly-stale doc_path risks silently littering the
// filesystem with bogus folders), so the row surfaces the path as
// muted, title-tooltipped text and lets the user locate it themselves.
function DocumentRow({ match: m }: { match: DocumentMatch }) {
  return (
    <div className="w-full text-left border-b last:border-b-0 p-4 min-w-0">
      <div className="flex items-center gap-2 flex-wrap">
        <FileText className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
        <span className="text-sm font-medium truncate">{m.doc_name}</span>
        <span className="rounded-full px-2 py-0.5 text-[10px] font-medium bg-muted text-muted-foreground">
          Document
        </span>
        <span
          className={
            "rounded-full px-2 py-0.5 text-[10px] font-medium "
            + similarityColor(m.similarity)
          }
          title="Cosine similarity. >0.7 strong, 0.5–0.7 plausible, <0.5 weak."
        >
          {Math.round(m.similarity * 100)}% match
        </span>
      </div>
      <div className="text-xs text-muted-foreground mb-2">
        {m.client || "No client"}
      </div>
      <p className="text-sm text-foreground/90 italic break-words">
        {m.text}
      </p>
      {m.doc_path && (
        <p
          className="text-[11px] text-muted-foreground/70 mt-1.5 truncate"
          title={m.doc_path}
        >
          {m.doc_path}
        </p>
      )}
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
