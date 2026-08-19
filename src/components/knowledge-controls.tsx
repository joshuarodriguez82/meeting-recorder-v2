"use client";

import { ScrollText, Sparkles, Type } from "lucide-react";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

// The one row of controls under the Knowledge Base query box: which
// kind of results you get, what they're scoped to, and (keyword only)
// whether transcript bodies are in play. Presentational — every piece
// of state lives in knowledge-view.tsx, because scope in particular has
// to reach the search request and the answer request identically.

export type Mode = "keyword" | "semantic";

export function QueryControls({
  mode, onModeChange,
  client, project, clients, projects,
  onClientChange, onProjectChange, onClearScope,
  deepScan, onDeepScanChange,
}: {
  mode: Mode;
  onModeChange: (m: Mode) => void;
  client: string;
  project: string;
  clients: string[];
  projects: string[];
  onClientChange: (v: string | null) => void;
  onProjectChange: (v: string | null) => void;
  onClearScope: () => void;
  deepScan: boolean;
  onDeepScanChange: (v: boolean) => void;
}) {
  return (
    <>
      <div className="flex items-center gap-3 text-xs flex-wrap">
        <div className="flex items-center gap-1 rounded-lg border bg-muted/30 p-1">
          <ModeButton
            active={mode === "keyword"}
            onClick={() => onModeChange("keyword")}
            icon={<Type className="h-3.5 w-3.5 mr-1.5" />}
            label="Keyword"
          />
          <ModeButton
            active={mode === "semantic"}
            onClick={() => onModeChange("semantic")}
            icon={<Sparkles className="h-3.5 w-3.5 mr-1.5" />}
            label="Semantic"
          />
        </div>

        <span className="text-muted-foreground">Scope:</span>
        <Select value={client || "__all__"} onValueChange={onClientChange}>
          <SelectTrigger className="h-7 w-44 text-xs">
            {/* Format the trigger label from the value rather than
                letting Base UI look it up from the item list: a Select
                that mounts mid-interaction (the project one below,
                which only appears once a client is picked) hasn't
                registered its items yet and renders the raw sentinel
                "__all__" on screen. */}
            <SelectValue>
              {(v) => (!v || v === "__all__" ? "All clients" : String(v))}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__all__">All clients</SelectItem>
            {clients.map((c) => (
              <SelectItem key={c} value={c}>{c}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {client && projects.length > 0 && (
          <Select value={project || "__all__"} onValueChange={onProjectChange}>
            <SelectTrigger className="h-7 w-44 text-xs">
              <SelectValue>
                {(v) => (!v || v === "__all__" ? "All projects" : String(v))}
              </SelectValue>
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
            onClick={onClearScope}
            className="text-[11px] text-muted-foreground hover:text-foreground underline"
          >
            Clear
          </button>
        )}

        {mode === "keyword" && (
          <button
            onClick={() => onDeepScanChange(!deepScan)}
            aria-pressed={deepScan}
            title={
              "Also regex-scan full transcript bodies. Fetches every "
              + "unmatched session's transcript once, then caches it."
            }
            className={
              "flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] transition-colors "
              + (deepScan
                ? "border-primary/40 bg-primary/10 text-primary"
                : "text-muted-foreground hover:text-foreground")
            }
          >
            <ScrollText className="h-3 w-3" />
            Search inside transcripts
          </button>
        )}
      </div>

      <p className="text-[11px] text-muted-foreground">
        {mode === "semantic" ? (
          <>
            Semantic ranks chunks by meaning, not keyword — slower (~1s),
            but it finds matches that share zero words with your query,
            and it covers Knowledge Folder documents as well as sessions.
            Only indexed content is searchable.
          </>
        ) : (
          <>
            Keyword regex-matches titles, summaries, action items,
            decisions and requirements —{" "}
            <strong>sessions only</strong>, Knowledge Folder documents
            aren&apos;t included. Instant and exact.
          </>
        )}{" "}
        Answers always retrieve semantically, whichever mode the results
        are in, and cost one LLM call (~$0.005) each.
      </p>
    </>
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
