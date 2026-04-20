"use client";

import { useEffect, useState } from "react";
import { api, formatBytes, type Settings } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Save, Trash2 } from "lucide-react";
import { GpuAccelerationCard } from "./gpu-acceleration-card";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const WHISPER_MODELS = ["tiny", "base", "small", "medium", "large"];
const CLAUDE_MODELS = [
  { value: "claude-haiku-4-5", label: "Haiku 4.5 (cheap, good for summaries)" },
  { value: "claude-sonnet-4-5", label: "Sonnet 4.5 (premium, ~4× cost)" },
  { value: "claude-3-5-haiku-latest", label: "Haiku 3.5 (legacy)" },
];

export function SettingsView() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [storage, setStorage] = useState<{
    total_bytes: number;
    session_count: number;
    wav_count: number;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  const [cleaning, setCleaning] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const [s, stats] = await Promise.all([
          api.getSettings(),
          api.getRetentionStats().catch(() => null),
        ]);
        setSettings(s);
        setStorage(stats);
      } catch (e) {
        toast.error(`Could not load settings: ${e instanceof Error ? e.message : e}`);
      }
    })();
  }, []);

  if (!settings) {
    return (
      <div className="flex h-full items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }

  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    setSettings({ ...settings, [key]: value });
  };

  const save = async () => {
    setSaving(true);
    try {
      await api.saveSettings(settings);
      toast.success("Settings saved. Restart for API/model changes to take effect.");
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const cleanup = async () => {
    setCleaning(true);
    try {
      const res = await api.runRetentionCleanup(
        settings.retention_processed_days,
        settings.retention_unprocessed_days
      );
      toast.success(
        `Freed ${formatBytes(res.bytes_freed)} from ${res.deleted_count} files`
      );
      const stats = await api.getRetentionStats();
      setStorage(stats);
    } catch (e) {
      toast.error(`Cleanup failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setCleaning(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      {/* API Keys */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">API Keys</CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Both are required. Anthropic powers AI extraction (summaries, action
            items, decisions, requirements, prep briefs). HuggingFace powers
            speaker identification via pyannote. Both are free to start, stored
            only on this machine in{" "}
            <code className="text-[11px]">%LOCALAPPDATA%\MeetingRecorder\config.env</code>.
          </p>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Anthropic */}
          <div className="space-y-2">
            <Label>Anthropic API Key</Label>
            <Input
              type="password"
              value={settings.anthropic_api_key}
              onChange={(e) => update("anthropic_api_key", e.target.value)}
              placeholder="sk-ant-api03-..."
              autoComplete="off"
            />
            <div className="rounded-md border bg-muted/40 p-3 text-xs space-y-1.5">
              <div className="font-medium text-foreground">How to get an Anthropic key:</div>
              <ol className="list-decimal pl-5 space-y-1 text-muted-foreground">
                <li>
                  Sign up at{" "}
                  <a href="https://console.anthropic.com" target="_blank" rel="noreferrer"
                     className="text-primary hover:underline">console.anthropic.com</a>
                </li>
                <li>Add $5-10 of credit (Billing → Buy credits) — a full meeting costs ~$0.05 on Haiku</li>
                <li>
                  Go to{" "}
                  <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer"
                     className="text-primary hover:underline">Settings → API Keys</a>,
                  click <strong>Create Key</strong>
                </li>
                <li>Copy the key (starts with <code className="text-[11px]">sk-ant-api03-</code>) and paste above</li>
              </ol>
            </div>
          </div>

          {/* HuggingFace */}
          <div className="space-y-2">
            <Label>HuggingFace Token</Label>
            <Input
              type="password"
              value={settings.hf_token}
              onChange={(e) => update("hf_token", e.target.value)}
              placeholder="hf_..."
              autoComplete="off"
            />
            <div className="rounded-md border bg-muted/40 p-3 text-xs space-y-1.5">
              <div className="font-medium text-foreground">
                How to get a HuggingFace token (and why there are 3 steps):
              </div>
              <ol className="list-decimal pl-5 space-y-1 text-muted-foreground">
                <li>
                  Sign up at{" "}
                  <a href="https://huggingface.co/join" target="_blank" rel="noreferrer"
                     className="text-primary hover:underline">huggingface.co/join</a>{" "}
                  (free)
                </li>
                <li>
                  Go to{" "}
                  <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer"
                     className="text-primary hover:underline">Settings → Access Tokens</a>,
                  click <strong>Create new token</strong>. <strong>Type: Read</strong> is enough
                  (don&apos;t need Write or Fine-grained). Copy the token
                  (starts with <code className="text-[11px]">hf_</code>) and paste above
                </li>
                <li>
                  <strong>Important:</strong> accept the model terms on BOTH of these pages (click
                  &quot;Agree and access repository&quot;):
                  <ul className="list-disc pl-5 mt-1 space-y-0.5">
                    <li>
                      <a href="https://huggingface.co/pyannote/speaker-diarization-3.1"
                         target="_blank" rel="noreferrer"
                         className="text-primary hover:underline">
                        pyannote/speaker-diarization-3.1
                      </a>
                    </li>
                    <li>
                      <a href="https://huggingface.co/pyannote/segmentation-3.0"
                         target="_blank" rel="noreferrer"
                         className="text-primary hover:underline">
                        pyannote/segmentation-3.0
                      </a>
                    </li>
                  </ul>
                  Without accepting both, speaker diarization will fail with a 403 when models
                  try to download the first time you Process a recording.
                </li>
              </ol>
            </div>
          </div>

          <div className="rounded-md border border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40 px-4 py-3 text-xs text-amber-900 dark:text-amber-200">
            After saving both keys, <strong>restart the app</strong> so the backend reloads
            config and downloads the pyannote models into cache (~200 MB, one-time, happens on
            first Process).
          </div>
        </CardContent>
      </Card>

      {/* Models */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">AI Models</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Whisper Model</Label>
              <Select
                value={settings.whisper_model}
                onValueChange={(v) => v && update("whisper_model", v)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WHISPER_MODELS.map((m) => (
                    <SelectItem key={m} value={m}>{m}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Max Speakers</Label>
              <Input
                type="number"
                min={2}
                max={20}
                value={settings.max_speakers}
                onChange={(e) => update("max_speakers", parseInt(e.target.value) || 10)}
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label>Claude Model</Label>
            <Select
              value={settings.claude_model}
              onValueChange={(v) => v && update("claude_model", v)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CLAUDE_MODELS.map((m) => (
                  <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Email */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Email (Outlook)</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label>Send To</Label>
            <Input
              value={settings.email_to}
              onChange={(e) => update("email_to", e.target.value)}
              placeholder="Leave blank to send to yourself"
            />
          </div>
        </CardContent>
      </Card>

      {/* Calendar */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Calendar</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <Label>Notify before meeting (minutes, 0 = off)</Label>
          <Input
            type="number"
            min={0}
            max={30}
            value={settings.notify_minutes_before}
            onChange={(e) => update("notify_minutes_before", parseInt(e.target.value) || 0)}
          />
        </CardContent>
      </Card>

      {/* Workflow */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Workflow</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <Toggle
            label="Auto-process after recording stops"
            description="Runs transcribe → summarize → action items → requirements → decisions"
            checked={settings.auto_process_after_stop}
            onChange={(v) => update("auto_process_after_stop", v)}
          />
          <Toggle
            label="Auto-draft follow-up email"
            description="Creates an Outlook draft to attendees after processing"
            checked={settings.auto_follow_up_email}
            onChange={(v) => update("auto_follow_up_email", v)}
          />
          <Toggle
            label="Launch on Windows startup"
            description="Adds a shortcut to the Windows Startup folder"
            checked={settings.launch_on_startup}
            onChange={(v) => update("launch_on_startup", v)}
          />
        </CardContent>
      </Card>

      {/* GPU acceleration */}
      <GpuAccelerationCard />

      {/* Retention */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retention</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {storage && (
            <div className="rounded-md bg-muted/60 px-3 py-2 text-sm">
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Current storage</span>
                <span className="font-medium">{formatBytes(storage.total_bytes)}</span>
              </div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                {storage.session_count} sessions · {storage.wav_count} WAV files
              </div>
            </div>
          )}
          <Toggle
            label="Enable automatic cleanup of old audio files"
            description="Only WAV audio is deleted. Transcripts, summaries, action items, decisions stay forever."
            checked={settings.retention_enabled}
            onChange={(v) => update("retention_enabled", v)}
          />
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Processed audio (days)</Label>
              <Input
                type="number"
                min={0}
                max={365}
                value={settings.retention_processed_days}
                onChange={(e) => update("retention_processed_days", parseInt(e.target.value) || 0)}
              />
            </div>
            <div className="space-y-2">
              <Label>Unprocessed audio (days)</Label>
              <Input
                type="number"
                min={0}
                max={365}
                value={settings.retention_unprocessed_days}
                onChange={(e) => update("retention_unprocessed_days", parseInt(e.target.value) || 0)}
              />
            </div>
          </div>
          <Button variant="outline" onClick={cleanup} disabled={cleaning}>
            {cleaning ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Trash2 className="h-4 w-4 mr-2" />}
            Clean up now
          </Button>
        </CardContent>
      </Card>

      {/* Save bar */}
      <div className="sticky bottom-0 -mx-6 border-t border-border bg-background/95 px-6 py-3 backdrop-blur">
        <div className="mx-auto max-w-3xl flex justify-end gap-2">
          <Button onClick={save} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Save className="h-4 w-4 mr-2" />}
            Save Settings
          </Button>
        </div>
      </div>
    </div>
  );
}

function Toggle({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-start gap-3">
      <Switch checked={checked} onCheckedChange={onChange} className="mt-0.5" />
      <div>
        <div className="text-sm font-medium">{label}</div>
        {description && <div className="text-xs text-muted-foreground mt-0.5">{description}</div>}
      </div>
    </div>
  );
}
