"use client";

import { useEffect, useState } from "react";
import { api, formatBytes, type Settings, type TemplateEntry, type CoPilotPromptEntry } from "@/lib/api";
import { estimateCopilotCost, formatUsd } from "@/lib/copilot-cost";
import { confirmDialog } from "@/lib/confirm";
import { toast } from "sonner";
import { Loader2, Save, Trash2, Plus, RotateCcw } from "lucide-react";
import { GpuAccelerationCard } from "./gpu-acceleration-card";
import { KnownSpeakersSection } from "./known-speakers-section";
import { SemanticIndexSection } from "./semantic-index-section";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
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
  // Pinned dated id, NOT the "-latest" alias: the API returns 404
  // not_found for claude-3-5-haiku-latest, so the alias is unusable here.
  { value: "claude-3-5-haiku-20241022", label: "Claude Haiku 3.5 — legacy" },
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

// Groq — generous free tier, fastest hosted inference available. Models
// rotate; current free roster as of early 2026.
const GROQ_MODELS = [
  { value: "llama-3.3-70b-versatile", label: "Llama 3.3 70B Versatile (free, fast)" },
  { value: "llama-3.1-8b-instant", label: "Llama 3.1 8B Instant (free, fastest)" },
  { value: "mixtral-8x7b-32768", label: "Mixtral 8x7B (free)" },
  { value: "gemma2-9b-it", label: "Gemma 2 9B (free)" },
];

// Google Gemini — free tier via the OpenAI-compatible compat endpoint.
// Same model id format as Google's native API.
const GEMINI_MODELS = [
  { value: "gemini-2.0-flash", label: "Gemini 2.0 Flash (free)" },
  { value: "gemini-2.0-flash-lite", label: "Gemini 2.0 Flash-Lite (free, faster)" },
  { value: "gemini-1.5-flash", label: "Gemini 1.5 Flash (free)" },
];

type ProviderPreset =
  | "anthropic"
  | "openrouter"
  | "ollama"
  | "groq"
  | "gemini"
  | "custom";

function presetFromSettings(s: Settings): ProviderPreset {
  if (s.ai_provider !== "openai") return "anthropic";
  const base = (s.openai_base_url || "").toLowerCase();
  if (base.includes("openrouter")) return "openrouter";
  if (base.includes("groq.com")) return "groq";
  if (base.includes("generativelanguage.googleapis")) return "gemini";
  if (base.includes("localhost") || base.includes("127.0.0.1")) return "ollama";
  return "custom";
}

const OPENROUTER_BASE = "https://openrouter.ai/api/v1";
const OLLAMA_BASE = "http://localhost:11434/v1";
const GROQ_BASE = "https://api.groq.com/openai/v1";
const GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai";

// User-facing path to the config.env file so the "stored on this machine"
// note in the API Keys section reflects the actual filesystem layout. We
// sniff platform from navigator.userAgent because Tauri exposes the host
// OS that way; SSR safety: fall back to the Windows path during render
// on the server (build) where `navigator` is undefined.
function configPathHint(): string {
  if (typeof navigator === "undefined") {
    return "%LOCALAPPDATA%\\MeetingRecorder\\config.env";
  }
  const ua = navigator.userAgent.toLowerCase();
  if (ua.includes("mac")) {
    return "~/Library/Application Support/MeetingRecorder/config.env";
  }
  if (ua.includes("linux")) {
    return "~/.config/MeetingRecorder/config.env";
  }
  return "%LOCALAPPDATA%\\MeetingRecorder\\config.env";
}

// True when the host OS is macOS, used to swap Windows-specific labels
// (Outlook draft / Windows Startup folder) for Mac equivalents (Mail.app
// or Outlook for Mac / LaunchAgent). SSR-safe.
function isMac(): boolean {
  if (typeof navigator === "undefined") return false;
  return navigator.userAgent.toLowerCase().includes("mac");
}

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

  // Functional updater — critical when callers fire multiple update()s in
  // a single tick (e.g. applyPreset flips ai_provider + openai_base_url +
  // claude_model in sequence). The previous spread-from-closure form
  // captured `settings` at render time, so back-to-back calls clobbered
  // each other and only the last update stuck. The visible symptom was:
  // pick "OpenRouter" from the AI Provider dropdown, the model name
  // changes but the provider label stubbornly stays on "anthropic".
  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => {
    setSettings((prev) => (prev ? { ...prev, [key]: value } : prev));
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
            HuggingFace is always required (powers speaker identification via
            pyannote). The Anthropic key is only required when AI Provider is
            set to Anthropic — pick a different provider in AI Models below to
            use OpenRouter / Ollama / a custom OpenAI-compatible endpoint
            instead. Both are free to start, stored only on this machine in{" "}
            <code className="text-[11px]">{configPathHint()}</code>.
          </p>
        </CardHeader>
        <CardContent className="space-y-5">
          {/* Anthropic — only relevant when AI Provider = Anthropic. Hidden
              otherwise so the user doesn't see two API-key fields competing
              for attention. The value persists in settings state across the
              hide/show cycle, so toggling back to Anthropic restores it. */}
          {settings.ai_provider === "anthropic" && (
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
          )}

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

      {/* Recordings folder (v2.4: cross-device sync support) */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Recordings Folder</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <Label>Where Meeting Recorder saves session audio, transcripts, and client list</Label>
            <div className="flex gap-2">
              <Input
                value={settings.recordings_dir}
                onChange={(e) => update("recordings_dir", e.target.value)}
                placeholder="/path/to/recordings"
                className="font-mono text-sm"
              />
              <Button
                type="button"
                variant="outline"
                onClick={async () => {
                  try {
                    const { open } = await import("@tauri-apps/plugin-dialog");
                    const picked = await open({
                      directory: true,
                      multiple: false,
                      defaultPath: settings.recordings_dir || undefined,
                      title: "Choose recordings folder",
                    });
                    if (typeof picked === "string" && picked) {
                      update("recordings_dir", picked);
                    }
                  } catch (e) {
                    toast.error(
                      `Folder picker unavailable: ${(e as Error).message ?? e}`);
                  }
                }}
              >
                Browse…
              </Button>
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Point this at a cloud-synced folder (OneDrive, iCloud Drive,
              Dropbox) to sync sessions, clients, and summary templates
              across your devices. Existing sessions stay where they are —
              new recordings go to the new location starting after Save.
              The app needs to restart for the change to take full effect.
            </p>
          </div>
        </CardContent>
      </Card>

      <AppUpdatesCard />

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

      {/* Auto-record blocklist patterns — substring matches that suppress
          auto-recording for any meeting whose subject contains the
          pattern (case-insensitive). Per-meeting exact blocks still live
          on the Record view (the "No auto" toggle on each tile); this
          card is for the catch-all patterns like "canceled". */}
      <AutoRecordBlocklistPatternsCard />

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
            label="Live transcription during recording"
            description="Streams a rolling preview of what's being said while you record. Disable on slower machines or for calls where the live preview is noisy. The canonical post-stop transcript runs regardless."
            checked={settings.live_transcription_enabled}
            onChange={(v) => update("live_transcription_enabled", v)}
          />
          <Toggle
            label="Live Co-Pilot (beta)"
            description="Every ~45s during a recording, asks the configured LLM for three short bullet lists (clarifying questions, risks, suggested follow-ups) based on the last few minutes of conversation. Requires Live transcription to also be on. Costs an LLM call per tick — about $0.10–$0.20 per hour on Anthropic Haiku."
            checked={settings.live_copilot_enabled}
            onChange={(v) => update("live_copilot_enabled", v)}
          />
          <Toggle
            label="Auto-draft follow-up email"
            description={isMac()
              ? "Creates a Mail.app (or Outlook for Mac) draft to attendees after processing"
              : "Creates an Outlook draft to attendees after processing"}
            checked={settings.auto_follow_up_email}
            onChange={(v) => update("auto_follow_up_email", v)}
          />
          <Toggle
            label={isMac() ? "Launch on login" : "Launch on Windows startup"}
            description={isMac()
              ? "Installs a LaunchAgent so the app starts automatically when you log in"
              : "Adds a shortcut to the Windows Startup folder"}
            checked={settings.launch_on_startup}
            onChange={(v) => update("launch_on_startup", v)}
          />
        </CardContent>
      </Card>

      {/* Live Co-Pilot model — only rendered when the toggle above is
          on. Optional override so users can route the 45s tick calls
          to a free / local model (Ollama, OpenRouter free tier) while
          post-meeting summaries stay on the main provider. Empty
          override → reuses the main provider, same as Phase A. */}
      {settings.live_copilot_enabled && (
        <LiveCoPilotModelCard settings={settings} update={update} />
      )}

      {/* Polling cadence + live cost estimate. The wide tick is the
          existing 45s baseline; the hot tick (off by default) layers a
          tighter window on top for time-sensitive coaching. Cost
          estimate recalculates as the user touches the inputs or
          switches providers. */}
      {settings.live_copilot_enabled && (
        <CoPilotCadenceCard settings={settings} update={update} />
      )}

      {/* Custom coaching context — free-text the SA pins per-engagement.
          Appended to every co-pilot tick prompt as authoritative role /
          topic framing. The biggest lever the user has to make the
          co-pilot suggestions actually relevant, especially when running
          on a smaller / local model that needs more explicit grounding. */}
      {settings.live_copilot_enabled && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Coaching context{" "}
              <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                optional
              </span>
            </CardTitle>
            <p className="text-xs text-muted-foreground mt-1">
              Pinned context the co-pilot uses on every tick. Treat this
              like a persistent system prompt addition — the model treats
              it as authoritative role and topic framing. Especially
              important when using a smaller / local model (Ollama,
              free-tier OpenRouter) where the baked-in SA framing alone
              isn&apos;t enough to keep suggestions specific.
            </p>
          </CardHeader>
          <CardContent className="space-y-2">
            <Label>Pinned context for this engagement / role</Label>
            <Textarea
              value={settings.copilot_custom_context || ""}
              onChange={(e) =>
                update("copilot_custom_context", e.target.value)
              }
              placeholder={
                "e.g. Current focus is a Genesys → Amazon Connect migration "
                + "for a US healthcare client (~800 agents). PHI compliance "
                + "drives every architectural choice. Watch for scope creep "
                + "around Salesforce screen-pop and FAX intake."
              }
              rows={5}
              className="resize-y text-sm font-mono"
            />
            <p className="text-[10px] text-muted-foreground italic">
              Persists across recordings. Update whenever you move between
              engagements. Clear it to fall back to the SA-default prompt
              only.
            </p>
          </CardContent>
        </Card>
      )}

      {/* Two editable libraries the Live Co-Pilot composes at tick
          time: persona ("you are an SA / sales / executive") and
          meeting-type modifier ("this is a discovery call / SOW
          review / status sync"). Both seeded with defaults the user
          can edit, reset, or add to. Gated on the same toggle as
          everything else co-pilot-related. */}
      {settings.live_copilot_enabled && (
        <CoPilotPromptLibraryCard
          title="Co-Pilot Modes"
          description="The persona framing the co-pilot uses on every tick. Pick the active mode from the dropdown in the panel header while recording. Three defaults ship — edit them or add your own."
          newItemPlaceholder="e.g. Engineering Lead, Customer Success"
          load={() => api.getCopilotModes()}
          save={(name, prompt) => api.putCopilotMode(name, prompt)}
          remove={(name) => api.deleteCopilotMode(name)}
          reset={(name) => api.resetCopilotMode(name)}
        />
      )}
      {settings.live_copilot_enabled && (
        <CoPilotPromptLibraryCard
          title="Co-Pilot Meeting Types"
          description="The meeting-type modifier layered on top of the mode — Discovery, SOW Review, Status, Technical, etc. Any mode × any meeting type combination just works."
          newItemPlaceholder="e.g. Post-Mortem, Quarterly Business Review"
          load={() => api.getCopilotMeetingTypes()}
          save={(name, prompt) => api.putCopilotMeetingType(name, prompt)}
          remove={(name) => api.deleteCopilotMeetingType(name)}
          reset={(name) => api.resetCopilotMeetingType(name)}
        />
      )}

      {/* Auto-stop watchdog — protects against forgotten recordings
          running for hours. All trigger settings live together so
          users see the full safety net at a glance, with the
          always-on hard cap at the bottom. */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Auto-stop</CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Catches forgotten recordings. Warnings appear in the app
            and fire a native OS notification; auto-stops actually end
            the recording (and run normal post-stop processing). All
            values are minutes. Set to <strong>0</strong> to disable a
            given trigger.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <NumberRow
            label="Warn after silence"
            description="Show a banner + native notification when the room has been quiet for this long. Default: 5 min."
            value={settings.silence_warn_min}
            onChange={(v) => update("silence_warn_min", v)}
            unit="min"
            max={120}
          />
          <NumberRow
            label="Auto-stop after silence"
            description="End the recording when the room has been quiet for this long. Off by default — opt in if you'd rather not deal with manually stopping."
            value={settings.silence_stop_min}
            onChange={(v) => update("silence_stop_min", v)}
            unit="min"
            max={120}
          />
          <NumberRow
            label="Warn after meeting end"
            description="When you start a recording from a calendar tile, warn this many minutes after the scheduled end time. Default: 5 min."
            value={settings.overrun_warn_min}
            onChange={(v) => update("overrun_warn_min", v)}
            unit="min"
            max={120}
          />
          <NumberRow
            label="Auto-stop after meeting end"
            description="Stop the recording N minutes after the scheduled end. Only fires when you started from a calendar tile. Off by default."
            value={settings.overrun_stop_min}
            onChange={(v) => update("overrun_stop_min", v)}
            unit="min"
            max={120}
          />
          <NumberRow
            label="Hard cap (always on)"
            description="Absolute maximum recording length. The safety net for 'I forgot the recording was running.' Default: 4 hours. Set to 0 to disable entirely (not recommended)."
            value={settings.hard_cap_hours}
            onChange={(v) => update("hard_cap_hours", v)}
            unit="hr"
            max={24}
          />
        </CardContent>
      </Card>

      {/* GPU acceleration */}
      <GpuAccelerationCard />

      {/* Known speakers (cross-session voice fingerprints) */}
      <KnownSpeakersSection />

      {/* Semantic search index (cross-session vector retrieval) */}
      <SemanticIndexSection />

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

// Compact label + number-input row for the auto-stop section. Used
// instead of plain Toggle because each trigger has a numeric threshold
// the user might want to tune (e.g. "warn at 10 min, not 5"). Setting
// the value to 0 disables the trigger — handled by the backend.
function NumberRow({
  label,
  description,
  value,
  onChange,
  unit,
  min = 0,
  max = 60,
}: {
  label: string;
  description?: string;
  value: number;
  onChange: (v: number) => void;
  unit: string;
  min?: number;
  max?: number;
}) {
  return (
    <div className="flex items-start gap-3">
      <Input
        type="number"
        value={value}
        min={min}
        max={max}
        onChange={(e) => onChange(Math.max(min, parseInt(e.target.value || "0", 10) || 0))}
        className="w-20 h-8 text-sm tabular-nums shrink-0 mt-0.5"
      />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium">
          {label}{" "}
          <span className="text-xs font-normal text-muted-foreground">
            ({unit})
          </span>
        </div>
        {description && (
          <div className="text-xs text-muted-foreground mt-0.5">
            {description}
          </div>
        )}
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

  // OpenRouter's free roster rotates constantly — fetch it live so the
  // dropdown never goes stale (the bundled OPENROUTER_MODELS is only a
  // no-network fallback). Stable non-":free" entries (paid pass-through)
  // are kept appended since those ids don't rotate.
  const [liveOpenrouter, setLiveOpenrouter] = useState<
    { value: string; label: string }[] | null
  >(null);
  useEffect(() => {
    if (preset !== "openrouter") return;
    let cancelled = false;
    api
      .getFreeModels("openrouter")
      .then((r) => {
        if (!cancelled && r.models && r.models.length) {
          const passThrough = OPENROUTER_MODELS.filter(
            (m) => !m.value.endsWith(":free"),
          );
          setLiveOpenrouter([...r.models, ...passThrough]);
        }
      })
      .catch(() => {
        /* keep bundled fallback */
      });
    return () => {
      cancelled = true;
    };
  }, [preset]);
  const openrouterModels =
    liveOpenrouter && liveOpenrouter.length
      ? liveOpenrouter
      : OPENROUTER_MODELS;

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
      if (!openrouterModels.find((m) => m.value === settings.claude_model)) {
        update("claude_model", openrouterModels[0].value);
      }
    } else if (next === "ollama") {
      update("openai_base_url", OLLAMA_BASE);
      if (!OLLAMA_MODELS.find((m) => m.value === settings.claude_model)) {
        update("claude_model", OLLAMA_MODELS[0].value);
      }
    } else if (next === "groq") {
      update("openai_base_url", GROQ_BASE);
      if (!GROQ_MODELS.find((m) => m.value === settings.claude_model)) {
        update("claude_model", GROQ_MODELS[0].value);
      }
    } else if (next === "gemini") {
      update("openai_base_url", GEMINI_BASE);
      if (!GEMINI_MODELS.find((m) => m.value === settings.claude_model)) {
        update("claude_model", GEMINI_MODELS[0].value);
      }
    } else {
      // Custom — leave URL and model alone so the user can fill them in.
      if (!settings.openai_base_url) update("openai_base_url", "");
    }
  };

  // Which preset list (if any) this provider uses. Custom gets no list —
  // the user types a model id directly.
  const presetModels = preset === "anthropic" ? ANTHROPIC_MODELS
    : preset === "openrouter" ? openrouterModels
    : preset === "ollama" ? OLLAMA_MODELS
    : preset === "groq" ? GROQ_MODELS
    : preset === "gemini" ? GEMINI_MODELS
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
            <SelectItem value="groq">Groq — free, fastest hosted inference (Llama, Mixtral, Gemma)</SelectItem>
            <SelectItem value="gemini">Google Gemini — free tier (Gemini 2.0 Flash)</SelectItem>
            <SelectItem value="openrouter">OpenRouter — free-tier Llama / Gemini / Qwen / DeepSeek</SelectItem>
            <SelectItem value="ollama">Ollama (local) — free, runs on your machine</SelectItem>
            <SelectItem value="custom">Custom OpenAI-compatible endpoint</SelectItem>
          </SelectContent>
        </Select>
        <p className="text-[11px] text-muted-foreground">
          {preset === "anthropic" && (
            <>Uses Claude directly. Best quality, but each extraction costs a few cents per meeting.</>
          )}
          {preset === "groq" && (
            <>
              Get a free API key at{" "}
              <a href="https://console.groq.com/keys" className="underline" target="_blank" rel="noreferrer">
                console.groq.com
              </a>
              . Generous free tier, very fast inference (often &lt;1s for a meeting summary).
              Paste the key in the OpenAI API Key field below.
            </>
          )}
          {preset === "gemini" && (
            <>
              Get a free API key at{" "}
              <a href="https://aistudio.google.com/apikey" className="underline" target="_blank" rel="noreferrer">
                aistudio.google.com
              </a>
              . Free tier with daily request limits — fine for personal use.
              Paste the key in the OpenAI API Key field below.
            </>
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

      {(preset === "openrouter" || preset === "groq" || preset === "gemini" || preset === "custom") && (
        <div className="space-y-2">
          <Label>
            {preset === "openrouter" ? "OpenRouter API Key"
              : preset === "groq" ? "Groq API Key"
              : preset === "gemini" ? "Gemini API Key"
              : "API Key"}
          </Label>
          <Input
            type="password"
            value={settings.openai_api_key}
            onChange={(e) => update("openai_api_key", e.target.value)}
            placeholder={
              preset === "openrouter" ? "sk-or-v1-..."
                : preset === "groq" ? "gsk_..."
                : preset === "gemini" ? "AIza..."
                : "Your provider's API key"
            }
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

/**
 * Update-availability card. Queries GitHub Releases on mount, compares
 * to the running app version, and surfaces a button that opens the
 * release page in the user's default browser when a newer version
 * exists. Intentionally NOT an in-place updater — see src/lib/updater.ts
 * for the rationale.
 *
 * Failures (no network, GitHub rate limit, deleted repo) collapse to
 * an "Update check unavailable" line rather than an error toast.
 */
// Compact override panel for the Live Co-Pilot's tick model. The main
// AIProviderSection already does a lot — full preset list, model-id
// dropdowns, key-acquisition instructions — so we don't try to repeat
// it here. Power users opting in to a different live model can paste
// a base URL + key + model id directly.
//
// Empty `live_ai_provider` = "use the main provider" (Phase A behavior).
// "anthropic" + blank live_anthropic_api_key reuses the main key.
// "openai" + base URL = OpenRouter / Ollama / Groq / Gemini / custom.
function LiveCoPilotModelCard({
  settings, update,
}: {
  settings: Settings;
  update: <K extends keyof Settings>(key: K, value: Settings[K]) => void;
}) {
  const useOverride = (settings.live_ai_provider || "").trim() !== "";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Live Co-Pilot model{" "}
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            optional
          </span>
        </CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          By default the co-pilot tick uses your main AI Provider above.
          Override here to route the 45-second tick calls to something
          cheaper or local — e.g. <strong>Ollama</strong> (free, runs on
          your machine) or a free <strong>OpenRouter</strong> model —
          while post-meeting summaries stay on your main provider.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <Toggle
          label="Use a different model for live ticks"
          description="When off, the co-pilot reuses the main AI Provider configured above."
          checked={useOverride}
          onChange={(on) => {
            if (on) {
              // Default to an OpenAI-compatible target with empty
              // fields — the user fills in base URL + key + model.
              update("live_ai_provider", "openai");
            } else {
              // Clear everything so the backend falls back cleanly.
              update("live_ai_provider", "");
              update("live_claude_model", "");
              update("live_openai_api_key", "");
              update("live_openai_base_url", "");
              update("live_anthropic_api_key", "");
            }
          }}
        />

        {useOverride && (
          <>
            <div className="space-y-2">
              <Label>Provider family</Label>
              <Select
                value={settings.live_ai_provider}
                onValueChange={(v) => v && update("live_ai_provider", v)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="anthropic">
                    Anthropic (e.g. claude-haiku-4-5)
                  </SelectItem>
                  <SelectItem value="openai">
                    OpenAI-compatible (Ollama, OpenRouter, Groq, Gemini,
                    custom)
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>Model id</Label>
              <Input
                value={settings.live_claude_model}
                onChange={(e) =>
                  update("live_claude_model", e.target.value)
                }
                placeholder={
                  settings.live_ai_provider === "anthropic"
                    ? "claude-haiku-4-5"
                    : "llama3.1, gpt-oss:20b, meta-llama/llama-3.3-70b-instruct:free, …"
                }
                className="font-mono text-sm"
              />
              <p className="text-[11px] text-muted-foreground">
                Leave blank to reuse the main provider&apos;s model id.
              </p>
            </div>

            {settings.live_ai_provider === "openai" && (
              <>
                <div className="space-y-2">
                  <Label>Base URL</Label>
                  <Input
                    value={settings.live_openai_base_url}
                    onChange={(e) =>
                      update("live_openai_base_url", e.target.value)
                    }
                    placeholder="http://localhost:11434/v1   or   https://openrouter.ai/api/v1"
                    className="font-mono text-sm"
                  />
                  <p className="text-[11px] text-muted-foreground">
                    Ollama: <code>http://localhost:11434/v1</code>{" "}
                    (install from{" "}
                    <a
                      href="https://ollama.com/download"
                      className="underline"
                      target="_blank"
                      rel="noreferrer"
                    >
                      ollama.com
                    </a>
                    , then <code>ollama pull llama3.1</code>).
                    OpenRouter: <code>https://openrouter.ai/api/v1</code>.
                  </p>
                </div>

                <div className="space-y-2">
                  <Label>API key</Label>
                  <Input
                    type="password"
                    value={settings.live_openai_api_key}
                    onChange={(e) =>
                      update("live_openai_api_key", e.target.value)
                    }
                    placeholder="sk-or-... (any non-empty string for Ollama)"
                    autoComplete="off"
                  />
                </div>
              </>
            )}

            {settings.live_ai_provider === "anthropic" && (
              <div className="space-y-2">
                <Label>Anthropic API key (optional)</Label>
                <Input
                  type="password"
                  value={settings.live_anthropic_api_key}
                  onChange={(e) =>
                    update("live_anthropic_api_key", e.target.value)
                  }
                  placeholder="Leave blank to reuse the main Anthropic key"
                  autoComplete="off"
                />
              </div>
            )}

            <div className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
              Tip: cost guidance. Anthropic Haiku ≈ $0.10–$0.20 per hour
              of meeting. Ollama (local) = $0. OpenRouter&apos;s free
              tier = $0 with a ~50 req/day cap (one meeting ≈ 80 ticks,
              so the cap kicks in after one long meeting).
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// Auto-record substring patterns. Each pattern is matched
// case-insensitively against the meeting subject; if it appears anywhere
// the meeting is skipped. The user's exact "this specific meeting"
// blocks still live on the Record view per-tile; this card is for
// patterns that apply across many meetings — most notably the
// Outlook-prefixed "Canceled: …" series.
function AutoRecordBlocklistPatternsCard() {
  const [patterns, setPatterns] = useState<string[]>([]);
  const [exactCount, setExactCount] = useState(0);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    try {
      const r = await api.getAutoRecordBlocklist();
      setPatterns(r.patterns || []);
      setExactCount(r.subjects?.length ?? 0);
    } catch {
      /* ignore — empty state is fine */
    }
  };

  useEffect(() => { void reload(); }, []);

  const add = async () => {
    const v = input.trim();
    if (!v) return;
    setBusy(true);
    try {
      const r = await api.addAutoRecordBlocklistPattern(v);
      setPatterns(r.patterns || []);
      setInput("");
      toast.success(`Auto-record blocked for meetings containing "${v}"`);
    } catch (e) {
      toast.error(`Couldn't add: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (p: string) => {
    setBusy(true);
    try {
      const r = await api.removeAutoRecordBlocklistPattern(p);
      setPatterns(r.patterns || []);
    } catch (e) {
      toast.error(`Couldn't remove: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Auto-record skip patterns</CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          Don&apos;t auto-record any meeting whose title <em>contains</em>{" "}
          one of these substrings (case-insensitive). Useful for the
          Outlook <strong>&quot;Canceled: …&quot;</strong> prefix or any other
          catch-all you want to skip without flagging individual meetings.
          Per-meeting exact blocks still live on the Record view&apos;s
          <em> No auto</em> toggle.
          {exactCount > 0 && (
            <span className="block mt-1 italic">
              {exactCount} exact-subject block{exactCount === 1 ? "" : "s"}{" "}
              also active (manage from the Record view).
            </span>
          )}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex gap-2">
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
            placeholder="e.g. canceled"
            disabled={busy}
          />
          <Button
            onClick={add}
            disabled={busy || !input.trim()}
            size="default"
          >
            <Plus className="h-4 w-4 mr-1" />
            Add
          </Button>
        </div>
        {patterns.length === 0 ? (
          <p className="text-xs italic text-muted-foreground">
            No patterns yet. Add &quot;canceled&quot; to skip any meeting
            that Outlook prefixes with &quot;Canceled:&quot; when the
            organizer cancels it.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {patterns.map((p) => (
              <Badge
                key={p}
                variant="secondary"
                className="text-xs gap-1 py-1 pl-2 pr-1"
              >
                {p}
                <button
                  onClick={() => remove(p)}
                  disabled={busy}
                  className="ml-1 inline-flex items-center justify-center rounded hover:bg-destructive/15 hover:text-destructive transition-colors disabled:opacity-50 h-4 w-4"
                  title={`Remove pattern "${p}"`}
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </Badge>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function AppUpdatesCard() {
  type LatestRelease = {
    tag: string;
    version: string;
    url: string;
    body: string;
    publishedAt: string;
    // Mirrors src/lib/updater.ts LatestRelease — needed so the value
    // can be handed to downloadUpdate() without a type error.
    assets: { name: string; url: string }[];
  };
  type State =
    | { kind: "loading"; current: string }
    | { kind: "up-to-date"; current: string }
    | { kind: "available"; current: string; release: LatestRelease }
    | { kind: "unknown"; current: string; reason: string };
  const [state, setState] = useState<State>({ kind: "loading", current: "…" });

  const runCheck = async () => {
    setState((prev) => ({ kind: "loading", current: prev.current }));
    const { checkForUpdate } = await import("@/lib/updater");
    const result = await checkForUpdate();
    if (result.kind === "available") {
      setState({
        kind: "available",
        current: result.currentVersion,
        release: result.release,
      });
    } else if (result.kind === "up-to-date") {
      setState({ kind: "up-to-date", current: result.currentVersion });
    } else {
      setState({
        kind: "unknown",
        current: result.currentVersion,
        reason: result.reason,
      });
    }
  };

  useEffect(() => { runCheck(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const openDownload = async () => {
    if (state.kind !== "available") return;
    const { downloadUpdate } = await import("@/lib/updater");
    const asset = await downloadUpdate(state.release);
    toast.success(
      asset
        ? `Downloading ${asset} — run it when it finishes to update.`
        : "Opened the releases page — pick the installer for your platform.",
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">App Updates</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm min-w-0">
            <div>
              Current version: <strong>v{state.current || "…"}</strong>
            </div>
            {state.kind === "loading" && (
              <div className="text-xs text-muted-foreground mt-1">
                <Loader2 className="inline h-3 w-3 animate-spin mr-1" />
                Checking GitHub…
              </div>
            )}
            {state.kind === "up-to-date" && (
              <div className="text-xs text-muted-foreground mt-1">
                You&apos;re on the latest release.
              </div>
            )}
            {state.kind === "available" && (
              <div className="text-xs text-primary mt-1">
                Update available: <strong>v{state.release.version}</strong>
                {state.release.publishedAt
                  ? ` · ${new Date(state.release.publishedAt).toLocaleDateString()}`
                  : ""}
              </div>
            )}
            {state.kind === "unknown" && (
              <div className="text-xs text-muted-foreground mt-1">
                Update check unavailable: {state.reason}
              </div>
            )}
          </div>
          <Button
            type="button"
            variant={state.kind === "available" ? "default" : "outline"}
            onClick={state.kind === "available" ? openDownload : runCheck}
            disabled={state.kind === "loading"}
          >
            {state.kind === "available" ? "Download Update" : "Check Now"}
          </Button>
        </div>
        {state.kind === "available" && state.release.body && (
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
              Release notes
            </summary>
            <pre className="mt-2 whitespace-pre-wrap font-sans bg-muted/40 rounded p-2 max-h-64 overflow-y-auto">
              {state.release.body}
            </pre>
          </details>
        )}
        <p className="text-xs text-muted-foreground">
          Update check queries{" "}
          <code>github.com/joshuarodriguez82/meeting-recorder-v2/releases</code>.
          Clicking <strong>Download Update</strong> downloads the correct
          installer for your OS directly (Windows <code>.exe</code>/
          <code>.msi</code>, macOS <code>.zip</code>) — run it to update.
          The current app keeps running until you replace it. (Not a
          silent in-place updater — that needs code signing the build
          doesn&apos;t have yet.)
        </p>
      </CardContent>
    </Card>
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
    if (!(await confirmDialog(`${label} template "${t.name}"?\n\n${detail}`, { title: label }))) return;
    try {
      await api.deleteTemplate(t.name);
      toast.success(t.is_default ? "Template hidden" : "Template deleted");
      refresh();
    } catch (e) {
      toast.error(`Delete failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleReset = async (t: TemplateEntry) => {
    if (!(await confirmDialog(`Reset "${t.name}" back to the shipped default prompt?`, { title: "Reset template" }))) return;
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

// Reusable list + editor for either the Co-Pilot Modes or the Meeting
// Types library. Both have identical shape — list of CoPilotPromptEntry,
// edit/save/reset/delete operations — so we pass the API methods in
// and stamp out two cards. Same edit-and-reset semantics as the
// SummaryTemplatesCard above so user mental model is consistent.
function CoPilotPromptLibraryCard({
  title, description, newItemPlaceholder, load, save, remove, reset,
}: {
  title: string;
  description: string;
  newItemPlaceholder: string;
  load: () => Promise<CoPilotPromptEntry[]>;
  save: (name: string, prompt: string) => Promise<CoPilotPromptEntry>;
  remove: (name: string) => Promise<unknown>;
  reset: (name: string) => Promise<CoPilotPromptEntry>;
}) {
  const [entries, setEntries] = useState<CoPilotPromptEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<CoPilotPromptEntry | null>(null);
  const [creating, setCreating] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      const xs = await load();
      setEntries(xs);
    } catch (e) {
      toast.error(`Could not load: ${e instanceof Error ? e.message : e}`);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  const handleSave = async (name: string, prompt: string) => {
    try {
      await save(name, prompt);
      toast.success(`Saved "${name}"`);
      setEditing(null);
      setCreating(false);
      refresh();
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleDelete = async (t: CoPilotPromptEntry) => {
    const label = t.is_default ? "Hide" : "Delete";
    const detail = t.is_default
      ? "This is a default — hiding it keeps the prompt on disk so you can restore it later."
      : "This permanently removes your custom entry.";
    if (!(await confirmDialog(`${label} "${t.name}"?\n\n${detail}`, { title: label }))) return;
    try {
      await remove(t.name);
      toast.success(t.is_default ? "Hidden" : "Deleted");
      refresh();
    } catch (e) {
      toast.error(`Delete failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const handleReset = async (t: CoPilotPromptEntry) => {
    if (!(await confirmDialog(`Reset "${t.name}" to the shipped default?`, { title: "Reset" }))) return;
    try {
      const fresh = await reset(t.name);
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
            <CardTitle className="text-base">{title}</CardTitle>
            <p className="text-xs text-muted-foreground mt-1">{description}</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => setCreating(true)}>
            <Plus className="h-3.5 w-3.5 mr-2" />
            New
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-1">
        {loading && !entries ? (
          <div className="flex justify-center py-4 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        ) : !entries || entries.length === 0 ? (
          <p className="text-sm text-muted-foreground py-4 text-center">
            None yet. Click New to add one.
          </p>
        ) : (
          entries.map((t) => {
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
                  title={t.is_default ? "Hide this default" : "Delete"}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })
        )}
      </CardContent>
      <CoPilotPromptEditDialog
        open={editing !== null}
        initial={editing}
        onOpenChange={(v) => !v && setEditing(null)}
        onSave={handleSave}
      />
      <CoPilotPromptEditDialog
        open={creating}
        initial={null}
        onOpenChange={(v) => !v && setCreating(false)}
        onSave={handleSave}
        placeholder={newItemPlaceholder}
      />
    </Card>
  );
}

function CoPilotPromptEditDialog({
  open, initial, onOpenChange, onSave, placeholder = "",
}: {
  open: boolean;
  initial: CoPilotPromptEntry | null;
  onOpenChange: (v: boolean) => void;
  onSave: (name: string, prompt: string) => Promise<void> | void;
  placeholder?: string;
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
          <DialogTitle>{isNew ? "New entry" : `Edit "${initial?.name}"`}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="space-y-2">
            <Label>Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={placeholder}
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
              className="w-full rounded-md border bg-background p-3 text-xs font-mono resize-y leading-relaxed"
              placeholder="The role / topic framing. The JSON output rules are appended automatically — you don't need to repeat them."
            />
            <p className="text-[11px] text-muted-foreground italic">
              The JSON output schema (clarifying_questions / risks /
              follow_ups) is appended by the system after this prompt —
              don&apos;t restate it. Edit only the role / topic framing.
            </p>
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={save} disabled={saving || !name.trim() || !prompt.trim()}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Save className="h-4 w-4 mr-2" />}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// Live Co-Pilot polling cadence — two intervals + a live cost
// estimator. Helps the user trade response latency against LLM cost
// without having to guess at the math. Estimator reads provider /
// model / base URL straight from settings (live override wins over
// main) so the numbers reflect what's ACTUALLY going to run.
function CoPilotCadenceCard({
  settings, update,
}: {
  settings: Settings;
  update: <K extends keyof Settings>(key: K, value: Settings[K]) => void;
}) {
  const wide = settings.live_copilot_wide_interval_sec || 45;
  const hot = settings.live_copilot_hot_interval_sec || 0;

  // Live override → main fallback. The override card above is what
  // sets these when the user opts in to a separate tick provider.
  const provider = (settings.live_ai_provider || settings.ai_provider || "anthropic").trim();
  const model = (settings.live_claude_model || settings.claude_model || "").trim();
  const baseUrl = (settings.live_openai_base_url || settings.openai_base_url || "").trim();

  const est = estimateCopilotCost({
    wideIntervalSec: wide,
    hotIntervalSec: hot,
    provider, model, baseUrl,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Co-Pilot Cadence{" "}
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            optional
          </span>
        </CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          Two polling tiers. <strong>Wide</strong> tick runs the full
          ~10 minute context window every N seconds — the existing
          coaching behavior. <strong>Hot</strong> tick runs only the
          last ~90 seconds with a tighter prompt biased toward EMPTY
          — fires only when something time-sensitive happens. Cheaper
          per call, but the per-minute call rate adds up.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1.5">
            <Label className="text-xs">Wide tick interval (seconds)</Label>
            <Input
              type="number" min={15} max={300} step={5}
              value={wide}
              onChange={(e) => {
                const v = Math.max(15, Math.min(300, parseInt(e.target.value) || 45));
                update("live_copilot_wide_interval_sec", v);
              }}
            />
            <p className="text-[10px] text-muted-foreground">
              Range 15–300s. Lower = more responsive, higher = cheaper. Default 45.
            </p>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Hot tick interval (seconds, 0 = off)</Label>
            <Input
              type="number" min={0} max={60} step={5}
              value={hot}
              onChange={(e) => {
                const v = Math.max(0, Math.min(60, parseInt(e.target.value) || 0));
                update("live_copilot_hot_interval_sec", v);
              }}
            />
            <p className="text-[10px] text-muted-foreground">
              Range 0–60s. 0 disables the hot tier entirely. Try 15s if you want just-in-time prompts.
            </p>
          </div>
        </div>

        {/* Live cost estimate. Recalculates as the user touches any
            input or switches the live-provider override above. */}
        <div className="rounded-md border bg-muted/30 p-3 space-y-2 text-xs">
          <div className="flex items-baseline gap-2 flex-wrap">
            <span className="font-medium">Estimated cost:</span>
            <span className="text-muted-foreground">
              {est.callsPerMinute.toFixed(1)} call{est.callsPerMinute === 1 ? "" : "s"}
              /min ({est.widePerHour.toFixed(0)} wide
              {est.hotPerHour > 0 ? ` + ${est.hotPerHour.toFixed(0)} hot` : ""}
              /hour)
            </span>
          </div>
          {est.currentHourlyUsd !== null ? (
            <p>
              At your current provider ({provider}
              {model ? ` / ${model}` : ""}):{" "}
              <strong>{formatUsd(est.currentHourlyUsd)} per hour of recording</strong>
            </p>
          ) : (
            <p className="italic text-muted-foreground">
              No price on file for{" "}
              <code className="font-mono">{provider}{model ? `:${model}` : ""}</code>.
              Cost is unknown — compare against the rows below or check
              your provider&apos;s pricing page.
            </p>
          )}
          {est.currentNote && (
            <p className="text-amber-600 dark:text-amber-400 italic">
              {est.currentNote}
            </p>
          )}
          <div className="pt-2 border-t border-muted">
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
              For comparison
            </p>
            <ul className="space-y-0.5">
              {est.comparisons.map((c) => (
                <li key={c.label} className="flex items-baseline gap-2">
                  <span className="flex-1">{c.label}</span>
                  <span className="font-mono tabular-nums">
                    {formatUsd(c.hourlyUsd)}/hr
                  </span>
                </li>
              ))}
            </ul>
            <p className="text-[10px] text-muted-foreground italic mt-2">
              Estimates assume ~2k input + ~200 output tokens per wide
              tick, ~1.2k + ~100 per hot tick. Rough — within ~30%.
              Local/Ollama and OpenRouter free tier cost $0 directly.
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
