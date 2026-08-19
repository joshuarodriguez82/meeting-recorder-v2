"use client";

import { AlertTriangle, FileText, Loader2, ScrollText } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

// Result rendering for the Knowledge Base view. Split out of
// knowledge-view.tsx so the container stays about *behaviour* (what
// runs when, and what costs money) and this file stays about *pixels*.
//
// The three hit shapes are a discriminated union on `kind`. They are
// NOT interchangeable: a Knowledge Folder document hit has no
// session_id, so it must never be rendered as a clickable session row.
// Doing that was a real shipped bug — it produced an unopenable
// "Untitled" meeting — and DocumentRow below exists to keep it fixed.

export interface FulltextMatch {
  kind: "fulltext";
  session_id: string;
  display_name: string;
  date: string;
  snippet: string;
  // Where the hit came from: session metadata (title/summary/
  // extractions) or the transcript body. Only meaningful in keyword
  // mode, where the two are searched by separate passes with very
  // different costs.
  field: "metadata" | "transcript";
}

export interface SemanticMatch {
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
// no session_id/date/timestamps, because it isn't a session.
export interface DocumentMatch {
  kind: "document";
  doc_name: string;
  doc_path: string;
  client: string;
  similarity: number;
  text: string;
}

export type Match = FulltextMatch | SemanticMatch | DocumentMatch;

// An empty result set and a failed request look identical if you only
// track `matches.length === 0`, so search state is an explicit union
// instead. "ok" carries the corpus size for the count line.
export type SearchStatus =
  | { state: "idle" }
  | { state: "searching" }
  | { state: "ok"; total: number }
  | { state: "error"; message: string };

export function ResultList({
  matches, status, mode, query, deepScan, onOpenSession, onRetry, onDeepScan,
}: {
  matches: Match[];
  status: SearchStatus;
  mode: "keyword" | "semantic";
  query: string;
  deepScan: boolean;
  onOpenSession: (id: string) => void;
  onRetry: () => void;
  onDeepScan: () => void;
}) {
  return (
    <Card>
      <CardContent className="p-0">
        {matches.length > 0 ? (
          matches.map((m, i) =>
            m.kind === "document" ? (
              <DocumentRow key={`doc-${m.doc_path}-${i}`} match={m} />
            ) : (
              <SessionRow
                key={`${m.session_id}-${i}`}
                match={m}
                onOpen={() => onOpenSession(m.session_id)}
              />
            )
          )
        ) : status.state === "error" ? (
          <div className="flex flex-col items-center gap-2 py-8 px-4 text-center">
            <AlertTriangle className="h-5 w-5 text-destructive" />
            <p className="text-sm text-destructive">Search failed</p>
            <p className="text-xs text-muted-foreground max-w-md break-words">
              {status.message}
            </p>
            <button
              onClick={onRetry}
              className="text-xs text-primary underline underline-offset-2 hover:no-underline"
            >
              Try again
            </button>
          </div>
        ) : status.state === "searching" ? (
          <p className="flex items-center justify-center gap-2 text-sm text-muted-foreground py-8">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Searching…
          </p>
        ) : status.state === "ok" ? (
          <div className="flex flex-col items-center gap-2 py-8 px-4 text-center">
            <p className="text-sm text-muted-foreground">
              No matches for{" "}
              <span className="text-foreground">&ldquo;{query}&rdquo;</span>
            </p>
            {mode === "keyword" ? (
              deepScan ? (
                <p className="text-xs text-muted-foreground max-w-md">
                  Nothing in titles, summaries, extractions or transcripts.
                  Try <strong>Semantic</strong> — it matches on meaning, and
                  it covers Knowledge Folder documents too.
                </p>
              ) : (
                <>
                  <p className="text-xs text-muted-foreground max-w-md">
                    Titles, summaries and extractions only — transcript
                    bodies weren&apos;t scanned.
                  </p>
                  <button
                    onClick={onDeepScan}
                    className="inline-flex items-center gap-1.5 text-xs text-primary underline underline-offset-2 hover:no-underline"
                  >
                    <ScrollText className="h-3 w-3" />
                    Search inside transcripts
                  </button>
                </>
              )
            ) : (
              <p className="text-xs text-muted-foreground max-w-md">
                Try different wording, or check that your sessions are
                indexed (Settings → Semantic Index).
              </p>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground py-8 text-center">
            {mode === "semantic"
              ? "Start typing to search by meaning across every indexed session and document."
              : "Start typing to search titles, summaries and extractions."}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function SessionRow({
  match: m, onOpen,
}: {
  match: FulltextMatch | SemanticMatch;
  onOpen: () => void;
}) {
  return (
    <button
      onClick={onOpen}
      className="w-full text-left border-b last:border-b-0 p-4 hover:bg-muted/40 transition-colors min-w-0"
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-medium text-primary truncate">
          {m.display_name}
        </span>
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
        {m.kind === "fulltext" && m.field === "transcript" && (
          <span className="rounded-full px-2 py-0.5 text-[10px] font-medium bg-muted text-muted-foreground">
            Transcript
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
  );
}

// Renders a Knowledge Folder document hit. Deliberately NOT a
// clickable button that calls onOpenSession — a document has no
// session to open, and forcing it through that path is exactly the bug
// this guards against (see the union comment at the top). There's no
// safe existing endpoint to open an arbitrary file path from here
// (api.openFolder's "path" kind is built for folders and auto-creates
// missing paths as directories — reusing it on a possibly-stale
// doc_path risks silently littering the filesystem with bogus
// folders), so the row surfaces the path as muted, title-tooltipped
// text and lets the user locate it themselves.
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

export function similarityColor(sim: number): string {
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

export function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
