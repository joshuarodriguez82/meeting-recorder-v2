"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  type SessionSummary,
  type EngagementRegister,
  type EngagementRecord,
  type EngagementOverlay,
} from "@/lib/api";
import { PortalSyncControls } from "@/components/portal-sync-controls";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Loader2, FileSpreadsheet, RefreshCw, Pencil, Save,
} from "lucide-react";

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

        {/* Sync-only here. BINDING management lives in the Clients tab
            with the rest of the per-client/per-project configuration
            (user feedback 2026-08-21: setup buried in a viewing screen
            "makes 0 sense") — this view is where the register is READ,
            so the only portal control it keeps is the action on the
            data being looked at. Visible only when a specific project
            is chosen: bindings are strictly per-project. */}
        {client && project && (
          <PortalSyncControls client={client} project={project} mode="sync-only" />
        )}
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
            {reg.last_meeting_at && (
              <Badge variant="outline">
                Last: {new Date(reg.last_meeting_at).toLocaleDateString()}
              </Badge>
            )}
            {reg.first_meeting_at && reg.first_meeting_at !== reg.last_meeting_at && (
              <Badge variant="outline">
                Since: {new Date(reg.first_meeting_at).toLocaleDateString()}
              </Badge>
            )}
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
            {reg.counts.total_commitments > 0 && (
              <Badge variant={reg.counts.outstanding_commitments > 0 ? "default" : "outline"}>
                {reg.counts.outstanding_commitments} outstanding commitment
                {reg.counts.outstanding_commitments === 1 ? "" : "s"}
                {" "}of {reg.counts.total_commitments}
              </Badge>
            )}
          </div>

          {/* Manual overlay — status, sponsor, milestone, notes.
              The auto-roll handles "what's the system know"; this
              handles "what's the SA know." Click Edit to open the
              editor inline; values persist across sessions. */}
          <EngagementOverlayCard
            client={reg.client}
            project={reg.project}
            overlay={reg.overlay}
            onSaved={(next) => setReg({ ...reg, overlay: next })}
          />

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

// Manual overlay card. Two states: read (compact display of whatever's
// set, with an Edit button) and edit (form). Status uses a select with
// known values, the others are inputs / textarea. Auto-detects unset
// state and shows a CTA.
function EngagementOverlayCard({
  client, project, overlay, onSaved,
}: {
  client: string;
  project: string;
  overlay: EngagementOverlay;
  onSaved: (next: EngagementOverlay) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [knownStatuses, setKnownStatuses] = useState<string[]>([
    "active", "on-hold", "at-risk", "won", "lost", "archived",
  ]);
  const [form, setForm] = useState<EngagementOverlay>(overlay);

  useEffect(() => { setForm(overlay); }, [overlay]);

  // Pull canonical status list lazily on mount so the dropdown always
  // reflects what the backend will accept.
  useEffect(() => {
    api.engagementKnownStatuses()
      .then((r) => { if (r.statuses?.length) setKnownStatuses(r.statuses); })
      .catch(() => { /* fall back to local list */ });
  }, []);

  const isEmpty = !overlay.status && !overlay.exec_sponsor
    && !overlay.next_milestone && !overlay.notes;

  const save = async () => {
    setSaving(true);
    try {
      const res = await api.putEngagementOverlay(client, {
        project,
        status: form.status,
        exec_sponsor: form.exec_sponsor,
        next_milestone: form.next_milestone,
        notes: form.notes,
      });
      onSaved(res.overlay);
      setEditing(false);
      toast.success("Engagement details saved");
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  // ── Read mode ──
  if (!editing) {
    return (
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
          <CardTitle className="text-sm font-medium">Engagement details</CardTitle>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setEditing(true)}
            className="h-7"
          >
            <Pencil className="h-3 w-3 mr-1.5" />
            {isEmpty ? "Add details" : "Edit"}
          </Button>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {isEmpty ? (
            <p className="text-muted-foreground italic text-xs">
              Add status, exec sponsor, next milestone, and free-form
              notes — context that isn&apos;t in any recorded meeting.
              Stored separately from the auto-rolled register.
            </p>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3">
              {overlay.status && (
                <OverlayReadField label="Status">
                  <OverlayStatusBadge status={overlay.status} />
                </OverlayReadField>
              )}
              {overlay.exec_sponsor && (
                <OverlayReadField label="Exec sponsor">
                  {overlay.exec_sponsor}
                </OverlayReadField>
              )}
              {overlay.next_milestone && (
                <OverlayReadField label="Next milestone">
                  {overlay.next_milestone}
                </OverlayReadField>
              )}
              {overlay.updated_at && (
                <OverlayReadField label="Last updated">
                  <span className="text-muted-foreground text-xs">
                    {new Date(overlay.updated_at).toLocaleString()}
                  </span>
                </OverlayReadField>
              )}
              {overlay.notes && (
                <div className="md:col-span-2 space-y-1">
                  <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                    Notes
                  </div>
                  <div className="whitespace-pre-wrap text-sm leading-relaxed bg-muted/40 rounded-md p-3">
                    {overlay.notes}
                  </div>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    );
  }

  // ── Edit mode ──
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-sm font-medium">Engagement details</CardTitle>
        <div className="flex gap-2">
          <Button
            size="sm" variant="ghost" className="h-7"
            onClick={() => { setForm(overlay); setEditing(false); }}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button size="sm" className="h-7" onClick={save} disabled={saving}>
            {saving
              ? <Loader2 className="h-3 w-3 animate-spin mr-1.5" />
              : <Save className="h-3 w-3 mr-1.5" />}
            Save
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label className="text-xs">Status</Label>
            <Select
              value={form.status || "__none__"}
              onValueChange={(v) =>
                setForm({ ...form, status: !v || v === "__none__" ? "" : v })}
            >
              <SelectTrigger>
                <SelectValue placeholder="Pick a status…" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">— None —</SelectItem>
                {knownStatuses.map((s) => (
                  <SelectItem key={s} value={s}>{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Exec sponsor</Label>
            <Input
              value={form.exec_sponsor}
              onChange={(e) =>
                setForm({ ...form, exec_sponsor: e.target.value })}
              placeholder="e.g. Carla Rivera, CTO"
            />
          </div>
          <div className="md:col-span-2 space-y-1.5">
            <Label className="text-xs">Next milestone</Label>
            <Input
              value={form.next_milestone}
              onChange={(e) =>
                setForm({ ...form, next_milestone: e.target.value })}
              placeholder="e.g. SOW signature target 2026-06-15"
            />
          </div>
          <div className="md:col-span-2 space-y-1.5">
            <Label className="text-xs">Notes</Label>
            <Textarea
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
              placeholder="Anything the auto-rolled register can't see — exec asks, political dynamics, commercial context, redlines just received…"
              rows={5}
              className="resize-y"
            />
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground italic">
          Stored separately from session data so editing here can&apos;t
          break any meeting records.
        </p>
      </CardContent>
    </Card>
  );
}

function OverlayReadField({
  label, children,
}: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="text-sm">{children}</div>
    </div>
  );
}

function OverlayStatusBadge({ status }: { status: string }) {
  // Map known statuses to badge variants. Unknown values still
  // render — they just use the neutral outline variant. Named
  // separately from the per-record StatusBadge above (which renders
  // record-level statuses like "open" / "met" / "dropped").
  const variant: "default" | "secondary" | "destructive" | "outline" =
    status === "active" ? "default"
    : status === "at-risk" ? "destructive"
    : status === "on-hold" ? "secondary"
    : status === "won" ? "default"
    : "outline";
  return (
    <Badge variant={variant} className="text-xs capitalize">
      {status}
    </Badge>
  );
}
