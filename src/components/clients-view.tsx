"use client";

import { useMemo, useState, useEffect } from "react";
import { formatDuration, type SessionSummary } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";

export function ClientsView({ sessions }: { sessions: SessionSummary[] }) {
  const clients = useMemo(
    () => Array.from(new Set(sessions.map((s) => s.client).filter(Boolean))).sort(),
    [sessions]
  );
  const [selected, setSelected] = useState<string>(clients[0] || "");

  useEffect(() => {
    if (!selected && clients.length > 0) setSelected(clients[0]);
  }, [clients, selected]);

  if (clients.length === 0) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center max-w-sm">
          <div className="mx-auto mb-3 flex h-14 w-14 items-center justify-center rounded-full bg-muted text-2xl">
            🏢
          </div>
          <h3 className="text-base font-semibold mb-1">No clients tagged yet</h3>
          <p className="text-sm text-muted-foreground">
            Tag meetings with a client in the Record view to build this dashboard.
          </p>
        </div>
      </div>
    );
  }

  const clientSessions = sessions.filter((s) => s.client === selected);
  const totalHours = clientSessions.reduce((sum, s) => sum + s.duration_s, 0);
  const projects = new Set(clientSessions.map((s) => s.project).filter(Boolean));
  const openActions = clientSessions.reduce((count, s) => {
    if (!s.action_items) return count;
    const matches = s.action_items.match(/^\s*-\s*\[\s\]/gm);
    return count + (matches?.length || 0);
  }, 0);
  const decisions = clientSessions.reduce((count, s) => {
    if (!s.decisions) return count;
    const matches = s.decisions.match(/^##/gm);
    return count + (matches?.length || 0);
  }, 0);

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div className="flex items-center gap-3">
        <span className="text-sm text-muted-foreground">Client:</span>
        <Select value={selected} onValueChange={(v) => v && setSelected(v)}>
          <SelectTrigger className="w-64"><SelectValue /></SelectTrigger>
          <SelectContent>
            {clients.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Meetings" value={clientSessions.length.toString()} />
        <StatCard label="Total Time" value={`${(totalHours / 3600).toFixed(1)}h`} />
        <StatCard label="Projects" value={projects.size.toString()} />
        <StatCard label="Open Actions" value={openActions.toString()} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Meetings */}
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 border-b text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Meetings
            </div>
            {clientSessions.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">No meetings yet.</p>
            ) : (
              clientSessions.map((s) => (
                <div key={s.session_id} className="flex items-center gap-3 border-b last:border-b-0 p-3 text-sm">
                  <div className="flex-1 min-w-0">
                    <div className="truncate font-medium">{s.display_name}</div>
                    <div className="text-xs text-muted-foreground">
                      {s.started_at ? new Date(s.started_at).toLocaleDateString() : ""}
                      {s.project && <> · <span>{s.project}</span></>}
                      {" · "}{formatDuration(s.duration_s)}
                    </div>
                  </div>
                  {s.has_summary && <Badge variant="outline" className="text-[10px]">✨</Badge>}
                  {s.has_action_items && <Badge variant="outline" className="text-[10px]">📋</Badge>}
                  {s.has_decisions && <Badge variant="outline" className="text-[10px]">🎯</Badge>}
                </div>
              ))
            )}
          </CardContent>
        </Card>

        {/* Decisions */}
        <Card>
          <CardContent className="p-0">
            <div className="px-4 py-3 border-b text-xs font-medium uppercase tracking-wider text-muted-foreground">
              Recent Decisions ({decisions})
            </div>
            <div className="p-3 max-h-[320px] overflow-y-auto text-sm">
              {decisions === 0 ? (
                <p className="text-muted-foreground text-center py-4">
                  No decisions logged for this client.
                </p>
              ) : (
                clientSessions
                  .filter((s) => s.decisions)
                  .slice(0, 20)
                  .map((s) => (
                    <div key={s.session_id} className="mb-3 pb-3 border-b last:border-b-0">
                      <div className="font-medium text-xs text-muted-foreground mb-1">
                        {s.display_name} · {s.started_at ? new Date(s.started_at).toLocaleDateString() : ""}
                      </div>
                      <pre className="whitespace-pre-wrap font-sans text-xs text-foreground/90">
                        {s.decisions.slice(0, 400)}
                        {s.decisions.length > 400 && "..."}
                      </pre>
                    </div>
                  ))
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-2xl font-bold text-primary">{value}</div>
        <div className="text-xs text-muted-foreground mt-1">{label}</div>
      </CardContent>
    </Card>
  );
}
