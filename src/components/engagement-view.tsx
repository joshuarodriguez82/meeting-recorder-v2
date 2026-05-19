"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type SessionSummary,
  type EngagementRegister,
  type EngagementRecord,
} from "@/lib/api";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Loader2, FileSpreadsheet, RefreshCw } from "lucide-react";

interface Props {
  sessions: SessionSummary[];
}

const TERMINAL = new Set(["met", "dropped", "done", "answered"]);

const selectCls =
  "h-9 rounded-md border border-input bg-background px-3 text-sm " +
  "focus:outline-none focus:ring-2 focus:ring-ring";

function provenance(rec: EngagementRecord): string {
  const occ = rec.occurrences || [];
  if (occ.length === 0) return "—";
  const last = (occ[occ.length - 1]?.at || "").slice(0, 10);
  const names = occ
    .map((o) => o.display_name || o.session_id)
    .filter(Boolean);
  const uniq = Array.from(new Set(names));
  const shown = uniq.slice(0, 3).join(", ");
  const more = uniq.length > 3 ? ` +${uniq.length - 3} more` : "";
  return `${occ.length} session${occ.length === 1 ? "" : "s"} · last ${last} · ${shown}${more}`;
}

function StatusBadge({ status }: { status: string }) {
  const s = (status || "open").toLowerCase();
  const resolved = TERMINAL.has(s);
  return (
    <Badge variant={resolved ? "secondary" : "default"} className="capitalize">
      {s || "open"}
    </Badge>
  );
}

function Section({
  title,
  records,
  primary,
  secondary,
}: {
  title: string;
  records: EngagementRecord[];
  primary: (r: EngagementRecord) => string;
  secondary?: (r: EngagementRecord) => string;
}) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base flex items-center gap-2">
          {title}
          <Badge variant="outline">{records.length}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {records.length === 0 && (
          <p className="text-sm text-muted-foreground">None.</p>
        )}
        {records.map((r) => {
          const sec = secondary?.(r);
          const carried = String(r.detected || "").startsWith("carried over");
          return (
            <div
              key={r.id}
              className="rounded-md border p-3 text-sm space-y-1"
            >
              <div className="flex items-start justify-between gap-3">
                <span className="font-medium">{primary(r) || "—"}</span>
                <div className="flex items-center gap-2 shrink-0">
                  {r.source === "notes" && (
                    <Badge variant="outline">from notes</Badge>
                  )}
                  <StatusBadge status={String(r.status || "open")} />
                </div>
              </div>
              {sec && (
                <div className="text-muted-foreground">{sec}</div>
              )}
              <div className="text-xs text-muted-foreground">
                {provenance(r)}
                {carried && " · no longer detected"}
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

export function EngagementView({ sessions }: Props) {
  const clients = useMemo(
    () =>
      Array.from(
        new Set(sessions.map((s) => s.client).filter(Boolean)),
      ).sort((a, b) => a.localeCompare(b)),
    [sessions],
  );

  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [reg, setReg] = useState<EngagementRegister | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);

  const projects = useMemo(() => {
    if (!client) return [];
    return Array.from(
      new Set(
        sessions
          .filter(
            (s) => s.client.toLowerCase() === client.toLowerCase() && s.project,
          )
          .map((s) => s.project),
      ),
    ).sort((a, b) => a.localeCompare(b));
  }, [sessions, client]);

  const load = async (c: string, p: string) => {
    if (!c) {
      setReg(null);
      return;
    }
    setLoading(true);
    try {
      const { register } = await api.engagementRegister(c, p);
      setReg(register);
    } catch (e) {
      toast.error(
        `Could not build register: ${e instanceof Error ? e.message : e}`,
      );
      setReg(null);
    } finally {
      setLoading(false);
    }
  };

  // Reset project + reload whenever the client changes.
  useEffect(() => {
    setProject("");
    void load(client, "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  const onExport = async () => {
    if (!client) return;
    setExporting(true);
    try {
      const res = await api.engagementExport(client, project);
      if (res.warning) {
        toast.warning(res.warning, { duration: 9000 });
      } else {
        toast.success(`Workbook written: ${res.path}`);
      }
    } catch (e) {
      toast.error(
        `Export failed: ${e instanceof Error ? e.message : e}`,
      );
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <select
          className={selectCls}
          value={client}
          onChange={(e) => setClient(e.target.value)}
        >
          <option value="">Select a client…</option>
          {clients.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>

        <select
          className={selectCls}
          value={project}
          disabled={!client || projects.length === 0}
          onChange={(e) => {
            setProject(e.target.value);
            void load(client, e.target.value);
          }}
        >
          <option value="">All projects</option>
          {projects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>

        <Button
          variant="outline"
          size="sm"
          disabled={!client || loading}
          onClick={() => void load(client, project)}
        >
          <RefreshCw className="h-4 w-4 mr-1" />
          Refresh
        </Button>

        <Button
          size="sm"
          disabled={!reg || exporting}
          onClick={() => void onExport()}
        >
          {exporting ? (
            <Loader2 className="h-4 w-4 mr-1 animate-spin" />
          ) : (
            <FileSpreadsheet className="h-4 w-4 mr-1" />
          )}
          Export to Excel
        </Button>
      </div>

      {!client && (
        <p className="text-sm text-muted-foreground">
          Pick a client to see its engagement register — every
          requirement, decision, action item, and open question rolled
          up across all of that client&apos;s meetings.
        </p>
      )}

      {loading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Building register…
        </div>
      )}

      {reg && !loading && (
        <>
          <div className="flex flex-wrap gap-2 text-sm">
            <Badge variant="outline">
              {reg.session_count} session
              {reg.session_count === 1 ? "" : "s"}
            </Badge>
            <Badge variant="outline">
              {reg.counts.open_requirements} open requirements
            </Badge>
            <Badge variant="outline">
              {reg.counts.decisions} decisions
            </Badge>
            <Badge variant="outline">
              {reg.counts.open_action_items} open actions
            </Badge>
            <Badge variant="outline">
              {reg.counts.open_questions} open questions
            </Badge>
          </div>

          <Section
            title="Requirements"
            records={reg.requirements}
            primary={(r) => String(r.text || "")}
            secondary={(r) =>
              String(r.kind || "") ? `Kind: ${r.kind}` : ""
            }
          />
          <Section
            title="Decisions"
            records={reg.decisions}
            primary={(r) => String(r.title || "")}
            secondary={(r) =>
              [
                r.decided && `Decided: ${r.decided}`,
                r.rationale && `Why: ${r.rationale}`,
              ]
                .filter(Boolean)
                .join("  ·  ")
            }
          />
          <Section
            title="Action Items"
            records={reg.action_items}
            primary={(r) => String(r.text || "")}
            secondary={(r) =>
              [
                r.owner && `Owner: ${r.owner}`,
                r.due && `Due: ${r.due}`,
              ]
                .filter(Boolean)
                .join("  ·  ")
            }
          />
          <Section
            title="Open Questions"
            records={reg.open_questions}
            primary={(r) => String(r.text || "")}
          />
        </>
      )}
    </div>
  );
}
