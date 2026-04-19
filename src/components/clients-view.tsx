"use client";

import { useMemo, useState, useEffect } from "react";
import { api, formatDuration, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Plus, Sparkles, Loader2, Tag, Pencil, Trash2 } from "lucide-react";

interface Props {
  sessions: SessionSummary[];
  onReload: () => void;
  onOpenSession: (id: string) => void;
}

export function ClientsView({ sessions, onReload, onOpenSession }: Props) {
  const clients = useMemo(
    () => Array.from(new Set(sessions.map((s) => s.client).filter(Boolean))).sort(),
    [sessions]
  );
  const [selected, setSelected] = useState<string>("");
  const [showNewClient, setShowNewClient] = useState(false);
  const [newClientName, setNewClientName] = useState("");
  const [showTagMeetings, setShowTagMeetings] = useState(false);
  const [showAiSuggest, setShowAiSuggest] = useState(false);
  const [showRename, setShowRename] = useState(false);

  useEffect(() => {
    if (!selected && clients.length > 0) setSelected(clients[0]);
  }, [clients, selected]);

  if (clients.length === 0 && !showNewClient) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center max-w-sm space-y-4">
          <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-muted text-2xl">
            🏢
          </div>
          <div>
            <h3 className="text-base font-semibold mb-1">No clients yet</h3>
            <p className="text-sm text-muted-foreground">
              Create a client to organize meetings by customer or account.
            </p>
          </div>
          <Button onClick={() => setShowNewClient(true)}>
            <Plus className="h-4 w-4 mr-2" />
            Create Your First Client
          </Button>
          <NewClientDialog
            open={showNewClient}
            onOpenChange={setShowNewClient}
            name={newClientName}
            setName={setNewClientName}
            onCreate={() => {
              const n = newClientName.trim();
              if (!n) return;
              setSelected(n);
              setShowNewClient(false);
              setNewClientName("");
              setShowTagMeetings(true);
              toast.success(`Client "${n}" ready — tag some meetings to it.`);
            }}
          />
        </div>
      </div>
    );
  }

  const clientSessions = sessions.filter((s) => s.client === selected);
  const totalSeconds = clientSessions.reduce((sum, s) => sum + s.duration_s, 0);
  const projects = new Set(clientSessions.map((s) => s.project).filter(Boolean));
  const openActions = clientSessions.reduce((count, s) => {
    if (!s.action_items) return count;
    return count + (s.action_items.match(/^\s*-\s*\[\s\]/gm)?.length || 0);
  }, 0);
  const decisions = clientSessions.reduce((count, s) => {
    if (!s.decisions) return count;
    return count + (s.decisions.match(/^##/gm)?.length || 0);
  }, 0);

  return (
    <div className="mx-auto max-w-6xl grid grid-cols-1 md:grid-cols-[240px_1fr] gap-6">
      {/* Client list sidebar */}
      <div className="space-y-2">
        <div className="flex items-center justify-between px-1">
          <Label className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Clients ({clients.length})
          </Label>
          <Button
            size="sm" variant="ghost"
            className="h-6 w-6 p-0"
            onClick={() => setShowNewClient(true)}
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        </div>
        <div className="space-y-0.5">
          {clients.map((c) => {
            const count = sessions.filter((s) => s.client === c).length;
            return (
              <button
                key={c}
                onClick={() => setSelected(c)}
                className={`w-full text-left flex items-center justify-between rounded-md px-2.5 py-2 text-sm transition-colors ${
                  selected === c
                    ? "bg-accent text-accent-foreground font-medium"
                    : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
                }`}
              >
                <span className="truncate">{c}</span>
                <Badge variant="outline" className="text-[10px] shrink-0">{count}</Badge>
              </button>
            );
          })}
        </div>
      </div>

      {/* Selected client detail */}
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl font-semibold">{selected}</h2>
            <p className="text-xs text-muted-foreground">
              {clientSessions.length} meetings · {projects.size} projects · {formatDuration(totalSeconds)} total
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => setShowRename(true)}>
              <Pencil className="h-3.5 w-3.5 mr-2" />
              Rename
            </Button>
            <Button variant="outline" size="sm" onClick={() => setShowAiSuggest(true)}>
              <Sparkles className="h-3.5 w-3.5 mr-2" />
              AI Suggest
            </Button>
            <Button size="sm" onClick={() => setShowTagMeetings(true)}>
              <Tag className="h-3.5 w-3.5 mr-2" />
              Tag Meetings
            </Button>
          </div>
        </div>

        {/* Stat cards */}
        <div className="grid grid-cols-4 gap-3">
          <StatCard label="Meetings" value={clientSessions.length.toString()} />
          <StatCard label="Hours" value={(totalSeconds / 3600).toFixed(1)} />
          <StatCard label="Open Actions" value={openActions.toString()} />
          <StatCard label="Decisions" value={decisions.toString()} />
        </div>

        {/* Meetings list */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Meetings</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {clientSessions.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">
                No meetings tagged with this client yet. Click &quot;Tag Meetings&quot; above.
              </p>
            ) : (
              clientSessions.map((s) => (
                <button
                  key={s.session_id}
                  onClick={() => onOpenSession(s.session_id)}
                  className="w-full text-left flex items-center gap-3 border-b last:border-b-0 p-3 hover:bg-muted/40 transition-colors text-sm"
                >
                  <div className="flex-1 min-w-0">
                    <div className="truncate font-medium">{s.display_name}</div>
                    <div className="text-xs text-muted-foreground">
                      {s.started_at ? new Date(s.started_at).toLocaleDateString() : ""}
                      {s.project && <> · <span>{s.project}</span></>}
                      {" · "}{formatDuration(s.duration_s)}
                    </div>
                  </div>
                  <div className="flex gap-1 shrink-0">
                    {s.has_summary && <Badge variant="outline" className="text-[10px]">✨</Badge>}
                    {s.has_action_items && <Badge variant="outline" className="text-[10px]">📋</Badge>}
                    {s.has_decisions && <Badge variant="outline" className="text-[10px]">🎯</Badge>}
                  </div>
                </button>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      {/* Dialogs */}
      <NewClientDialog
        open={showNewClient}
        onOpenChange={setShowNewClient}
        name={newClientName}
        setName={setNewClientName}
        onCreate={() => {
          const n = newClientName.trim();
          if (!n) return;
          setSelected(n);
          setShowNewClient(false);
          setNewClientName("");
          setShowTagMeetings(true);
          toast.success(`Client "${n}" ready — tag some meetings to it.`);
        }}
      />

      <TagMeetingsDialog
        open={showTagMeetings}
        onOpenChange={setShowTagMeetings}
        client={selected}
        sessions={sessions}
        onDone={onReload}
      />

      <AiSuggestDialog
        open={showAiSuggest}
        onOpenChange={setShowAiSuggest}
        client={selected}
        onDone={onReload}
      />

      <RenameClientDialog
        open={showRename}
        onOpenChange={setShowRename}
        client={selected}
        sessions={sessions}
        onRenamed={(newName) => { setSelected(newName); onReload(); }}
      />
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

function NewClientDialog({
  open, onOpenChange, name, setName, onCreate,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  name: string;
  setName: (s: string) => void;
  onCreate: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Create Client</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="space-y-2">
            <Label>Client Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. SimpliSafe"
              autoFocus
              onKeyDown={(e) => e.key === "Enter" && onCreate()}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            After creating, you&apos;ll be prompted to tag meetings to this client.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={onCreate} disabled={!name.trim()}>Create</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TagMeetingsDialog({
  open, onOpenChange, client, sessions, onDone,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  client: string;
  sessions: SessionSummary[];
  onDone: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    if (open) setSelected(new Set());
  }, [open]);

  const available = sessions.filter((s) =>
    s.client !== client &&
    (!filter || s.display_name.toLowerCase().includes(filter.toLowerCase()))
  );

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };

  const apply = async () => {
    if (selected.size === 0) return;
    setApplying(true);
    try {
      const res = await api.bulkTag(Array.from(selected), client);
      toast.success(`Tagged ${res.updated} meetings to ${client}`);
      onDone();
      onOpenChange(false);
    } catch (e) {
      toast.error(`Tag failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setApplying(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-6 py-4 border-b">
          <DialogTitle>Tag Meetings to &quot;{client}&quot;</DialogTitle>
        </DialogHeader>
        <div className="px-6 py-3 border-b">
          <Input
            placeholder="Filter meetings..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            autoComplete="off"
          />
        </div>
        <div className="flex-1 overflow-y-auto">
          {available.length === 0 ? (
            <p className="text-sm text-muted-foreground p-8 text-center">
              No untagged meetings match.
            </p>
          ) : (
            available.map((s) => (
              <label
                key={s.session_id}
                className="flex items-center gap-3 border-b last:border-b-0 p-3 hover:bg-muted/40 cursor-pointer text-sm"
              >
                <input
                  type="checkbox"
                  checked={selected.has(s.session_id)}
                  onChange={() => toggle(s.session_id)}
                  className="h-4 w-4"
                />
                <div className="flex-1 min-w-0">
                  <div className="truncate font-medium">{s.display_name}</div>
                  <div className="text-xs text-muted-foreground">
                    {s.started_at ? new Date(s.started_at).toLocaleDateString() : ""}
                    {s.client && <> · currently: <span className="text-amber-600">{s.client}</span></>}
                  </div>
                </div>
              </label>
            ))
          )}
        </div>
        <DialogFooter className="px-6 py-3 border-t">
          <p className="text-sm text-muted-foreground mr-auto">{selected.size} selected</p>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={apply} disabled={selected.size === 0 || applying}>
            {applying ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : null}
            Apply Tag
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AiSuggestDialog({
  open, onOpenChange, client, onDone,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  client: string;
  onDone: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [suggestions, setSuggestions] = useState<Array<{
    session_id: string;
    display_name: string;
    started_at: string;
    confidence: number;
    reason: string;
  }>>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    if (open && client) {
      setSuggestions([]);
      setSelected(new Set());
      setLoading(true);
      api.suggestTagging(client)
        .then((res) => {
          setSuggestions(res.suggestions);
          // Auto-select high-confidence ones
          const auto = new Set(
            res.suggestions
              .filter((s) => s.confidence >= 0.75)
              .map((s) => s.session_id)
          );
          setSelected(auto);
        })
        .catch((e) => toast.error(`AI suggest failed: ${e instanceof Error ? e.message : e}`))
        .finally(() => setLoading(false));
    }
  }, [open, client]);

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };

  const apply = async () => {
    if (selected.size === 0) return;
    setApplying(true);
    try {
      await api.bulkTag(Array.from(selected), client);
      toast.success(`Tagged ${selected.size} meetings to ${client}`);
      onDone();
      onOpenChange(false);
    } catch (e) {
      toast.error(`Tag failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setApplying(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col p-0 gap-0">
        <DialogHeader className="px-6 py-4 border-b">
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            AI-Suggested Meetings for &quot;{client}&quot;
          </DialogTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Claude reviews your past meetings and suggests which belong to this client.
          </p>
        </DialogHeader>
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="h-6 w-6 animate-spin mr-2" />
              Analyzing meetings…
            </div>
          ) : suggestions.length === 0 ? (
            <p className="text-sm text-muted-foreground p-8 text-center">
              No matching meetings found.
            </p>
          ) : (
            suggestions.map((s) => (
              <label
                key={s.session_id}
                className="flex items-start gap-3 border-b last:border-b-0 p-3 hover:bg-muted/40 cursor-pointer text-sm"
              >
                <input
                  type="checkbox"
                  checked={selected.has(s.session_id)}
                  onChange={() => toggle(s.session_id)}
                  className="h-4 w-4 mt-0.5"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium truncate">{s.display_name}</span>
                    <Badge
                      variant="outline"
                      className={`text-[10px] ${
                        s.confidence >= 0.8 ? "border-green-500 text-green-700" :
                        s.confidence >= 0.6 ? "border-blue-500 text-blue-700" :
                        "border-muted-foreground"
                      }`}
                    >
                      {Math.round(s.confidence * 100)}%
                    </Badge>
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {s.started_at ? new Date(s.started_at).toLocaleDateString() : ""}
                  </div>
                  <div className="text-xs mt-1 text-muted-foreground italic">{s.reason}</div>
                </div>
              </label>
            ))
          )}
        </div>
        <DialogFooter className="px-6 py-3 border-t">
          <p className="text-sm text-muted-foreground mr-auto">{selected.size} of {suggestions.length} selected</p>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
          <Button onClick={apply} disabled={selected.size === 0 || applying}>
            {applying ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : null}
            Apply to {selected.size}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RenameClientDialog({
  open, onOpenChange, client, sessions, onRenamed,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  client: string;
  sessions: SessionSummary[];
  onRenamed: (newName: string) => void;
}) {
  const [name, setName] = useState(client);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) setName(client);
  }, [open, client]);

  const affected = sessions.filter((s) => s.client === client).length;

  const save = async () => {
    const n = name.trim();
    if (!n || n === client) {
      onOpenChange(false);
      return;
    }
    setSaving(true);
    try {
      const ids = sessions.filter((s) => s.client === client).map((s) => s.session_id);
      await api.bulkTag(ids, n);
      toast.success(`Renamed to "${n}" (${ids.length} meetings updated)`);
      onRenamed(n);
      onOpenChange(false);
    } catch (e) {
      toast.error(`Rename failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Rename Client</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="space-y-2">
            <Label>New Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              onKeyDown={(e) => e.key === "Enter" && save()}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            This will update {affected} meeting{affected === 1 ? "" : "s"} currently tagged &quot;{client}&quot;.
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={save} disabled={!name.trim() || saving}>
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : null}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
