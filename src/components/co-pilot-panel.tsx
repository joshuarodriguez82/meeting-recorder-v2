"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Loader2, Pause, Play, RefreshCw, Sparkles, Copy, Check, X,
  Save, CheckSquare, Lightbulb, StickyNote, Undo2, Send, ChevronDown,
  ChevronRight,
} from "lucide-react";
import { toast } from "sonner";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  api, ApiError,
  type CoPilotBoardItem, type CoPilotKind, type CoPilotQA, type CoPilotState,
  type CoPilotStatus,
} from "@/lib/api";
import { boardView, errorMessage, statusLine } from "@/lib/copilot-board";
import { Button } from "@/components/ui/button";

// Live Co-Pilot panel.
//
// The Co-Pilot runs in the BACKEND now (backend/services/copilot_runner.py):
// it ticks on the configured interval while a recording is in progress,
// whichever tab is open. It used to tick from this panel, so leaving the
// Record tab stopped coaching altogether — and with it the observations
// the post-meeting summary reads.
//
// This panel reads GET /recording/copilot/state every few seconds (no
// model call — cheap) and shows ONE board: each suggestion once, with
// the user's verdict on it. Repeats merge into the existing entry;
// marking an item done or dismissed sticks, and the model is told about
// both so it stops raising them. "Ask" answers a question about the call
// from the live transcript.

const STATE_POLL_MS = 3000;

interface Props {
  recording: boolean;
  enabled: boolean;
}

type SaveKind = "follow_up" | "decision" | "note";
const DEFAULT_SAVE: Record<CoPilotKind, SaveKind> = {
  clarifying_questions: "follow_up",
  risks: "decision",
  follow_ups: "follow_up",
};

export function CoPilotPanel({ recording, enabled }: Props) {
  const [state, setState] = useState<CoPilotState | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [showHandled, setShowHandled] = useState(false);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [activeMode, setActiveMode] = useState<string>("SA");
  const [activeType, setActiveType] = useState<string>("General");
  const [modes, setModes] = useState<string[]>([]);
  const [meetingTypes, setMeetingTypes] = useState<string[]>([]);
  const polling = useRef(false);

  const load = useCallback(async () => {
    if (polling.current) return;
    polling.current = true;
    try {
      setState(await api.copilotState());
    } catch {
      // Backend restarting or between recordings — keep what's shown.
    } finally {
      polling.current = false;
    }
  }, []);

  useEffect(() => {
    if (!recording || !enabled) return;
    void load();
    const id = setInterval(() => void load(), STATE_POLL_MS);
    return () => clearInterval(id);
  }, [recording, enabled, load]);

  // Persona + meeting-type libraries, and the current choice.
  useEffect(() => {
    if (!recording || !enabled) return;
    let cancelled = false;
    (async () => {
      try {
        const [m, t, s] = await Promise.all([
          api.getCopilotModes(),
          api.getCopilotMeetingTypes(),
          api.getSettings(),
        ]);
        if (cancelled) return;
        setModes(m.map((x) => x.name));
        setMeetingTypes(t.map((x) => x.name));
        if (s.live_copilot_mode) setActiveMode(s.live_copilot_mode);
        if (s.live_copilot_meeting_type) setActiveType(s.live_copilot_meeting_type);
      } catch {
        // Dropdowns fall back to the current choice only.
      }
    })();
    return () => { cancelled = true; };
  }, [recording, enabled]);

  const changeMode = async (next: string | null) => {
    if (!next || next === activeMode) return;
    const prev = activeMode;
    setActiveMode(next);
    try {
      await api.setCopilotActive(next, undefined);
      toast.success(`Co-Pilot persona: ${next}`);
    } catch (e) {
      setActiveMode(prev);
      toast.error(`Couldn't change persona: ${e instanceof Error ? e.message : e}`);
    }
  };

  const changeType = async (next: string | null) => {
    if (!next || next === activeType) return;
    const prev = activeType;
    setActiveType(next);
    try {
      await api.setCopilotActive(undefined, next);
      toast.success(`Meeting type: ${next}`);
    } catch (e) {
      setActiveType(prev);
      toast.error(`Couldn't change meeting type: ${e instanceof Error ? e.message : e}`);
    }
  };

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await api.copilotTick();
      const added = r.clarifying_questions.length + r.risks.length
        + r.follow_ups.length;
      if (!added && !r.error) toast.message("Nothing new to add right now.");
      await load();
    } catch (e) {
      if (!(e instanceof ApiError && (e.status === 403 || e.status === 409))) {
        toast.error(e instanceof Error ? e.message : "Refresh failed");
      }
    } finally {
      setRefreshing(false);
    }
  };

  const togglePause = async () => {
    const next = !(state?.paused ?? false);
    try {
      await api.copilotPause(next);
      await load();
    } catch (e) {
      toast.error(`Couldn't ${next ? "pause" : "resume"}: ${e instanceof Error ? e.message : e}`);
    }
  };

  const setStatus = async (item: CoPilotBoardItem, status: CoPilotStatus) => {
    // Optimistic: the board is the user's own list, the click should land.
    setState((s) => s && {
      ...s,
      board: s.board.map((i) => (i.id === item.id ? { ...i, status, fresh: false } : i)),
    });
    try {
      await api.copilotSetItem(item.id, status);
    } catch (e) {
      toast.error(`Couldn't update: ${e instanceof Error ? e.message : e}`);
      void load();
    }
  };

  const save = async (item: CoPilotBoardItem, kind: SaveKind) => {
    try {
      await api.saveCopilotSuggestion(kind, item.text);
      toast.success(`Saved as ${kind === "follow_up" ? "follow-up" : kind}`);
      await setStatus(item, "saved");
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const ask = async () => {
    const q = question.trim();
    if (!q || asking) return;
    setAsking(true);
    try {
      await api.copilotAsk(q);
      setQuestion("");
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "The Co-Pilot didn't answer");
    } finally {
      setAsking(false);
    }
  };

  if (!recording || !enabled) return null;

  const view = boardView(state?.board ?? []);
  const error = errorMessage(state?.error);
  const qa: CoPilotQA[] = [...(state?.qa ?? [])].reverse();

  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium flex-wrap">
        <Sparkles className="h-4 w-4 text-primary" />
        Co-Pilot
        {view.openCount > 0 && (
          <span className="rounded-full bg-primary/10 px-1.5 text-[10px] text-primary">
            {view.openCount} open
          </span>
        )}
        <Select value={activeMode} onValueChange={changeMode}>
          <SelectTrigger className="h-7 w-32 text-xs" title="Co-Pilot persona">
            <SelectValue placeholder="Persona" />
          </SelectTrigger>
          <SelectContent>
            {(modes.length ? modes : [activeMode]).map((m) => (
              <SelectItem key={m} value={m}>{m}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={activeType} onValueChange={changeType}>
          <SelectTrigger className="h-7 w-40 text-xs" title="Meeting type">
            <SelectValue placeholder="Meeting type" />
          </SelectTrigger>
          <SelectContent>
            {(meetingTypes.length ? meetingTypes : [activeType]).map((t) => (
              <SelectItem key={t} value={t}>{t}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="sm" onClick={() => void refresh()}
            disabled={refreshing || state?.paused}
            title="Check the conversation now">
            {refreshing
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <RefreshCw className="h-3.5 w-3.5" />}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => void togglePause()}
            title={state?.paused ? "Resume" : "Pause"}>
            {state?.paused
              ? <Play className="h-3.5 w-3.5" />
              : <Pause className="h-3.5 w-3.5" />}
          </Button>
        </div>
      </div>

      <form
        className="flex items-center gap-2"
        onSubmit={(e) => { e.preventDefault(); void ask(); }}
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about this call — e.g. what did they say about the timeline?"
          className="h-8 flex-1 rounded-md border bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          maxLength={1000}
          aria-label="Ask the Co-Pilot about this call"
        />
        <Button type="submit" size="sm" variant="secondary"
          disabled={asking || !question.trim()} aria-label="Ask">
          {asking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
        </Button>
      </form>
      {qa.length > 0 && (
        <div className="space-y-2">
          {qa.slice(0, 3).map((x) => (
            <div key={x.asked_at + x.question} className="rounded-md bg-muted/40 p-2 text-sm">
              <p className="text-xs font-medium text-muted-foreground">{x.question}</p>
              <p className="mt-1 whitespace-pre-wrap leading-snug">{x.answer}</p>
            </div>
          ))}
        </div>
      )}

      <div className="max-h-[28rem] overflow-y-auto pr-1 space-y-3">
        {view.openCount === 0 ? (
          <p className="text-xs italic text-muted-foreground py-2">
            {state?.last_tick_at
              ? "Nothing to act on right now — new suggestions appear here as the conversation moves."
              : "Listening — the first suggestions appear after the first update."}
          </p>
        ) : (
          view.open.map((group) => (
            <div key={group.kind} className="space-y-1">
              <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
                {group.title}
              </p>
              <ul className="space-y-1">
                {group.items.map((item) => (
                  <BoardRow key={item.id} item={item}
                    onStatus={(s) => void setStatus(item, s)}
                    onSave={(k) => void save(item, k)} />
                ))}
              </ul>
            </div>
          ))
        )}

        {view.handled.length > 0 && (
          <div className="border-t pt-2">
            <button type="button"
              onClick={() => setShowHandled((v) => !v)}
              className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
              {showHandled ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              Handled ({view.handled.length})
            </button>
            {showHandled && (
              <ul className="mt-1 space-y-1">
                {view.handled.map((item) => (
                  <li key={item.id} className="flex items-start gap-2 text-xs text-muted-foreground">
                    <span className="mt-0.5 w-16 shrink-0 uppercase tracking-wide text-[9px]">
                      {item.status}
                    </span>
                    <span className={`flex-1 ${item.status === "dismissed" ? "line-through" : ""}`}>
                      {item.text}
                    </span>
                    <button type="button" title="Put back on the board"
                      aria-label="Undo"
                      onClick={() => void setStatus(item, "open")}
                      className="rounded p-0.5 hover:bg-muted hover:text-foreground">
                      <Undo2 className="h-3 w-3" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      <div className="flex items-start justify-between gap-2 text-[10px] text-muted-foreground">
        <span>
          {statusLine(state)}
          {state?.segment_count ? ` · ${state.segment_count} recent segments` : ""}
        </span>
        {error && (
          <span className="text-right text-amber-600 dark:text-amber-400" title={state?.error_detail ?? ""}>
            {error}
          </span>
        )}
      </div>
    </div>
  );
}

function BoardRow({
  item, onStatus, onSave,
}: {
  item: CoPilotBoardItem;
  onStatus: (s: CoPilotStatus) => void;
  onSave: (k: SaveKind) => void;
}) {
  const def = DEFAULT_SAVE[item.kind];
  return (
    <li className="group flex items-start gap-2 text-sm leading-snug">
      <span className="mt-0.5 select-none text-muted-foreground">•</span>
      <span className="flex-1">
        {item.text}
        {item.fresh && (
          <span className="ml-1.5 rounded bg-primary/10 px-1 text-[9px] uppercase tracking-wide text-primary">
            new
          </span>
        )}
        {item.times_suggested > 1 && (
          <span className="ml-1.5 text-[10px] text-muted-foreground"
            title="The Co-Pilot keeps coming back to this">
            raised {item.times_suggested}×
          </span>
        )}
      </span>
      <span className="flex items-center gap-0.5 opacity-60 group-hover:opacity-100 focus-within:opacity-100">
        <IconButton label={item.kind === "clarifying_questions" ? "Asked" : "Done"}
          onClick={() => onStatus("done")}>
          <Check className="h-3 w-3" />
        </IconButton>
        <DropdownMenu>
          <DropdownMenuTrigger aria-label="Save" title="Save as a follow-up, decision or note"
            className="inline-flex h-5 w-5 items-center justify-center rounded border-0 bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground">
            <Save className="h-3 w-3" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            {([
              ["follow_up", "As follow-up", <CheckSquare key="f" className="mr-2 h-3.5 w-3.5 text-primary" />],
              ["decision", "As decision", <Lightbulb key="d" className="mr-2 h-3.5 w-3.5 text-amber-500" />],
              ["note", "To my notes", <StickyNote key="n" className="mr-2 h-3.5 w-3.5 text-muted-foreground" />],
            ] as [SaveKind, string, React.ReactNode][]).map(([k, label, icon]) => (
              <DropdownMenuItem key={k} onClick={() => onSave(k)}>
                {icon}
                {label}
                {def === k && (
                  <span className="ml-auto text-[9px] uppercase tracking-wide text-muted-foreground">
                    default
                  </span>
                )}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        <CopyButton text={item.text} />
        <IconButton label="Dismiss — not useful" onClick={() => onStatus("dismissed")}>
          <X className="h-3 w-3" />
        </IconButton>
      </span>
    </li>
  );
}

function IconButton({
  label, onClick, children,
}: { label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label}
      className="inline-flex h-5 w-5 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground">
      {children}
    </button>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const onClick = async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch (err) {
      toast.error(`Copy failed: ${err instanceof Error ? err.message : err}`);
    }
  };
  return (
    <IconButton label="Copy" onClick={() => void onClick()}>
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
    </IconButton>
  );
}
