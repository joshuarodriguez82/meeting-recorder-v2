"use client";

import { Loader2, Sparkles, Square, Trash2 } from "lucide-react";
import { type QASource } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card, CardAction, CardContent, CardHeader, CardTitle,
} from "@/components/ui/card";
import { formatTime } from "@/components/knowledge-results";

// The synthesized-answer half of the Knowledge Base view: the
// conversation thread, its source chips, and inline citation parsing.
// Presentational only — it never starts or stops a stream, it just
// renders the turns the container hands it and calls back for Stop /
// Clear. Keeping the LLM trigger out of this file is deliberate: an
// answer costs money, so exactly one place in the codebase gets to
// start one.

export type Turn = {
  id: string;             // unique per turn
  question: string;
  sources: QASource[];    // empty until backend's `sources` event arrives
  answer: string;         // accumulates as text fragments stream in
  status: "streaming" | "done" | "error";
  error: string | null;
};

export function AnswerThread({
  turns, onOpenSession, onStop, onClear,
}: {
  turns: Turn[];
  onOpenSession: (id: string) => void;
  onStop: () => void;
  onClear: () => void;
}) {
  const streaming = turns.length > 0
    && turns[turns.length - 1].status === "streaming";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" />
          Answer
        </CardTitle>
        {/* CardAction, not a flex sibling: CardHeader is a grid, and it
            only switches to the two-column [1fr_auto] layout when it
            sees a card-action child. */}
        <CardAction className="flex items-center gap-1.5">
          {streaming && (
            <Button variant="destructive" size="sm" onClick={onStop}>
              <Square className="h-3 w-3" />
              Stop
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={onClear}
            disabled={streaming}
            title="Clear the conversation. Search results stay put."
          >
            <Trash2 className="h-3 w-3" />
            Clear
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-6">
        {turns.map((t) => (
          <TurnView key={t.id} turn={t} onOpenSession={onOpenSession} />
        ))}
        <p className="text-[11px] text-muted-foreground italic border-t pt-3">
          Grounded in chunks retrieved from your indexed meetings and
          Knowledge Folder documents. Each{" "}
          <code className="text-[10px]">[id @ mm:ss]</code> is clickable —
          it opens the source session so you can verify the claim.
          Sessions without an embedding aren&apos;t searchable; backfill in
          Settings → Semantic Index.
        </p>
      </CardContent>
    </Card>
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
        <div className="flex-1 text-sm pt-1 break-words">{turn.question}</div>
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
        Reading your meetings…
      </div>
    );
  }
  // Splits the streamed answer text into spans, turning every
  // `[ABC123 @ 12:34]` citation into a click-to-jump button. The regex
  // is built per call rather than hoisted to module scope: a /g regex
  // carries mutable `lastIndex` state, and sharing one across streaming
  // renders means one component's partial scan can silently offset the
  // next one's. Permissive on the timestamp so "1:23" parses as well as
  // "12:34".
  const re = /\[([A-Za-z0-9]{4,16})\s+@\s+(\d+:\d{2})\]/g;
  const parts: React.ReactNode[] = [];
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
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
