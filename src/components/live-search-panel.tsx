"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Search, Square } from "lucide-react";
import { api, type QASource } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

// In-call search panel.
//
// Two modes via a toggle:
//
//   "this call"   — instant text find through the LIVE transcript of
//                   the current recording. No Claude call, no API cost.
//                   Subscribes to /recording/transcript/stream and
//                   accumulates segments, then matches a substring.
//                   Covers the common case ("did anyone mention X yet?",
//                   "what was that company name?").
//
//   "past meetings" — semantic search across indexed history via
//                     /qa/stream. Streams a Claude answer with citations.
//                     Uses an API call per question.
//
// Default is "this call" because mid-meeting recall is the dominant
// in-call search use case and instant > smart for that.
//
// Why subscribe to SSE here instead of hoisting segments from
// LiveTranscriptPanel: the backend's pub/sub is a fan-out queue per
// subscriber, so two consumers cost almost nothing, and the components
// stay decoupled — neither has to know about the other's lifecycle.

const BACKEND = "http://127.0.0.1:17645";

type Segment = {
  start: number;
  end: number;
  text: string;
  speaker?: "you" | "them" | "room";
};

// Three search scopes:
//   "this-call"     → instant text find, no AI
//   "this-call-ai"  → Claude over the live transcript (current call only)
//   "past"          → Claude + retrieval over indexed history
type Mode = "this-call" | "this-call-ai" | "past";
type RemoteStatus = "idle" | "streaming" | "done" | "error";

export function LiveSearchPanel({ recording }: { recording: boolean }) {
  const [mode, setMode] = useState<Mode>("this-call");
  const [query, setQuery] = useState("");

  // ── Live transcript subscription (for "this call" mode) ─────────────
  const [segments, setSegments] = useState<Segment[]>([]);
  useEffect(() => {
    if (!recording) {
      setSegments([]);
      return;
    }
    const es = new EventSource(`${BACKEND}/recording/transcript/stream`);
    es.onmessage = (e) => {
      try {
        const seg: Segment = JSON.parse(e.data);
        setSegments((prev) => [...prev, seg]);
      } catch { /* ignore malformed */ }
    };
    es.addEventListener("done", () => es.close());
    es.onerror = () => es.close();
    return () => es.close();
  }, [recording]);

  // ── Past-meetings remote search ────────────────────────────────────
  const [submitted, setSubmitted] = useState<string>("");
  const [remoteAnswer, setRemoteAnswer] = useState("");
  const [remoteSources, setRemoteSources] = useState<QASource[]>([]);
  const [remoteStatus, setRemoteStatus] = useState<RemoteStatus>("idle");
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const abortRef = useRef<{ abort: () => void } | null>(null);

  // Reset per-recording so prior answers don't bleed in.
  useEffect(() => {
    if (recording) {
      setQuery("");
      setSubmitted("");
      setRemoteAnswer("");
      setRemoteSources([]);
      setRemoteStatus("idle");
      setRemoteError(null);
    }
  }, [recording]);

  // Abort an in-flight remote stream on unmount or stop.
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  if (!recording) return null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    if (mode === "this-call") {
      // Local find — no remote round-trip. Match state lives in
      // `segments` + `query`; the render block below filters.
      setSubmitted(q);
      return;
    }
    // Streaming modes → cancel any prior, kick off new.
    if (remoteStatus === "streaming") return;
    abortRef.current?.abort();
    setSubmitted(q);
    setRemoteAnswer("");
    setRemoteSources([]);
    setRemoteStatus("streaming");
    setRemoteError(null);
    if (mode === "this-call-ai") {
      // Inline mode: send the live transcript as context. Each
      // segment becomes one line "[YOU mm:ss] text" so Claude has
      // the speaker tag + rough timing without needing structured
      // chunks. Cap at the most recent ~8000 characters so a long
      // call doesn't blow past the context window — recency is what
      // people typically ask about ("what was that number she just
      // said?"). The full transcript still lands in the post-stop
      // canonical pass.
      const lines = segments.map((s) => {
        const tag =
          s.speaker === "you" ? "YOU"
            : s.speaker === "them" ? "THEM"
            : s.speaker === "room" ? "ROOM"
            : "?";
        const t = formatTime(s.start);
        return `[${tag} ${t}] ${s.text}`;
      });
      let context = lines.join("\n");
      const MAX_CONTEXT_CHARS = 8000;
      if (context.length > MAX_CONTEXT_CHARS) {
        context = "…\n" + context.slice(-MAX_CONTEXT_CHARS);
      }
      abortRef.current = api.qaInlineStream(
        { query: q, context },
        {
          onText: (t) => setRemoteAnswer((prev) => prev + t),
          onDone: () => setRemoteStatus("done"),
          onError: (m) => {
            setRemoteError(m);
            setRemoteStatus("error");
          },
        },
      );
      return;
    }
    // Past-meetings mode → /qa/stream (with retrieval).
    abortRef.current = api.qaStream(
      { query: q },
      {
        onSources: (s) => setRemoteSources(s),
        onText: (t) => setRemoteAnswer((prev) => prev + t),
        onDone: () => setRemoteStatus("done"),
        onError: (m) => {
          setRemoteError(m);
          setRemoteStatus("error");
        },
      },
    );
  };

  const cancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setRemoteStatus("done");
  };

  // Local matches for "this call" mode. Case-insensitive substring;
  // we don't do fuzzy matching here because the user's typing the
  // exact phrase they remember hearing — keep it predictable.
  const localMatches: Segment[] =
    mode === "this-call" && submitted
      ? segments.filter((s) =>
          s.text.toLowerCase().includes(submitted.toLowerCase()),
        )
      : [];

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <Search className="h-4 w-4 text-primary" />
        Search
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
          live
        </span>
        {/* Scope toggle. Two pill-buttons; clicking switches the active
            mode without losing the current query so the user can swap
            "this call" → "past" on the same question. */}
        <div className="ml-auto flex rounded-md border overflow-hidden text-[11px]">
          {(
            [
              ["this-call", "This call"],
              ["this-call-ai", "This call (AI)"],
              ["past", "Past meetings"],
            ] as const
          ).map(([key, label], i) => (
            <button
              key={key}
              type="button"
              onClick={() => setMode(key)}
              className={
                "px-2 py-0.5 transition-colors "
                + (i > 0 ? "border-l " : "")
                + (mode === key
                  ? "bg-primary text-primary-foreground"
                  : "bg-transparent text-muted-foreground hover:text-foreground")
              }
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <form onSubmit={submit} className="flex gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={
            mode === "this-call"
              ? "Find a phrase in the current call…"
              : mode === "this-call-ai"
                ? "Ask about what's been said in this call…"
                : "What did we tell Acme about pricing last quarter?"
          }
          disabled={mode !== "this-call" && remoteStatus === "streaming"}
          className="flex-1"
        />
        {mode !== "this-call" && remoteStatus === "streaming" ? (
          <Button type="button" variant="secondary" onClick={cancel}>
            <Square className="h-4 w-4 mr-1" />
            Stop
          </Button>
        ) : (
          <Button type="submit" disabled={!query.trim()}>
            <Search className="h-4 w-4 mr-1" />
            {mode === "this-call" ? "Find" : "Ask"}
          </Button>
        )}
      </form>

      <div className="min-h-[6rem] rounded-md bg-muted/40 p-3 text-sm leading-relaxed">
        {mode === "this-call" ? (
          // ── This-call mode: local matches ──────────────────────────
          !submitted ? (
            <p className="text-muted-foreground text-xs italic">
              Find a phrase from the live transcript instantly. No API
              call. Switch to <strong>Past meetings</strong> to ask
              Claude across your indexed history.
            </p>
          ) : localMatches.length === 0 ? (
            <p className="text-muted-foreground text-xs">
              No matches yet for &ldquo;{submitted}&rdquo; in this call.
              Live transcript has {segments.length} segment
              {segments.length === 1 ? "" : "s"} so far — try a shorter
              phrase, or wait for more speech.
            </p>
          ) : (
            <div className="space-y-1.5">
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {localMatches.length} match
                {localMatches.length === 1 ? "" : "es"} for &ldquo;
                {submitted}&rdquo;
              </p>
              {localMatches.map((seg, i) => (
                <div key={i} className="flex gap-3">
                  <span className="font-mono text-[10px] text-muted-foreground tabular-nums shrink-0 pt-1">
                    {formatTime(seg.start)}
                  </span>
                  {seg.speaker ? (
                    <span
                      className={
                        "shrink-0 px-1.5 rounded text-[10px] font-medium uppercase tracking-wide self-start mt-1 "
                        + (seg.speaker === "you"
                          ? "bg-primary/15 text-primary"
                          : "bg-muted-foreground/15 text-foreground/70")
                      }
                    >
                      {seg.speaker === "you" ? "You" : "Them"}
                    </span>
                  ) : null}
                  <span className="break-words">
                    {highlight(seg.text, submitted)}
                  </span>
                </div>
              ))}
            </div>
          )
        ) : (
          // ── AI modes (this-call-ai or past): remote answer ─────────
          <>
            {remoteStatus === "idle" && !remoteAnswer && (
              <p className="text-muted-foreground text-xs italic">
                {mode === "this-call-ai" ? (
                  <>
                    Ask Claude about what&apos;s been said in this call.
                    The recent live transcript is sent as context — no
                    semantic search, no citation links, just a synthesized
                    answer.
                  </>
                ) : (
                  <>
                    Search across your indexed meetings. Answers come back
                    with citations — no click-through during recording, but
                    you can revisit cited sessions when you stop.
                  </>
                )}
              </p>
            )}
            {submitted && (
              <p className="text-xs text-muted-foreground italic mb-2">
                &ldquo;{submitted}&rdquo;
              </p>
            )}
            {remoteAnswer && (
              <p className="whitespace-pre-wrap break-words">
                {remoteAnswer}
              </p>
            )}
            {remoteStatus === "streaming" && (
              <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground mt-2">
                <Loader2 className="h-3 w-3 animate-spin" />
                {mode === "this-call-ai"
                  ? "thinking about this call"
                  : "searching meetings"}
              </p>
            )}
            {remoteStatus === "error" && remoteError && (
              <p className="text-destructive text-xs mt-2">{remoteError}</p>
            )}
            {mode === "past" && remoteSources.length > 0 && (
              <div className="mt-3 pt-2 border-t border-border space-y-1">
                <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                  Sources
                </p>
                <ul className="space-y-0.5 text-[11px] text-muted-foreground">
                  {remoteSources.map((s) => (
                    <li
                      key={`${s.session_id}-${s.start_s}`}
                      className="truncate"
                    >
                      <span className="font-mono text-foreground/70">
                        [{s.session_id} @ {formatTime(s.start_s)}]
                      </span>{" "}
                      {s.display_name || "Untitled"}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// Wrap each occurrence of `needle` in <mark>. Case-insensitive. Splits
// on the lowercased needle so a query like "Pricing" still highlights
// "pricing", "PRICING", etc. while preserving the original casing of
// the segment text.
function highlight(text: string, needle: string): React.ReactNode {
  if (!needle) return text;
  const lower = text.toLowerCase();
  const target = needle.toLowerCase();
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    const idx = lower.indexOf(target, cursor);
    if (idx === -1) {
      parts.push(text.slice(cursor));
      break;
    }
    if (idx > cursor) parts.push(text.slice(cursor, idx));
    parts.push(
      <mark
        key={idx}
        className="bg-primary/25 text-foreground rounded px-0.5"
      >
        {text.slice(idx, idx + needle.length)}
      </mark>,
    );
    cursor = idx + needle.length;
  }
  return parts;
}
