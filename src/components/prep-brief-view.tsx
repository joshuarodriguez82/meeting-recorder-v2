"use client";

import { useMemo, useState } from "react";
import { api, type SessionSummary } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Sparkles, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

export function PrepBriefView({ sessions }: { sessions: SessionSummary[] }) {
  const [subject, setSubject] = useState("");
  const [client, setClient] = useState("");
  const [project, setProject] = useState("");
  const [brief, setBrief] = useState("");
  const [relatedCount, setRelatedCount] = useState(0);
  const [generating, setGenerating] = useState(false);

  const clients = useMemo(
    () => Array.from(new Set(sessions.map((s) => s.client).filter(Boolean))).sort(),
    [sessions]
  );
  const projects = useMemo(
    () => Array.from(new Set(sessions.map((s) => s.project).filter(Boolean))).sort(),
    [sessions]
  );

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
      toast.success(`Brief ready (from ${res.related_count} prior meetings)`);
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
      <Card>
        <CardContent className="p-6 space-y-4">
          <div className="space-y-2">
            <Label>Upcoming Meeting Subject</Label>
            <Input
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="e.g. Weekly Sync with SimpliSafe"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Client</Label>
              <Select value={client || "none"} onValueChange={(v) => setClient(!v || v === "none" ? "" : v)}>
                <SelectTrigger>
                  <SelectValue placeholder="Any" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">(any)</SelectItem>
                  {clients.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Project</Label>
              <Select value={project || "none"} onValueChange={(v) => setProject(!v || v === "none" ? "" : v)}>
                <SelectTrigger>
                  <SelectValue placeholder="Any" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">(any)</SelectItem>
                  {projects.map((p) => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          </div>
          <Button onClick={generate} disabled={generating || !subject.trim()}>
            {generating ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Sparkles className="h-4 w-4 mr-2" />
            )}
            Generate Prep Brief
          </Button>
        </CardContent>
      </Card>

      {brief && (
        <Card>
          <CardContent className="p-6 space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-sm font-semibold">Brief for &quot;{subject}&quot;</h3>
                <p className="text-xs text-muted-foreground">
                  Based on {relatedCount} prior meeting{relatedCount === 1 ? "" : "s"}
                </p>
              </div>
              <Button size="sm" variant="ghost" onClick={copy}>
                <Copy className="h-3.5 w-3.5 mr-2" />
                Copy
              </Button>
            </div>
            <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed rounded-lg bg-muted/40 p-4">
              {brief}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
