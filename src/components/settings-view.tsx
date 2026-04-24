"use client";

import { useEffect, useState } from "react";
import { api, formatBytes, type Settings, type TemplateEntry } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Save, Trash2, Plus, RotateCcw } from "lucide-react";
import { GpuAccelerationCard } from "./gpu-acceleration-card";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const WHISPER_MODELS = ["tiny", "base", "small", "medium", "large"];

// Preset model options per provider. Picking a preset writes the value
// into `claude_model` (the backend reuses that field as the model id
// across providers). The "Custom…" option lets the user type any string,
// so niche models (new OpenRouter releases, fine-tuned Ollama tags) work
// even when they're not on the shortlist.
const ANTHROPIC_MODELS = [
  { value: "claude-haiku-4-5", label: "Claude Haiku 4.5 — cheap, great for summaries" },
  { value: "claude-sonnet-4-6", label: "Claude Sonnet 4.6 — premium (~4× cost)" },
  { value: "claude-opus-4-7", label: "Claude Opus 4.7 — max quality, ~15× cost" },
  { value: "claude-3-5-haiku-latest", label: "Claude Haiku 3.5 — legacy" },
];

// Free-tier selections from OpenRouter as of early 2026. The ":free"
// suffix is required — without it OpenRouter routes to the paid tier.
const OPENROUTER_MODELS = [
  { value: "meta-llama/llama-3.3-70b-instruct:free", label: "Llama 3.3 70B (free)" },
  { value: "google/gemini-2.0-flash-exp:free", label: "Gemini 2.0 Flash (free)" },
  { value: "qwen/qwen-2.5-72b-instruct:free", label: "Qwen 2.5 72B (free)" },
  { value: "deepseek/deepseek-r1:free", label: "DeepSeek R1 (free, reasoning)" },
  { value: "mistralai/mistral-small-3.1-24b-instruct:free", label: "Mistral Small 3.1 (free)" },
  { value: "anthropic/claude-haiku-4-5", label: "Claude Haiku 4.5 (paid pass-through)" },
  { value: "openai/gpt-4o-mini", label: "GPT-4o mini (paid pass-through)" },
];

// Common local models via Ollama. The user still has to pull them with
// `ollama pull <name>` — we can't detect installed ones from here.
const OLLAMA_MODELS = [
  { value: "llama3.1", label: "Llama 3.1 8B (default)" },
  { value: "llama3.3", label: "Llama 3.3 70B" },
  { value: "qwen2.5", label: "Qwen 2.5 7B" },
  { value: "mistral", label: "Mistral 7B" },
  { value: "phi3", label: "Phi-3 3.8B — small + fast" },
];

type ProviderPreset = "anthropic" | "openrouter" | "ollama" | "custom";

function presetFromSettings(s: Settings): ProviderPreset {
  if (s.ai_provider !== "openai") return "anthropic";
  const base = (s.openai_base_url || "").toLowerCase();
  if (base.includes("openrouter")) return "openrouter";
  if (base.includes("localhost") || base.includes("127.0.0.1")) return "ollama";
  return "custom";
}

const OPENROUTER_BASE = "https://openrouter.ai/api/v1";
const OLLAMA_BASE = "http://localhost:11434/v1";

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
          <AIProviderSection settings={settings} update={update} />
        </CardContent>
      </Card>

      {/* Summary Templates */}
      <SummaryTemplatesCard />

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

function AIProviderSection({
  settings, update,
}: {
  settings: Settings;
  update: <K extends keyof Settings>(key: K, value: Settings[K]) => void;
}) {
  const preset = presetFromSettings(settings);

  // Apply a preset: sets ai_provider, openai_base_url, and (when a
  // sensible default exists) claude_model. Touching the API-key field
  // is avoided — users may already have one pasted for a different
  // provider they'll switch back to.
  const applyPreset = (next: ProviderPreset) => {
    if (next === "anthropic") {
      update("ai_provider", "anthropic");
      update("openai_base_url", "");
      if (!ANTHROPIC_MODELS.find((m) => m.value === settings.claude_model)) {
        update("claude_model", ANTHROPIC_MODELS[0].value);
      }
      return;
    }
    update("ai_provider", "openai");
    if (next === "openrouter") {
      update("openai_base_url", OPENROUTER_BASE);
      if (!OPENROUTER_MODELS.find((m) => m.value === settings.claude_model)) {
        update("claude_model", OPENROUTER_MODELS[0].value);
      }
    } else if (next === "ollama") {
      update("openai_base_url", OLLAMA_BASE);
      if (!OLLAMA_MODELS.find((m) => m.value === settings.claude_model)) {
        update("claude_model", OLLAMA_MODELS[0].value);
      }
    } else {
      // Custom — leave URL and model alone so the user can fill them in.
      if (!settings.openai_base_url) update("openai_base_url", "");
    }
  };

  // Which preset list (if any) this provider uses. Custom gets no list —
  // the user types a model id directly.
  const presetModels = preset === "anthropic" ? ANTHROPIC_MODELS
    : preset === "openrouter" ? OPENROUTER_MODELS
    : preset === "ollama" ? OLLAMA_MODELS
    : null;
  const modelIsPreset = presetModels
    ? presetModels.some((m) => m.value === settings.claude_model)
    : false;

  return (
    <div className="space-y-4 border-t pt-4">
      <div className="space-y-2">
        <Label>AI Provider</Label>
        <Select value={preset} onValueChange={(v) => v && applyPreset(v as ProviderPreset)}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="anthropic">Anthropic — Claude (uses Anthropic API key above)</SelectItem>
            <SelectItem value="openrouter">OpenRouter — free-tier Llama / Gemini / Qwen / DeepSeek</SelectItem>
            <SelectItem value="ollama">Ollama (local) — free, runs on your machine</SelectItem>
            <SelectItem value="custom">Custom OpenAI-compatible endpoint</SelectItem>
          </SelectContent>
        </Select>
        <p className="text-[11px] text-muted-foreground">
          {preset === "anthropic" && (
            <>Uses Claude directly. Best quality, but each extraction costs a few cents per meeting.</>
          )}
          {preset === "openrouter" && (
            <>
              Get a free API key at{" "}
              <a href="https://openrouter.ai/settings/keys" className="underline" target="_blank" rel="noreferrer">
                openrouter.ai
              </a>
              . Free-tier models have rate limits (~50 requests/day) but cost $0.
            </>
          )}
          {preset === "ollama" && (
            <>
              Install Ollama from{" "}
              <a href="https://ollama.com/download" className="underline" target="_blank" rel="noreferrer">
                ollama.com
              </a>{" "}
              and run <code className="text-[11px]">ollama pull llama3.1</code> (or your preferred model)
              before saving. Everything stays on your machine. No API key needed.
            </>
          )}
          {preset === "custom" && (
            <>Any service that speaks the OpenAI Chat Completions protocol — LM Studio, vLLM, LocalAI, Together.ai, Groq, etc.</>
          )}
        </p>
      </div>

      {(preset === "openrouter" || preset === "custom") && (
        <div className="space-y-2">
          <Label>
            {preset === "openrouter" ? "OpenRouter API Key" : "API Key"}
          </Label>
          <Input
            type="password"
            value={settings.openai_api_key}
            onChange={(e) => update("openai_api_key", e.target.value)}
            placeholder={preset === "openrouter" ? "sk-or-v1-..." : "Your provider's API key"}
            autoComplete="off"
          />
        </div>
      )}

      {(preset === "ollama" || preset === "custom") && (
        <div className="space-y-2">
          <Label>Base URL</Label>
          <Input
            value={settings.openai_base_url}
            onChange={(e) => update("openai_base_url", e.target.value)}
            placeholder={preset === "ollama" ? OLLAMA_BASE : "https://your-endpoint/v1"}
            autoComplete="off"
          />
          <p className="text-[11px] text-muted-foreground">
            {preset === "ollama"
              ? "Ollama's default. Change only if you run Ollama on a different port."
              : "Must end in /v1 and expose OpenAI-compatible /chat/completions."}
          </p>
        </div>
      )}

      <div className="space-y-2">
        <Label>Model</Label>
        {presetModels && modelIsPreset ? (
          <Select
            value={settings.claude_model}
            onValueChange={(v) => v && update("claude_model", v)}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {presetModels.map((m) => (
                <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>
              ))}
              <SelectItem value="__custom__">Custom (type your own below)</SelectItem>
            </SelectContent>
          </Select>
        ) : (
          <>
            <Input
              value={settings.claude_model}
              onChange={(e) => update("claude_model", e.target.value)}
              placeholder={
                preset === "anthropic" ? "claude-haiku-4-5" :
                preset === "openrouter" ? "meta-llama/llama-3.3-70b-instruct:free" :
                preset === "ollama" ? "llama3.1" :
                "model-id"
              }
              autoComplete="off"
            />
            {presetModels && (
              <button
                type="button"
                onClick={() => update("claude_model", presetModels[0].value)}
                className="text-[11px] text-primary hover:underline"
              >
                ← Back to preset list
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SummaryTemplatesCard() {
  const [templates, setTemplates] = useState<TemplateEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<TemplateEntry | null>(null);
  const [creating, setCreating] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const ts = await api.getTemplates();
      setTemplates(ts);
    } catch (e) {
      toast.error(`Could not load templates: ${e instanceof Error ? e.message : e}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const handleSave = async (name: string, prompt: string) => {
    try {
      await api.upsertTemplate(name, prompt);
      toast.success(`Saved "${name}"`);
      setEditing(null);
      setCreating(false);
      refresh();
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleDelete = async (t: TemplateEntry) => {
    const label = t.is_default ? "Hide" : "Delete";
    const detail = t.is_default
      ? "This is a default template. Hiding it keeps the prompt on disk so you can restore it later."
      : "This permanently removes your custom template.";
    if (!confirm(`${label} template "${t.name}"?\n\n${detail}`)) return;
    try {
      await api.deleteTemplate(t.name);
      toast.success(t.is_default ? "Template hidden" : "Template deleted");
      refresh();
    } catch (e) {
      toast.error(`Delete failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleReset = async (t: TemplateEntry) => {
    if (!confirm(`Reset "${t.name}" back to the shipped default prompt?`)) return;
    try {
      const fresh = await api.resetTemplate(t.name);
      toast.success("Reset to default");
      setEditing(fresh);
      refresh();
    } catch (e) {
      toast.error(`Reset failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">Summary Templates</CardTitle>
            <p className="text-xs text-muted-foreground mt-1">
              Each template is a prompt Claude uses when you click <strong>Summarize</strong> on
              a session. Edit the prompts to match the kind of meetings you actually run, or add
              new ones (e.g. &quot;SOW Kickoff&quot;, &quot;AWS Connect Discovery&quot;).
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-3.5 w-3.5 mr-2" />
            New
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-1">
        {loading && !templates ? (
          <div className="flex justify-center py-4 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        ) : !templates || templates.length === 0 ? (
          <p className="text-sm text-muted-foreground py-4 text-center">
            No templates. Click New to add one.
          </p>
        ) : (
          templates.map((t) => {
            const edited = t.is_default && t.default_prompt !== null
              && t.prompt !== t.default_prompt;
            return (
              <div
                key={t.name}
                className="flex items-center gap-3 rounded-md px-3 py-2 hover:bg-muted/50 cursor-pointer"
                onClick={() => setEditing(t)}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium truncate">{t.name}</span>
                    {t.is_default && (
                      <Badge variant="outline" className="text-[10px]">default</Badge>
                    )}
                    {edited && (
                      <Badge variant="outline" className="text-[10px] border-amber-400 text-amber-700">edited</Badge>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground truncate mt-0.5">
                    {t.prompt.slice(0, 120)}{t.prompt.length > 120 ? "…" : ""}
                  </div>
                </div>
                {t.is_default && edited && (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); handleReset(t); }}
                    className="h-7 w-7 inline-flex items-center justify-center rounded-md hover:bg-accent text-muted-foreground"
                    title="Reset to shipped default"
                  >
                    <RotateCcw className="h-3.5 w-3.5" />
                  </button>
                )}
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); handleDelete(t); }}
                  className="h-7 w-7 inline-flex items-center justify-center rounded-md hover:bg-destructive/10 text-muted-foreground hover:text-destructive"
                  title={t.is_default ? "Hide this default" : "Delete this custom template"}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })
        )}
      </CardContent>
      <TemplateEditDialog
        open={editing !== null}
        initial={editing}
        onOpenChange={(v) => !v && setEditing(null)}
        onSave={handleSave}
      />
      <TemplateEditDialog
        open={creating}
        initial={null}
        onOpenChange={(v) => !v && setCreating(false)}
        onSave={handleSave}
      />
    </Card>
  );
}

function TemplateEditDialog({
  open, initial, onOpenChange, onSave,
}: {
  open: boolean;
  initial: TemplateEntry | null;
  onOpenChange: (v: boolean) => void;
  onSave: (name: string, prompt: string) => Promise<void> | void;
}) {
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [saving, setSaving] = useState(false);
  const isNew = initial === null;

  useEffect(() => {
    if (open) {
      setName(initial?.name || "");
      setPrompt(initial?.prompt || "");
    }
  }, [open, initial]);

  const save = async () => {
    const n = name.trim();
    const p = prompt.trim();
    if (!n || !p) return;
    setSaving(true);
    try {
      await onSave(n, p);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{isNew ? "New Template" : `Edit "${initial?.name}"`}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="space-y-2">
            <Label>Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. AWS Connect Discovery"
              disabled={!isNew}
              autoComplete="off"
            />
            {!isNew && (
              <p className="text-[11px] text-muted-foreground">
                Renaming isn&apos;t supported here — delete this one and create a new name to rename.
              </p>
            )}
          </div>
          <div className="space-y-2">
            <Label>Prompt</Label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={14}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono"
              placeholder="Write the instruction Claude should follow when summarizing meetings of this type…"
            />
            <p className="text-[11px] text-muted-foreground">
              The user&apos;s session notes + meeting transcript are automatically appended after
              this prompt — don&apos;t include a placeholder for the transcript.
            </p>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={save} disabled={!name.trim() || !prompt.trim() || saving}>
            {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-2" /> : <Save className="h-3.5 w-3.5 mr-2" />}
            {isNew ? "Create" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
