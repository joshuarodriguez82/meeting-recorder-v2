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
import { Plus, Sparkles, Loader2, Tag, Pencil, Trash2, FolderKanban, X, FolderOpen, Check } from "lucide-react";

interface Props {
  sessions: SessionSummary[];
  onReload: () => void;
  onOpenSession: (id: string) => void;
}

export function ClientsView({ sessions, onReload, onOpenSession }: Props) {
  // Per-client configs (keyed by normalized name; matches backend).
  const [clientConfigs, setClientConfigs] = useState<Record<string, { export_folder: string }>>({});

  useEffect(() => {
    let cancelled = false;
    api.getClientConfigs()
      .then((cfgs) => { if (!cancelled) setClientConfigs(cfgs); })
      .catch(() => { /* non-fatal */ });
    return () => { cancelled = true; };
  }, []);

  // Client names actually used on sessions
  const taggedClients = useMemo(
    () => Array.from(new Set(sessions.map((s) => s.client).filter(Boolean))).sort(),
    [sessions]
  );
  // Additional names the user has created this session that aren't tagged yet
  const [pendingClients, setPendingClients] = useState<string[]>([]);
  const clients = useMemo(() => {
    const merged = new Set([...taggedClients, ...pendingClients]);
    return Array.from(merged).sort();
  }, [taggedClients, pendingClients]);
  const [selected, setSelected] = useState<string>("");
  const [showNewClient, setShowNewClient] = useState(false);
  const [newClientName, setNewClientName] = useState("");
  const [showTagMeetings, setShowTagMeetings] = useState(false);
  const [showAiSuggest, setShowAiSuggest] = useState(false);
  const [showRename, setShowRename] = useState(false);

  // Project sub-selection (null = show all meetings for this client)
  const [selectedProject, setSelectedProject] = useState<string | null>(null);
  // Extra projects the user has created for this client but hasn't tagged yet
  const [pendingProjectsByClient, setPendingProjectsByClient] = useState<Record<string, string[]>>({});
  const [showNewProject, setShowNewProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [showTagProject, setShowTagProject] = useState(false);
  const [showRenameProject, setShowRenameProject] = useState(false);

  // Reset project sub-selection whenever we switch clients
  useEffect(() => {
    setSelectedProject(null);
  }, [selected]);

  useEffect(() => {
    if (!selected && clients.length > 0) setSelected(clients[0]);
  }, [clients, selected]);

  const handleCreate = () => {
    const n = newClientName.trim();
    if (!n) return;
    setPendingClients((prev) => Array.from(new Set([...prev, n])));
    setSelected(n);
    setShowNewClient(false);
    setNewClientName("");
    setShowTagMeetings(true);
    toast.success(`Client "${n}" ready — tag meetings to it`);
  };

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
            onCreate={handleCreate}
          />
        </div>
      </div>
    );
  }

  const clientSessions = sessions.filter((s) => s.client === selected);
  const totalSeconds = clientSessions.reduce((sum, s) => sum + s.duration_s, 0);

  // Projects that have been tagged on real meetings for this client …
  const taggedProjects = Array.from(
    new Set(clientSessions.map((s) => s.project).filter(Boolean))
  ).sort();
  // … plus any the user created but hasn't tagged yet (pending, per-client)
  const pendingForThisClient = pendingProjectsByClient[selected] || [];
  const projectsList = Array.from(new Set([...taggedProjects, ...pendingForThisClient])).sort();

  // Meetings to show in the table (filtered by project chip if one is active)
  const visibleSessions = selectedProject
    ? clientSessions.filter((s) => s.project === selectedProject)
    : clientSessions;
  const visibleSeconds = visibleSessions.reduce((sum, s) => sum + s.duration_s, 0);
  const openActions = visibleSessions.reduce((count, s) => {
    if (!s.action_items) return count;
    return count + (s.action_items.match(/^\s*-\s*\[\s\]/gm)?.length || 0);
  }, 0);
  const decisions = visibleSessions.reduce((count, s) => {
    if (!s.decisions) return count;
    return count + (s.decisions.match(/^##/gm)?.length || 0);
  }, 0);

  const handleCreateProject = () => {
    const n = newProjectName.trim();
    if (!n || !selected) return;
    setPendingProjectsByClient((prev) => {
      const existing = prev[selected] || [];
      return { ...prev, [selected]: Array.from(new Set([...existing, n])) };
    });
    setSelectedProject(n);
    setShowNewProject(false);
    setNewProjectName("");
    setShowTagProject(true);
    toast.success(`Project "${n}" ready under ${selected} — tag meetings to it`);
  };

  return (
    <div className="mx-auto max-w-6xl grid grid-cols-1 md:grid-cols-[240px_minmax(0,1fr)] gap-6">
      {/* Client list sidebar */}
      <div className="space-y-2 min-w-0">
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
      <div className="space-y-6 min-w-0">
        <div className="flex items-center justify-between gap-3 min-w-0">
          <div className="min-w-0">
            <h2 className="text-xl font-semibold truncate">{selected}</h2>
            <p className="text-xs text-muted-foreground">
              {clientSessions.length} meetings · {projectsList.length} projects · {formatDuration(totalSeconds)} total
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

        <DesignatedFolderCard
          client={selected}
          folder={clientConfigs[selected.trim().toLowerCase()]?.export_folder || ""}
          onSaved={(folder) => {
            setClientConfigs((prev) => ({
              ...prev,
              [selected.trim().toLowerCase()]: { export_folder: folder },
            }));
          }}
        />

        {/* Projects under this client — click a chip to filter below */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between py-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <FolderKanban className="h-4 w-4 text-primary" />
              Projects
            </CardTitle>
            <div className="flex gap-2">
              {selectedProject && (
                <Button variant="outline" size="sm" onClick={() => setShowRenameProject(true)}>
                  <Pencil className="h-3.5 w-3.5 mr-2" />
                  Rename Project
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={() => setShowNewProject(true)}>
                <Plus className="h-3.5 w-3.5 mr-2" />
                New Project
              </Button>
            </div>
          </CardHeader>
          <CardContent className="pt-0">
            {projectsList.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No projects yet. Create one to group meetings into workstreams.
              </p>
            ) : (
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => setSelectedProject(null)}
                  className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                    selectedProject === null
                      ? "bg-primary text-primary-foreground border-primary"
                      : "bg-background hover:bg-accent"
                  }`}
                >
                  All ({clientSessions.length})
                </button>
                {projectsList.map((p) => {
                  const count = clientSessions.filter((s) => s.project === p).length;
                  const active = selectedProject === p;
                  return (
                    <button
                      key={p}
                      onClick={() => setSelectedProject(active ? null : p)}
                      className={`group flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors ${
                        active
                          ? "bg-primary text-primary-foreground border-primary"
                          : "bg-background hover:bg-accent"
                      }`}
                    >
                      <span>{p}</span>
                      <Badge variant="outline" className={`text-[10px] ${active ? "border-primary-foreground/40 text-primary-foreground" : ""}`}>
                        {count}
                      </Badge>
                    </button>
                  );
                })}
                {selectedProject && (
                  <Button
                    size="sm"
                    onClick={() => setShowTagProject(true)}
                    className="h-7 text-xs ml-2"
                  >
                    <Tag className="h-3 w-3 mr-1.5" />
                    Tag to {selectedProject}
                  </Button>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Stat cards — reflect the current filter (all client meetings OR just the selected project) */}
        <div className="grid grid-cols-4 gap-3">
          <StatCard label="Meetings" value={visibleSessions.length.toString()} />
          <StatCard label="Hours" value={(visibleSeconds / 3600).toFixed(1)} />
          <StatCard label="Open Actions" value={openActions.toString()} />
          <StatCard label="Decisions" value={decisions.toString()} />
        </div>

        {/* Meetings list */}
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">
              {selectedProject ? `Meetings in "${selectedProject}"` : "Meetings"}
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {visibleSessions.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">
                {selectedProject
                  ? `No meetings in "${selectedProject}" yet. Click "Tag to ${selectedProject}" above.`
                  : "No meetings tagged with this client yet. Click \"Tag Meetings\" above."}
              </p>
            ) : (
              visibleSessions.map((s) => (
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
        onCreate={handleCreate}
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

      <NewProjectDialog
        open={showNewProject}
        onOpenChange={setShowNewProject}
        client={selected}
        name={newProjectName}
        setName={setNewProjectName}
        onCreate={handleCreateProject}
      />

      <TagProjectDialog
        open={showTagProject}
        onOpenChange={setShowTagProject}
        client={selected}
        project={selectedProject || ""}
        sessions={sessions}
        onDone={onReload}
      />

      <RenameProjectDialog
        open={showRenameProject}
        onOpenChange={setShowRenameProject}
        client={selected}
        project={selectedProject || ""}
        sessions={sessions}
        onRenamed={(newName) => { setSelectedProject(newName); onReload(); }}
      />
    </div>
  );
}

function NewProjectDialog({
  open, onOpenChange, client, name, setName, onCreate,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  client: string;
  name: string;
  setName: (s: string) => void;
  onCreate: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>New Project under {client}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="space-y-2">
            <Label>Project Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. AWS Connect PoC"
              autoFocus
              onKeyDown={(e) => e.key === "Enter" && onCreate()}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            After creating, you&apos;ll tag meetings under <strong>{client}</strong> to this project.
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

function TagProjectDialog({
  open, onOpenChange, client, project, sessions, onDone,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  client: string;
  project: string;
  sessions: SessionSummary[];
  onDone: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    if (open) setSelected(new Set());
  }, [open, project]);

  // Scope: meetings under this client that aren't already in this project
  const available = sessions.filter((s) =>
    s.client === client &&
    s.project !== project &&
    (!filter || s.display_name.toLowerCase().includes(filter.toLowerCase()))
  );

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };

  const apply = async () => {
    if (selected.size === 0 || !project) return;
    setApplying(true);
    try {
      // Set both client and project so untagged-client meetings get both at once
      const res = await api.bulkTag(Array.from(selected), client, project);
      toast.success(`Tagged ${res.updated} meetings to ${client} / ${project}`);
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
          <DialogTitle>
            Tag meetings to <span className="text-primary">{project}</span>
          </DialogTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Only showing meetings under <strong>{client}</strong>. To add a meeting from a different
            client, re-tag its client first.
          </p>
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
              No untagged meetings for {client}.
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
                    {s.project && <> · currently: <span className="text-amber-600">{s.project}</span></>}
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

function RenameProjectDialog({
  open, onOpenChange, client, project, sessions, onRenamed,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  client: string;
  project: string;
  sessions: SessionSummary[];
  onRenamed: (newName: string) => void;
}) {
  const [name, setName] = useState(project);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) setName(project);
  }, [open, project]);

  const affected = sessions.filter((s) => s.client === client && s.project === project).length;

  const save = async () => {
    const n = name.trim();
    if (!n || n === project) {
      onOpenChange(false);
      return;
    }
    setSaving(true);
    try {
      const ids = sessions
        .filter((s) => s.client === client && s.project === project)
        .map((s) => s.session_id);
      await api.bulkTag(ids, undefined, n);
      toast.success(`Renamed project to "${n}" (${ids.length} meetings updated)`);
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
          <DialogTitle>Rename Project</DialogTitle>
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
            This will update {affected} meeting{affected === 1 ? "" : "s"} under{" "}
            <strong>{client}</strong> currently tagged &quot;{project}&quot;.
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

function DesignatedFolderCard({
  client, folder, onSaved,
}: {
  client: string;
  folder: string;
  onSaved: (folder: string) => void;
}) {
  // Keep a local copy so typing doesn't have to round-trip through the
  // parent on every keystroke. Reset when the selected client changes
  // or the server-side folder value shifts (after a save).
  const [value, setValue] = useState(folder);
  const [saving, setSaving] = useState(false);
  useEffect(() => { setValue(folder); }, [client, folder]);

  const dirty = value.trim() !== (folder || "").trim();

  const save = async () => {
    setSaving(true);
    try {
      const res = await api.setClientConfig(client, { export_folder: value.trim() });
      onSaved(res.export_folder);
      toast.success(
        res.export_folder
          ? `Designated folder set for ${client}`
          : `Cleared designated folder for ${client}`
      );
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const openFolder = async () => {
    try {
      await api.openFolder({ kind: "client", client });
    } catch (e) {
      toast.error(`Could not open folder: ${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <Card>
      <CardHeader className="py-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <FolderOpen className="h-4 w-4 text-primary" />
          Designated Folder
        </CardTitle>
      </CardHeader>
      <CardContent className="pt-0 space-y-2">
        <p className="text-xs text-muted-foreground">
          New recordings, transcripts, summaries, and action items for{" "}
          <strong>{client}</strong> get copied here automatically after processing.
          Leave blank to skip auto-routing.
        </p>
        <div className="flex gap-2">
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="e.g. C:\Users\joshu\OneDrive - TTEC\Clients\Acme"
            autoComplete="off"
          />
          {folder && (
            <Button variant="outline" onClick={openFolder} title="Open folder in Explorer">
              <FolderOpen className="h-4 w-4" />
            </Button>
          )}
          <Button onClick={save} disabled={!dirty || saving}>
            {saving ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Check className="h-3.5 w-3.5" />
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
