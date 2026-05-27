"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api, type QASource, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Sparkles, Loader2, Square, MessageCircle, FileSearch } from "lucide-react";

// Cross-meeting Q&A view.
//
// Each turn is independent (no multi-turn conversation memory in v1) —
// the user asks a question, the backend retrieves top-K chunks via the
// semantic-search index, builds a citation-aware prompt, and streams
// the LLM's answer back via SSE. The answer arrives as text fragments
// that get appended to a single growing string per turn.
//
// Citations come back inline as `[ABC123 @ 12:34]`. We regex-detect
// those and render them as buttons that open the source session detail
// dialog at that timestamp (timestamp scrub UI is a follow-up — for
// now opening the session is enough to verify a claim).
//
// The scope filter (client / project) constrains which sessions get
// considered — useful when the same wording appears in many clients
// and you want to ask "what did we decide" within a single account.

type Turn = {
  id: string;             // unique per turn
  question: string;
  sources: QASource[];    // empty until backend's `sources` event arrives
  answer: string;         // accumulates as text fragments stream in
  status: "streaming" | "done" | "error";
  error: string | null;
  createdAt: number;
};

interface Props {
  // Open the source session in the detail dialog. Same handler the
  // search-view + sessions-view use, so click-to-jump is consistent.
  onOpenSession: (id: string) => void;
}

export function QAView({ onOpenSession }: Props) {
  const [query, setQuery] = useState("");
  const [client, setClient] = useState<string>("");
  const [project, setProject] = useState<string>("");
  const [turns, setTurns] = useState<Turn[]>([]);
  // Active stream handle so the user can cancel a long answer in flight.
  const abortRef = useRef<{ abort: () => void } | null>(null);
  // Auto-scroll the conversation pane to the bottom as text streams in,
  // unless the user has scrolled up to read older turns (mirrors the
  // live-transcript-panel UX).
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [autoStick, setAutoStick] = useState(true);

  // Pull the unique (client, project) options from the session list so
  // the user can scope queries without a separate clients endpoint.
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
  // makes no sense scoped to another.
  useEffect(() => { setProject(""); }, [client]);

  const isStreaming = turns.length > 0
    && turns[turns.length - 1].status === "streaming";

  const ask = () => {
    const q = query.trim();
    if (!q || isStreaming) return;
    setQuery("");
    setAutoStick(true);

    const turnId = String(Date.now());
    const newTurn: Turn = {
      id: turnId,
      question: q,
      sources: [],
      answer: "",
      status: "streaming",
      error: null,
      createdAt: Date.now(),
    };
    setTurns((prev) => [...prev, newTurn]);

    const handle = api.qaStream(
      {
        query: q,
        top_k: 8,
        client: client || undefined,
        project: project || undefined,
      },
      {
        onSources: (sources) => {
          setTurns((prev) => prev.map((t) =>
            t.id === turnId ? { ...t, sources } : t,
          ));
        },
        onText: (fragment) => {
          setTurns((prev) => prev.map((t) =>
            t.id === turnId ? { ...t, answer: t.answer + fragment } : t,
          ));
        },
        onDone: () => {
          setTurns((prev) => prev.map((t) =>
            t.id === turnId ? { ...t, status: "done" } : t,
          ));
          abortRef.current = null;
        },
        onError: (msg) => {
          setTurns((prev) => prev.map((t) =>
            t.id === turnId ? { ...t, status: "error", error: msg } : t,
          ));
          abortRef.current = null;
          toast.error(`Q&A failed: ${msg}`);
        },
      },
    );
    abortRef.current = handle;
  };

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setTurns((prev) => prev.map((t, i) =>
      i === prev.length - 1 && t.status === "streaming"
        ? { ...t, status: "done" }
        : t,
    ));
  };

  // Scroll-to-bottom behaviour. Same approach as the live transcript
  // panel: if the user is pinned within 80px of the bottom we follow
  // along; if they've scrolled up to read, we leave them alone.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const dist = el.scrollHeight - (el.scrollTop + el.clientHeight);
      setAutoStick(dist < 80);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    if (!autoStick) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [turns, autoStick]);

  return (
    <div className="mx-auto max-w-4xl flex flex-col h-full">
      {/* Top-of-view explainer — sibling to the one in search-view.tsx,
          drawing the same distinction so the user understands which
          tab to use for which job. */}
      <div className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground mb-4">
        <strong className="text-foreground">Ask generates an answer.</strong>{" "}
        Type a question; Claude reads matching chunks from your meeting
        history and writes a synthesized response with{" "}
        <span className="text-primary">[session]</span> citations you can
        click to jump to source.{" "}
        <span className="text-foreground/80">
          Just want the matching chunks themselves? Use{" "}
          <strong>Search</strong> — same retrieval, no LLM
          synthesis, faster, cheaper.
        </span>
      </div>

      {/* Scope filters — collapsed in their own row to leave the input
          a clean full width below. */}
      <div className="flex items-center gap-3 mb-3 text-xs flex-wrap">
        <Sparkles className="h-3.5 w-3.5 text-primary" />
        <span className="text-muted-foreground">Scope:</span>
        <Select
          value={client || "__all__"}
          onValueChange={(v) => setClient(v === "__all__" || !v ? "" : v)}
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

      {/* Conversation pane — fills the remaining viewport height with
          its own scroll, keeps the input pinned at the bottom. */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto rounded-lg border bg-card p-4 mb-3 space-y-6 min-h-[400px]"
      >
        {turns.length === 0 ? (
          <EmptyState />
        ) : (
          turns.map((t) => (
            <TurnView
              key={t.id}
              turn={t}
              onOpenSession={onOpenSession}
            />
          ))
        )}
      </div>

      {/* Input row */}
      <div className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }}
          placeholder={
            client
              ? `Ask anything about your meetings with ${client}…`
              : "Ask anything about your meetings…"
          }
          disabled={isStreaming}
          className="flex-1"
        />
        {isStreaming ? (
          <Button onClick={stop} variant="destructive">
            <Square className="h-4 w-4 mr-2" />
            Stop
          </Button>
        ) : (
          <Button onClick={ask} disabled={!query.trim()}>
            <MessageCircle className="h-4 w-4 mr-2" />
            Ask
          </Button>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground mt-2 italic">
        Answers are grounded in chunks retrieved from your indexed
        meetings. Each <code className="text-[10px]">[id @ mm:ss]</code> is
        clickable — opens the source session for verification. Sessions
        without an embedding aren&apos;t searchable; backfill in
        Settings → Semantic Index.
      </p>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center text-muted-foreground py-12">
      <FileSearch className="h-10 w-10 mb-3 opacity-40" />
      <p className="text-sm">
        Ask anything about your meetings — Claude searches your transcripts
        and answers with citations.
      </p>
      <p className="text-xs mt-2 max-w-md">
        Try: <em>&quot;What did we decide about pricing for ACME?&quot;</em> ·{" "}
        <em>&quot;Has Sarah committed to anything in the last three calls?&quot;</em>{" "}
        · <em>&quot;What open questions came up in the discovery sessions?&quot;</em>
      </p>
    </div>
  );
}

function TurnView({
  turn, onOpenSession,
}: {
  turn: Turn;
  onOpenSession: (id: string) => void;
}) {
  return (
    <div className="space-y-3">
      {/* User question */}
      <div className="flex gap-3">
        <div className="h-7 w-7 rounded-full bg-primary text-primary-foreground flex items-center justify-center text-xs shrink-0 font-medium">
          You
        </div>
        <div className="flex-1 text-sm pt-1">{turn.question}</div>
      </div>

      {/* Sources strip — shown as soon as the backend's `sources` event
          arrives, before the answer text starts streaming. Lets the user
          see what context Claude is reasoning over. */}
      {turn.sources.length > 0 && (
        <div className="ml-10 flex flex-wrap gap-1.5">
          {turn.sources.map((s, i) => (
            <button
              key={`${s.session_id}-${i}`}
              onClick={() => onOpenSession(s.session_id)}
              className="text-[11px] rounded-full border bg-muted/30 hover:bg-muted/60 transition-colors px-2 py-0.5 max-w-xs truncate"
              title={`${s.text.slice(0, 200)}…\n\n${Math.round(s.similarity * 100)}% match · click to open session`}
            >
              <span className="text-muted-foreground">📎</span>{" "}
              <span className="font-medium">{s.display_name || "Untitled"}</span>{" "}
              <span className="text-muted-foreground tabular-nums">
                {formatTime(s.start_s)}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Assistant answer */}
      <div className="flex gap-3">
        <div className="h-7 w-7 rounded-full bg-accent text-accent-foreground flex items-center justify-center text-xs shrink-0">
          <Sparkles className="h-3.5 w-3.5" />
        </div>
        <div className="flex-1 text-sm pt-1 break-words">
          {turn.status === "error" ? (
            <p className="text-destructive italic">{turn.error || "Failed."}</p>
          ) : (
            <AnswerWithCitations
              text={turn.answer}
              streaming={turn.status === "streaming"}
              onOpenSession={onOpenSession}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// Splits the streamed answer text into spans, turning every
// `[ABC123 @ 12:34]` citation into a click-to-jump button. We keep the
// regex permissive on the timestamp to handle "1:23" as well as "12:34".
const CITATION_RE = /\[([A-Za-z0-9]{4,16})\s+@\s+(\d+:\d{2})\]/g;

function AnswerWithCitations({
  text, streaming, onOpenSession,
}: {
  text: string;
  streaming: boolean;
  onOpenSession: (id: string) => void;
}) {
  if (!text) {
    return (
      <div className="flex items-center gap-2 text-muted-foreground italic">
        <Loader2 className="h-3 w-3 animate-spin" />
        Searching meetings…
      </div>
    );
  }
  // Walk the text, emitting plain spans + citation buttons.
  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  CITATION_RE.lastIndex = 0;
  while ((match = CITATION_RE.exec(text)) !== null) {
    if (match.index > lastIdx) {
      parts.push(text.slice(lastIdx, match.index));
    }
    const sid = match[1];
    const ts = match[2];
    parts.push(
      <button
        key={`${sid}-${ts}-${match.index}`}
        onClick={() => onOpenSession(sid)}
        className="inline-flex items-center px-1.5 mx-0.5 rounded bg-primary/10 hover:bg-primary/20 text-primary text-[11px] font-medium tabular-nums transition-colors align-baseline"
        title={`Open session ${sid} at ${ts}`}
      >
        {sid} · {ts}
      </button>
    );
    lastIdx = match.index + match[0].length;
  }
  if (lastIdx < text.length) parts.push(text.slice(lastIdx));
  return (
    <span className="whitespace-pre-wrap leading-relaxed">
      {parts}
      {streaming && (
        <span className="inline-block w-1 h-3 bg-primary/60 ml-0.5 animate-pulse align-baseline" />
      )}
    </span>
  );
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
