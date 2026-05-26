"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type Meeting, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Sparkles, Copy, Info, Calendar as CalendarIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

interface Props {
  sessions: SessionSummary[];
  // Upcoming calendar meetings — optional so the view degrades to its
  // pre-meeting-picker behavior on callers that haven't been updated.
  meetings?: Meeting[];
}

// Look back through past sessions for one whose display_name matches the
// upcoming meeting's subject. When we find a match, prefer its client +
// project as the defaults so the user doesn't re-tag the same calendar
// event every time. Exact-match first (handles recurring series cleanly),
// fallback to substring containment for slightly-renamed events.
function inferTagsFromHistory(
  subject: string, sessions: SessionSummary[],
): { client: string; project: string } | null {
  if (!subject.trim()) return null;
  const norm = (s: string) => s.toLowerCase().trim();
  const exact = sessions.find(
    (s) => norm(s.display_name) === norm(subject) && (s.client || s.project),
  );
  if (exact) return { client: exact.client, project: exact.project };
  const fuzzy = sessions.find(
    (s) =>
      s.display_name &&
      (norm(s.display_name).includes(norm(subject)) ||
        norm(subject).includes(norm(s.display_name))) &&
      (s.client || s.project),
  );
  if (fuzzy) return { client: fuzzy.client, project: fuzzy.project };
  return null;
}

export function PrepBriefView({ sessions, meetings = [] }: Props) {
  const [subject, setSubject] = useState("");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [brief, setBrief] = useState("");
  const [relatedCount, setRelatedCount] = useState(0);
  const [generating, setGenerating] = useState(false);
  // Sentinel value for the meeting picker so the SelectItem doesn't
  // collide with a real meeting whose subject is the empty string.
  const [pickedMeetingKey, setPickedMeetingKey] = useState<string>("");

  // Only show upcoming meetings (not all-day, in the future or
  // currently happening). Match the same qualifying logic the Record
  // view uses for the calendar tiles.
  const upcomingMeetings = useMemo(() => {
    const now = Date.now();
    return meetings.filter((m) => {
      try {
        const end = new Date(m.end).getTime();
        return end > now - 5 * 60 * 1000; // include meetings that just ended ≤5 min ago
      } catch {
        return false;
      }
    });
  }, [meetings]);

  const handlePickMeeting = (raw: string | null) => {
    const key = raw ?? "";
    setPickedMeetingKey(key);
    if (!key || key === "none") return;
    // key is the start ISO + "|" + subject, unique enough for the dropdown
    const sep = key.indexOf("|");
    if (sep < 0) return;
    const meetingSubject = key.slice(sep + 1);
    setSubject(meetingSubject);
    const inferred = inferTagsFromHistory(meetingSubject, sessions);
    if (inferred) {
      if (inferred.client) setClient(inferred.client);
      if (inferred.project) setProject(inferred.project);
      toast.success(
        `Auto-tagged from previous "${meetingSubject}" meeting: ` +
        [inferred.client, inferred.project].filter(Boolean).join(" / "),
      );
    }
  };

  const clients = useMemo(
    () => Array.from(new Set(sessions.map((s) => s.client).filter(Boolean))).sort(),
    [sessions]
  );
  // Projects are scoped to the currently selected client. If no client is
  // picked yet, show every project so the user can still filter broadly.
  const projects = useMemo(() => {
    const pool = client ? sessions.filter((s) => s.client === client) : sessions;
    return Array.from(new Set(pool.map((s) => s.project).filter(Boolean))).sort();
  }, [sessions, client]);

  // Clearing client or switching to a client that doesn't have the
  // currently selected project should reset the project filter.
  useEffect(() => {
    if (project && !projects.includes(project)) {
      setProject("");
    }
  }, [project, projects]);

  // Preview which meetings will be used. When both client AND project
  // are set we AND them — a project always belongs to a client, so that's
  // the correct semantics. Earlier this was OR, which incorrectly pulled
  // in meetings from other clients that happened to share a project name.
  const relatedPreview = useMemo(() => {
    if (!client && !project) return [];
    return sessions.filter((s) => {
      if (client && s.client !== client) return false;
      if (project && s.project !== project) return false;
      return Boolean(client) || Boolean(project);
    }).slice(0, 8);
  }, [client, project, sessions]);

  const generate = async () => {
    if (!subject.trim()) {
      toast.error("Enter a meeting subject first");
      return;
    }
    setGenerating(true);
    setBrief("");
    try {
      const res = await api.prepBrief(subject, client, project);
      setBrief(res.brief);
      setRelatedCount(res.related_count);
      toast.success(`Brief ready from ${res.related_count} prior meetings`);
    } catch (e) {
      toast.error(`Failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setGenerating(false);
    }
  };

  const copy = async () => {
    await navigator.clipboard.writeText(brief);
    toast.success("Brief copied to clipboard");
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      {/* Explanation banner */}
      <Card className="bg-accent/30 border-primary/20">
        <CardContent className="p-4 flex gap-3 items-start">
          <Info className="h-5 w-5 text-primary shrink-0 mt-0.5" />
          <div className="space-y-1 text-sm">
            <p className="font-medium">How Prep Brief works</p>
            <p className="text-muted-foreground text-xs leading-relaxed">
              Tell Claude what meeting you&apos;re going into. Optionally filter by Client or Project
              to narrow the context. Claude reads every tagged meeting&apos;s summary, action items, and
              decisions, then generates: recent context, open items, risks, and suggested discussion points.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            New Prep Brief
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          {upcomingMeetings.length > 0 && (
            <div className="space-y-2">
              <Label className="flex items-center gap-2">
                <CalendarIcon className="h-3.5 w-3.5 text-primary" />
                Pick from your calendar
              </Label>
              <Select value={pickedMeetingKey || "none"} onValueChange={handlePickMeeting}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Type a subject below, or pick an upcoming meeting…" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">— Type a subject manually instead —</SelectItem>
                  {upcomingMeetings.map((m) => {
                    const when = (() => {
                      try {
                        return new Date(m.start).toLocaleString([], {
                          weekday: "short", month: "short", day: "numeric",
                          hour: "numeric", minute: "2-digit",
                        });
                      } catch { return m.start; }
                    })();
                    const key = `${m.start}|${m.subject}`;
                    return (
                      <SelectItem key={key} value={key}>
                        {m.subject || "(no subject)"} · {when}
                      </SelectItem>
                    );
                  })}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Picking a meeting fills in the subject below. If a prior
                meeting with the same name was tagged with a client and
                project, those get carried over automatically.
              </p>
            </div>
          )}
          <div className="space-y-2">
            <Label>Upcoming Meeting Subject <span className="text-destructive">*</span></Label>
            <Input
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="e.g. Weekly Sync with SimpliSafe"
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              This is the meeting you&apos;re preparing for. Use a descriptive title.
            </p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Filter by Client</Label>
              <Select value={client || "none"} onValueChange={(v) => setClient(!v || v === "none" ? "" : v)}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Any client" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">Any client</SelectItem>
                  {clients.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Filter by Project</Label>
              <Select value={project || "none"} onValueChange={(v) => setProject(!v || v === "none" ? "" : v)}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Any project" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">Any project</SelectItem>
                  {projects.map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          </div>

          {/* Preview of meetings that'll be used */}
          {(client || project) && (
            <div className="rounded-lg border bg-muted/40 p-3">
              <div className="flex items-center justify-between mb-2">
                <Label className="text-xs uppercase tracking-wider text-muted-foreground">
                  Will use these {relatedPreview.length} meetings for context
                </Label>
                {client && <Badge variant="outline" className="text-[10px]">Client: {client}</Badge>}
                {project && <Badge variant="outline" className="text-[10px]">Project: {project}</Badge>}
              </div>
              {relatedPreview.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  No meetings match these filters. Try adjusting or clearing them — Claude will fall back to recent meetings.
                </p>
              ) : (
                <ul className="text-xs space-y-1 text-muted-foreground">
                  {relatedPreview.map((s) => (
                    <li key={s.session_id} className="flex items-center gap-2">
                      <span>•</span>
                      <span className="flex-1 truncate">{s.display_name}</span>
                      <span className="shrink-0">
                        {s.started_at ? new Date(s.started_at).toLocaleDateString() : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <Button onClick={generate} disabled={generating || !subject.trim()} className="w-full md:w-auto">
            {generating ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Sparkles className="h-4 w-4 mr-2" />
            )}
            Generate Brief
          </Button>
        </CardContent>
      </Card>

      {brief && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center justify-between">
              <span>Brief: {subject}</span>
              <Button size="sm" variant="ghost" onClick={copy}>
                <Copy className="h-3.5 w-3.5 mr-2" />
                Copy
              </Button>
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              Based on {relatedCount} prior meeting{relatedCount === 1 ? "" : "s"}
            </p>
          </CardHeader>
          <CardContent>
            <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed rounded-lg bg-muted/40 p-4 max-w-full overflow-x-hidden">
              {brief}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
