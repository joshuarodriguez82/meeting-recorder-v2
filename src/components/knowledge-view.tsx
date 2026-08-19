"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type SessionFull, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Sparkles, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { QueryControls, type Mode } from "@/components/knowledge-controls";
import {
  ResultList, type DocumentMatch, type FulltextMatch, type Match,
  type SearchStatus, type SemanticMatch,
} from "@/components/knowledge-results";
import { AnswerThread, type Turn } from "@/components/knowledge-answer";

// Knowledge Base — one door into the semantic index, replacing the old
// Search and Ask tabs (they were two doors into the same room; #2 in
// the merge issue).
//
// The interaction model, and the one rule that shapes everything else:
//
//   Typing searches. Free, local where possible, no confirmation.
//   Answering costs an LLM call, so it NEVER fires on its own.
//
// Concretely:
//   • Every keystroke (debounced) refreshes the result list. Keyword
//     mode matches locally against the already-loaded session list;
//     semantic mode hits /search/semantic, which is an embedding
//     lookup — no LLM, no per-query billing.
//   • An answer is only ever started by `askAnswer`, and `askAnswer` is
//     only ever called from an explicit user gesture: the Answer
//     button, the inline "answer this question" suggestion, or ⌘/Ctrl
//     +Enter. It is called from no effect, no debounce, no auto-run
//     path. If you add a caller, it had better be a click.
//   • Follow-ups append to the same thread, so the answer half behaves
//     like the old Ask tab once you are in a conversation.
//
// Search mode and scope apply to both halves: the answer is generated
// from the same query and the same client/project filter as the
// results below it. That is the whole point of merging the tabs.

// Debounce per mode. Keyword matching is pure local work over an
// in-memory array, so it can be near-instant; semantic mode crosses a
// process boundary and runs a MiniLM encode, so it waits for a real
// pause in typing.
const DEBOUNCE_MS: Record<Mode, number> = { keyword: 120, semantic: 400 };

// Heuristic for "this reads like a question, not a keyword". Used only
// to *offer* an answer inline — it never triggers one.
const QUESTION_OPENERS = /^(what|why|how|when|where|who|whom|whose|which|did|do|does|is|are|was|were|can|could|should|would|will|has|have|had|any)\b/i;

function looksLikeQuestion(q: string): boolean {
  const t = q.trim();
  if (t.endsWith("?")) return true;
  return QUESTION_OPENERS.test(t) && t.split(/\s+/).length >= 4;
}

export function KnowledgeView({
  onOpenSession,
}: {
  onOpenSession: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("keyword");
  // Keyword mode searches session metadata (title / summary / action
  // items / decisions / requirements) for free off the cached list.
  // Scanning transcript *bodies* means one fetch per unmatched session,
  // which is far too expensive to run on every keystroke — so it's an
  // opt-in that the empty state also offers as a one-click escape
  // hatch. Once fetched, a transcript is cached for the session's life.
  const [deepScan, setDeepScan] = useState(false);
  const [matches, setMatches] = useState<Match[]>([]);
  const [status, setStatus] = useState<SearchStatus>({ state: "idle" });

  // Scope filter — one pair of selects for the whole view, applied to
  // keyword filtering, semantic search and the answer request alike.
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
  // makes no sense scoped to another. Done inline in the Select's
  // onValueChange rather than in a useEffect keyed on `client` so it
  // doesn't trip react-hooks/set-state-in-effect.
  const selectClient = (v: string | null) => {
    setClient(v === "__all__" || !v ? "" : v);
    setProject("");
  };

  // Answer thread state.
  const [turns, setTurns] = useState<Turn[]>([]);
  const abortRef = useRef<{ abort: () => void } | null>(null);
  const isStreaming = turns.length > 0
    && turns[turns.length - 1].status === "streaming";

  // Transcript body cache for deep keyword scans, so flipping a word in
  // the query doesn't re-download every transcript.
  const transcriptCache = useRef(new Map<string, string>());
  // Monotonic request id: a slow response from an older keystroke must
  // never overwrite a newer one's results.
  const seqRef = useRef(0);

  const getTranscript = useCallback(async (id: string): Promise<string> => {
    const cached = transcriptCache.current.get(id);
    if (cached !== undefined) return cached;
    try {
      const full = (await api.getSessionRaw(id)) as unknown as SessionFull;
      const text = full.segments
        ? full.segments.map((seg) => seg.text).join(" ")
        : "";
      transcriptCache.current.set(id, text);
      return text;
    } catch {
      // Cache the failure as empty so a broken session isn't refetched
      // on every keystroke.
      transcriptCache.current.set(id, "");
      return "";
    }
  }, []);

  const runSearch = useCallback(async (q: string) => {
    const seq = ++seqRef.current;
    const fresh = () => seq === seqRef.current;
    if (!q) {
      setMatches([]);
      setStatus({ state: "idle" });
      return;
    }
    setStatus({ state: "searching" });
    try {
      if (mode === "semantic") {
        const res = await api.semanticSearch(
          q, 15, client || undefined, project || undefined,
        );
        if (!fresh()) return;
        const results: Match[] = res.results.map((r): Match => {
          if (r.source === "document") {
            // Document hits keep their own shape all the way to the
            // renderer — never coerced into a session row.
            const d: DocumentMatch = {
              kind: "document",
              doc_name: r.doc_name || "Untitled document",
              doc_path: r.doc_path,
              client: r.client,
              similarity: r.similarity,
              text: r.text,
            };
            return d;
          }
          // r.source is "session" or undefined (older backend predating
          // the field) — both are session hits.
          const s: SemanticMatch = {
            kind: "semantic",
            session_id: r.session_id,
            display_name: r.display_name || "Untitled",
            date: r.started_at
              ? new Date(r.started_at).toLocaleDateString()
              : "",
            similarity: r.similarity,
            start_s: r.start_s,
            end_s: r.end_s,
            text: r.text,
          };
          return s;
        });
        setMatches(results);
        // The semantic endpoint doesn't report corpus size; the index
        // status endpoint does. Best-effort, never blocks the results.
        api.searchIndexStatus()
          .then((st) => {
            if (fresh()) setStatus({ state: "ok", total: st.indexed_sessions });
          })
          .catch(() => { if (fresh()) setStatus({ state: "ok", total: 0 }); });
        return;
      }

      // ── Keyword mode ───────────────────────────────────────────────
      // Scoped against the session list loaded once on mount, so this
      // costs nothing and can run on every keystroke. (The list is
      // refetched whenever the view mounts, i.e. on every nav switch.)
      const scoped = sessions.filter((s) => {
        if (client && s.client !== client) return false;
        if (project && s.project !== project) return false;
        return true;
      });
      const re = new RegExp(escapeRegExp(q), "i");
      const results: FulltextMatch[] = [];
      const unmatched: SessionSummary[] = [];
      for (const s of scoped) {
        const hay = [
          s.display_name,
          s.client || "",
          s.project || "",
          s.summary || "",
          s.action_items || "",
          s.decisions || "",
          s.requirements || "",
        ].join("\n");
        const m = re.exec(hay);
        if (m) {
          results.push({
            kind: "fulltext",
            session_id: s.session_id,
            display_name: s.display_name,
            date: s.started_at
              ? new Date(s.started_at).toLocaleDateString()
              : "",
            snippet: makeSnippet(hay, m.index, q.length),
            field: "metadata",
          });
        } else if (s.has_transcript) {
          unmatched.push(s);
        }
      }
      if (!fresh()) return;
      // Paint metadata hits immediately; the transcript pass (if the
      // user asked for it) streams in behind them.
      setMatches(results);
      if (!deepScan) {
        setStatus({ state: "ok", total: scoped.length });
        return;
      }
      const deep = await Promise.all(
        unmatched.map(async (s): Promise<FulltextMatch | null> => {
          const transcript = await getTranscript(s.session_id);
          if (!transcript) return null;
          const hit = re.exec(transcript);
          if (!hit) return null;
          return {
            kind: "fulltext",
            session_id: s.session_id,
            display_name: s.display_name,
            date: s.started_at
              ? new Date(s.started_at).toLocaleDateString()
              : "",
            snippet: makeSnippet(transcript, hit.index, q.length),
            field: "transcript",
          };
        })
      );
      if (!fresh()) return;
      setMatches([...results, ...deep.filter((r): r is FulltextMatch => !!r)]);
      setStatus({ state: "ok", total: scoped.length });
    } catch (e) {
      if (!fresh()) return;
      // Inline, not a toast: search re-runs as you type, and a toast per
      // keystroke would be unusable. A failed call has to stay visually
      // distinct from an honest zero-result set.
      setMatches([]);
      setStatus({
        state: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }, [mode, client, project, deepScan, sessions, getTranscript]);

  // Debounced auto-search. Note the setTimeout: every state update runs
  // inside the timer callback, never synchronously in the effect body.
  useEffect(() => {
    const q = query.trim();
    const id = setTimeout(() => { void runSearch(q); }, q ? DEBOUNCE_MS[mode] : 0);
    return () => clearTimeout(id);
  }, [query, mode, runSearch]);

  // ── The one and only LLM entry point ────────────────────────────────
  // Called from click handlers exclusively. Never from an effect.
  const askAnswer = (raw: string) => {
    const q = raw.trim();
    if (!q || isStreaming) return;

    const turnId = `${Date.now()}-${turns.length}`;
    setTurns((prev) => [...prev, {
      id: turnId,
      question: q,
      sources: [],
      answer: "",
      status: "streaming",
      error: null,
    }]);

    const patch = (fn: (t: Turn) => Turn) =>
      setTurns((prev) => prev.map((t) => (t.id === turnId ? fn(t) : t)));

    abortRef.current = api.qaStream(
      {
        query: q,
        top_k: 8,
        // Same scope the results below were filtered with.
        client: client || undefined,
        project: project || undefined,
      },
      {
        onSources: (sources) => patch((t) => ({ ...t, sources })),
        onText: (fragment) =>
          patch((t) => ({ ...t, answer: t.answer + fragment })),
        onDone: () => {
          patch((t) => ({ ...t, status: "done" }));
          abortRef.current = null;
        },
        onError: (msg) => {
          patch((t) => ({ ...t, status: "error", error: msg }));
          abortRef.current = null;
          toast.error(`Answer failed: ${msg}`);
        },
      },
    );
  };

  const stopAnswer = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setTurns((prev) => prev.map((t, i) =>
      i === prev.length - 1 && t.status === "streaming"
        ? { ...t, status: "done" }
        : t,
    ));
  };

  // Cancel any in-flight stream if the view unmounts (nav switch)
  // rather than leaving a paid-for response streaming into nothing.
  useEffect(() => () => abortRef.current?.abort(), []);

  const trimmed = query.trim();
  const suggestAnswer = !!trimmed
    && !isStreaming
    && looksLikeQuestion(trimmed)
    && turns[turns.length - 1]?.question !== trimmed;

  const enableDeepScan = () => {
    setDeepScan(true);
    // runSearch's identity changes with deepScan, so the debounce effect
    // re-runs on its own — nothing else to kick here.
  };

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      {/* One-line orientation. Search and Ask used to be separate tabs
          with duelling explainer banners telling you to go use the
          other one; the distinction that actually matters now is
          free-and-instant vs. costs-an-LLM-call. */}
      <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
        <strong className="text-foreground">
          One search across every meeting and document.
        </strong>{" "}
        Results appear as you type — free, no AI call. Hit{" "}
        <strong className="text-foreground">Answer</strong> when you want
        Claude to read the matches and write a cited answer instead of
        making you read them yourself.
      </div>

      {/* Query row. Enter searches (it already has, as you typed);
          ⌘/Ctrl+Enter is the keyboard shortcut for an answer. */}
      <div className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              askAnswer(query);
            }
          }}
          placeholder={
            client
              ? `Search or ask about ${client}…`
              : "Search your meetings, or ask a question…"
          }
          className="flex-1"
        />
        {isStreaming ? (
          <Button onClick={stopAnswer} variant="destructive">
            <Square className="h-4 w-4" />
            <span className="ml-1.5">Stop</span>
          </Button>
        ) : (
          <Button
            onClick={() => askAnswer(query)}
            disabled={!trimmed}
            title="Send this query to Claude for a written, cited answer. One LLM call."
          >
            <Sparkles className="h-4 w-4" />
            <span className="ml-1.5">
              {turns.length > 0 ? "Ask follow-up" : "Answer"}
            </span>
          </Button>
        )}
      </div>

      {/* Controls row: result mode, scope, and (keyword only) the
          transcript deep-scan opt-in. Everything that changes what the
          view does lives on one line. */}
      <QueryControls
        mode={mode}
        onModeChange={setMode}
        client={client}
        project={project}
        clients={clients}
        projects={projects}
        onClientChange={selectClient}
        onProjectChange={(v) => setProject(v === "__all__" || !v ? "" : v)}
        onClearScope={() => { setClient(""); setProject(""); }}
        deepScan={deepScan}
        onDeepScanChange={setDeepScan}
      />

      {/* Inline suggestion. It offers; it does not fire. */}
      {suggestAnswer && (
        <button
          onClick={() => askAnswer(query)}
          className="flex w-full items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-left text-xs transition-colors hover:bg-primary/10"
        >
          <Sparkles className="h-3.5 w-3.5 text-primary shrink-0" />
          <span className="text-foreground">
            That reads like a question — want Claude to answer it from
            these results?
          </span>
          <span className="ml-auto text-[11px] font-medium text-primary shrink-0">
            Answer →
          </span>
        </button>
      )}

      {/* Answer above results: it's the thing you explicitly asked for,
          and it cites the results underneath it. */}
      {turns.length > 0 && (
        <AnswerThread
          turns={turns}
          onOpenSession={onOpenSession}
          onStop={stopAnswer}
          onClear={() => setTurns([])}
        />
      )}

      <div className="flex items-center gap-2 min-h-4">
        {status.state === "searching" && (
          <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
        )}
        {status.state === "ok" && matches.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {matches.length}{" "}
            {mode === "semantic"
              ? plural(matches.length, "chunk")
              : plural(matches.length, "match", "matches")}
            {status.total > 0
              ? ` across ${status.total} ${plural(status.total, "session")}`
              : ""}
          </p>
        )}
      </div>

      <ResultList
        matches={matches}
        status={status}
        mode={mode}
        query={trimmed}
        deepScan={deepScan}
        onOpenSession={onOpenSession}
        onRetry={() => { void runSearch(trimmed); }}
        onDeepScan={enableDeepScan}
      />
    </div>
  );
}

function makeSnippet(text: string, idx: number, qLen: number): string {
  const before = Math.max(0, idx - 80);
  const after = Math.min(text.length, idx + qLen + 80);
  return (before > 0 ? "…" : "")
    + text.slice(before, after).replace(/\s+/g, " ").trim()
    + (after < text.length ? "…" : "");
}

function plural(n: number, one: string, many = `${one}s`): string {
  return n === 1 ? one : many;
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
