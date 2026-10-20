"use client";

// Notes during the call, in a panel that slides out over the record
// screen from a tab on its right edge, so it takes no room from the
// transcript and Co-Pilot until it's wanted.
//
// What's typed goes onto the recording's own session as you type: the
// same Notes the meeting shows afterwards, which the summary reads as
// context and Co-Pilot reads while it coaches. It is saved to the
// recording in progress (PUT /recording/notes), not to the file on
// disk, because Stop writes the recording over that file.

import { useCallback, useEffect, useRef, useState } from "react";
import { Clock, Loader2, NotebookPen, X } from "lucide-react";
import { api } from "@/lib/api";
import { insertStamp } from "@/lib/recap";
import { Button } from "@/components/ui/button";

const SAVE_AFTER_MS = 600;

type SaveState = "idle" | "saving" | "saved" | "error";

export function LiveNotesDrawer({
  recording, elapsedS, sessionId,
}: {
  recording: boolean;
  /** Seconds into the call, for the time stamp button. */
  elapsedS: number;
  /** The recording's session, to save anything pending after Stop. */
  sessionId: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [state, setState] = useState<SaveState>("idle");
  // What the server last had, as far as this panel knows — sent with
  // each save so a Co-Pilot "Save to my notes" appended meanwhile is
  // kept rather than overwritten.
  const base = useRef("");
  const pending = useRef<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const area = useRef<HTMLTextAreaElement | null>(null);
  const lastSession = useRef<string | null>(sessionId);

  useEffect(() => { if (sessionId) lastSession.current = sessionId; }, [sessionId]);

  const save = useCallback(async () => {
    const value = pending.current;
    if (value === null) return;
    pending.current = null;
    setState("saving");
    try {
      const res = await api.saveRecordingNotes(value, base.current);
      base.current = res.notes;
      if (res.notes !== value && pending.current === null) setText(res.notes);
      setState("saved");
    } catch {
      pending.current = pending.current ?? value;
      setState("error");
    }
  }, []);

  // Load what's there (Co-Pilot may have added to it) each time it opens.
  useEffect(() => {
    if (!recording || !open) return;
    let cancelled = false;
    api.recordingNotes()
      .then((r) => {
        if (cancelled || pending.current !== null) return;
        base.current = r.notes;
        setText(r.notes);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [recording, open]);

  // Stop pressed with a save still waiting: the recording is over, so
  // the notes go to the saved session instead.
  useEffect(() => {
    if (recording) return;
    if (timer.current) clearTimeout(timer.current);
    const value = pending.current;
    const sid = lastSession.current;
    pending.current = null;
    setOpen(false);
    if (value !== null && sid) {
      api.patchSession(sid, { notes: value }).catch(() => {});
    }
  }, [recording]);

  const change = (value: string) => {
    setText(value);
    pending.current = value;
    setState("idle");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(save, SAVE_AFTER_MS);
  };

  const stamp = () => {
    const el = area.current;
    const caret = el ? el.selectionStart ?? text.length : text.length;
    const out = insertStamp(text, caret, elapsedS);
    change(out.text);
    requestAnimationFrame(() => {
      if (!area.current) return;
      area.current.focus();
      area.current.setSelectionRange(out.caret, out.caret);
    });
  };

  const close = () => {
    if (timer.current) clearTimeout(timer.current);
    void save();
    setOpen(false);
  };

  if (!recording) return null;

  return (
    <>
      {!open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="fixed right-0 top-1/3 z-40 flex items-center gap-1.5 rounded-l-md border border-r-0 bg-card px-2 py-3 text-xs font-medium shadow-md hover:bg-muted [writing-mode:vertical-rl]"
          aria-label="Open call notes"
        >
          <NotebookPen className="h-3.5 w-3.5 rotate-90" />
          Notes
        </button>
      )}
      {open && (
        <aside
          aria-label="Call notes"
          className="fixed inset-y-0 right-0 z-50 flex w-[min(26rem,100vw)] flex-col border-l bg-background shadow-2xl"
        >
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <NotebookPen className="h-4 w-4 text-primary" />
            <h2 className="text-sm font-semibold">Call notes</h2>
            <span className="text-xs text-muted-foreground" aria-live="polite">
              {state === "saving" && <Loader2 className="inline h-3 w-3 animate-spin" />}
              {state === "saved" && "Saved"}
              {state === "error" && "Not saved — retrying when you type"}
            </span>
            <div className="ml-auto flex items-center gap-1">
              <Button variant="outline" size="sm" className="h-7 px-2 text-xs"
                onClick={stamp} title="Insert the current time in the call">
                <Clock className="h-3.5 w-3.5 mr-1" />
                Insert time
              </Button>
              <Button variant="ghost" size="icon" className="h-7 w-7"
                onClick={close} aria-label="Close call notes">
                <X className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <textarea
            ref={area}
            value={text}
            onChange={(e) => change(e.target.value)}
            placeholder="Anything the transcript won't capture — context, decisions, who owns what. Saved to this meeting's Notes as you type; the summary reads it."
            className="flex-1 resize-none bg-background px-4 py-3 text-sm leading-relaxed outline-none"
            autoFocus
          />
        </aside>
      )}
    </>
  );
}
