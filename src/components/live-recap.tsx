"use client";

// The Recap button's result on the record screen: what has happened on
// the call so far, from every line the live transcript has caught, for
// the people on the call to read out and agree on before it ends. The
// newest recap replaces the last. It is kept on the session, so it is
// also on the meeting's Overview afterwards. It changes nothing else —
// the summary after the call is still made from the full recording.

import { useCallback, useEffect, useState } from "react";
import { Check, Copy, ListChecks, Loader2, RefreshCw, X } from "lucide-react";
import { toast } from "sonner";
import { api, type LiveRecap } from "@/lib/api";
import { callClock, parseRecap } from "@/lib/recap";
import { Button } from "@/components/ui/button";

function isRecap(value: unknown): value is LiveRecap {
  return !!value && typeof (value as LiveRecap).recap === "string"
    && (value as LiveRecap).recap.length > 0;
}

/** State behind the Recap button and its panel. */
export function useLiveRecap(recording: boolean) {
  const [recap, setRecap] = useState<LiveRecap | null>(null);
  const [busy, setBusy] = useState(false);
  const [hidden, setHidden] = useState(false);

  // Coming back to the record screen mid-call shows the last recap.
  useEffect(() => {
    if (!recording) return;
    let cancelled = false;
    api.latestRecordingRecap()
      .then((r) => { if (!cancelled && isRecap(r)) setRecap(r); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [recording]);

  const run = useCallback(async () => {
    setBusy(true);
    setHidden(false);
    try {
      setRecap(await api.recordingRecap());
    } catch (e) {
      toast.error("Recap failed", {
        description: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }, []);

  return { recap: recording ? recap : null, busy, run, hidden, setHidden };
}

/** Headings and bullets, as the recap is meant to be read. */
export function RecapBody({ markdown }: { markdown: string }) {
  const blocks = parseRecap(markdown);
  const out: React.ReactNode[] = [];
  let list: string[] = [];
  const flush = () => {
    if (list.length) {
      out.push(
        <ul key={`ul-${out.length}`} className="ml-4 list-disc space-y-1">
          {list.map((t, i) => <li key={i}>{t}</li>)}
        </ul>,
      );
      list = [];
    }
  };
  for (const b of blocks) {
    if (b.kind === "bullet") { list.push(b.text); continue; }
    flush();
    out.push(b.kind === "heading"
      ? <h4 key={`h-${out.length}`} className="mt-3 first:mt-0 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{b.text}</h4>
      : <p key={`p-${out.length}`}>{b.text}</p>);
  }
  flush();
  return <div className="space-y-1.5 text-sm leading-relaxed">{out}</div>;
}

function CopyRecap({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Couldn't copy — select the text instead.");
    }
  };
  return (
    <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={copy}
      title="Copy the recap, e.g. to paste into the meeting chat">
      {copied ? <Check className="h-3.5 w-3.5 mr-1" /> : <Copy className="h-3.5 w-3.5 mr-1" />}
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

export function LiveRecapPanel({
  recap, busy, onRefresh, onHide,
}: {
  recap: LiveRecap | null;
  busy: boolean;
  onRefresh: () => void;
  onHide: () => void;
}) {
  if (!recap && !busy) return null;
  return (
    <section
      aria-label="Recap of the call so far"
      className="rounded-lg border bg-card px-4 py-3 shadow-sm"
    >
      <div className="mb-2 flex items-center gap-2">
        <ListChecks className="h-4 w-4 text-primary" />
        <h3 className="text-sm font-semibold">
          {recap ? `Recap as of ${callClock(recap.as_of_s)}` : "Recapping the call so far…"}
        </h3>
        {recap && (
          <span className="text-xs text-muted-foreground">
            {recap.lines.toLocaleString()} lines
          </span>
        )}
        <div className="ml-auto flex items-center gap-1">
          {recap && <CopyRecap text={recap.recap} />}
          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs"
            onClick={onRefresh} disabled={busy} title="Recap again, up to now">
            {busy ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
              : <RefreshCw className="h-3.5 w-3.5 mr-1" />}
            Refresh
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7"
            onClick={onHide} aria-label="Hide the recap">
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
      {recap ? (
        <div className={busy ? "opacity-60" : undefined}>
          <RecapBody markdown={recap.recap} />
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          Reading every line so far — this can take up to a minute on a long call.
        </p>
      )}
    </section>
  );
}
