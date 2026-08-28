"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatBytes, type ArchiveStatus, type McpClientState, type McpStatus, type Settings, type TemplateEntry, type CoPilotPromptEntry } from "@/lib/api";
import { MCP_CLIENTS, mcpClient, mcpConfigSnippet } from "@/lib/mcp-config";
import { estimateCopilotCost, formatUsd } from "@/lib/copilot-cost";
import { confirmDialog } from "@/lib/confirm";
import { toast } from "sonner";
import { Loader2, Save, Trash2, Plus, RotateCcw, AlertTriangle, CheckCircle2, Copy, DownloadCloud, HelpCircle } from "lucide-react";
import { GpuAccelerationCard } from "./gpu-acceleration-card";
import { KnownSpeakersSection } from "./known-speakers-section";
import { SemanticIndexSection } from "./semantic-index-section";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { InfoTip } from "@/components/ui/info-tip";
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

// Google Gemini — free tier via the OpenAI-compatible compat endpoint
// (https://generativelanguage.googleapis.com/v1beta/openai/). Same model
// ids as Google's native API. The 2.5 line is the current generation;
// 2.0 / 1.5 entries kept for users who picked them before this update
// — they still work, just slower / lower quality.
const GEMINI_MODELS = [
  { value: "gemini-2.5-flash", label: "Gemini 2.5 Flash (recommended)" },
  { value: "gemini-2.5-flash-lite", label: "Gemini 2.5 Flash-Lite (faster, cheaper)" },
  { value: "gemini-2.5-pro", label: "Gemini 2.5 Pro (highest quality, slower)" },
  { value: "gemini-2.0-flash", label: "Gemini 2.0 Flash (legacy)" },
  { value: "gemini-2.0-flash-lite", label: "Gemini 2.0 Flash-Lite (legacy)" },
  { value: "gemini-1.5-flash", label: "Gemini 1.5 Flash (legacy)" },
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

export function SettingsView({ onSaved }: { onSaved?: () => void } = {}) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [storage, setStorage] = useState<{
    total_bytes: number;
    session_count: number;
    wav_count: number;
  } | null>(null);
  const [saving, setSaving] = useState(false);
  // Bumped after every successful Save Settings — the Session Archive
  // card watches this to refresh its status readout (folder counts) so
  // a folder change shows up without the user having to leave the tab
  // and come back.
  const [settingsSavedAt, setSettingsSavedAt] = useState(0);
  const [cleaning, setCleaning] = useState(false);
  const [ghostCount, setGhostCount] = useState<number | null>(null);
  const [purging, setPurging] = useState(false);
  // Which settings tab is showing. Grouped so the page stops being one
  // endless scroll — each tab is ~4-5 related cards.
  const [tab, setTab] = useState<string>("setup");

  // Switching tabs must start at the top. The tabs share ONE scroll
  // container (page.tsx's `overflow-y-auto` shell, which every view
  // renders into), so scrolling deep into Setup and then clicking
  // Data & Diagnostics left the new tab already scrolled halfway —
  // the same stale-scroll bug fixed for the main nav in v2.23.1, just
  // one level down. We walk up to whichever ancestor actually scrolls
  // rather than reaching for a ref in page.tsx, so this keeps working
  // if the shell's markup changes.
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    let el = rootRef.current?.parentElement;
    while (el) {
      const oy = getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight) {
        el.scrollTo({ top: 0 });
        return;
      }
      el = el.parentElement;
    }
  }, [tab]);

  useEffect(() => {
    (async () => {
      try {
        const [s, stats, ghosts] = await Promise.all([
          api.getSettings(),
          api.getRetentionStats().catch(() => null),
          api.listGhostSessions().catch(() => null),
        ]);
        setSettings(s);
        setStorage(stats);
        setGhostCount(ghosts?.count ?? null);
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
      // Let the parent re-pull settings so toggles that affect the shell
      // (e.g. the Today tab's visibility) reflect immediately without a
      // restart or focus event.
      onSaved?.();
      setSettingsSavedAt(Date.now());
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

  const purgeGhosts = async () => {
    // Confirm before deleting so a misclick doesn't nuke the row a
    // recovery flow might still produce a WAV for (e.g. OneDrive still
    // syncing it back from another machine). The 14-day auto-purge
    // covers the don't-care case; this button is for "I know there's
    // never coming a WAV for these."
    if (ghostCount == null || ghostCount === 0) return;
    if (!confirm(
      `Delete ${ghostCount} session row(s) that have no audio file on disk?\n\n` +
      "Their transcripts + summaries (if any) will be removed too. " +
      "Recordings whose audio is still syncing down from the cloud will be SKIPPED automatically."
    )) return;
    setPurging(true);
    try {
      // Pass min_age_days: 0 so the endpoint deletes everything the
      // scan returns. The audio-exists defence-in-depth check inside
      // the endpoint still protects against deleting a row whose WAV
      // synced down between the scan and the delete.
      const res = await api.deleteGhostSessions({ min_age_days: 0 });
      // The endpoint's type promises `deleted`/`errors` are always
      // arrays, but a malformed response shouldn't be read as "0
      // deleted, 0 errors" — that's indistinguishable from genuine
      // success and would hide a real problem.
      const deletedKnown = Array.isArray(res.deleted);
      const errorsKnown = Array.isArray(res.errors);
      if (!deletedKnown || !errorsKnown) {
        toast.warning(
          "Purge response was incomplete — check Sessions to confirm what was actually removed."
        );
      } else {
        const ok = res.deleted.length;
        const errs = res.errors.length;
        if (errs > 0) {
          toast.warning(
            `Deleted ${ok} ghost session(s); ${errs} skipped (see backend log).`
          );
        } else {
          toast.success(`Deleted ${ok} ghost session(s).`);
        }
      }
      const fresh = await api.listGhostSessions().catch(() => null);
      setGhostCount(fresh?.count ?? 0);
    } catch (e) {
      toast.error(`Could not purge ghost sessions: ${e instanceof Error ? e.message : e}`);
    } finally {
      setPurging(false);
    }
  };

  const SETTINGS_TABS = [
    { id: "setup", label: "Setup" },
    { id: "integrations", label: "Templates & Integrations" },
    { id: "recording", label: "Recording & Co-Pilot" },
    { id: "data", label: "Data & Diagnostics" },
  ];

  return (
    <div ref={rootRef} className="mx-auto max-w-3xl space-y-6">
      {/* Tab bar — sticky so it stays reachable while a tab's cards scroll.
          -mt-6/pt-6 is a matched pair that cancels out visually (net zero
          offset for the tab row) but lets the bar's opaque background
          extend up through the scroll container's `pt-6` in page.tsx —
          without it, position:sticky can't leave its containing block, so
          the bar would park 24px below the real top of the scrollport and
          content would keep scrolling visibly through that gap. Coupled to
          page.tsx's `pt-6` on the `overflow-y-auto` container: if that
          padding value ever changes, this offset must change with it. */}
      {/* Sticky offsets are NEGATIVE on purpose. `top: 0` sticks to the
          scroll container's CONTENT box, i.e. below its pt-6 — measured
          in a real browser: viewport top 64px, bar top 88px. -top-6
          cancels that padding so the bar pins flush to the viewport
          edge; -mt-6/pt-6 then extend its background over the same
          band so nothing can scroll visibly above it. Both values
          mirror page.tsx's pt-6 on the shared scroll container — change
          one and you must change the other. */}
      <div className="sticky -top-6 z-10 -mx-6 -mt-6 border-b border-border bg-background px-6 pt-2">
        <div className="mx-auto flex max-w-3xl flex-wrap gap-1">
          {SETTINGS_TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={
                "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors " +
                (tab === t.id
                  ? "border-primary text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground")
              }
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {tab === "setup" && (<>
      {/* API Keys */}
      <Card>
        <CardHeader>
          <CardTitle>API Keys</CardTitle>
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
          <CardTitle>Recordings Folder</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <div className="flex items-center gap-1">
              <Label>Where Meeting Recorder saves session audio, transcripts, and client list</Label>
              <InfoTip label="Why this must be a local folder">
                <strong>Use a local folder</strong> (e.g.{" "}
                <code className="text-[11px]">C:\Users\you\MeetingRecordings</code>).
                Recording writes large audio streams here in real time — a
                cloud-stream folder (Google Drive <code className="text-[11px]">G:\</code>,
                OneDrive Files On-Demand) stalls those writes and can freeze
                the backend mid-recording. To get sessions onto a network /
                cloud folder, set the <strong>Cloud Mirror</strong> below —
                it copies in the background where a slow drive can&apos;t
                hurt a recording. Existing sessions stay where they are;
                restart the app after changing.
              </InfoTip>
            </div>
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
            <p className="text-xs text-muted-foreground">
              Must be local disk — a cloud-sync folder can stall writes and
              freeze the backend mid-recording.
            </p>
          </div>

          <div className="space-y-2 border-t pt-4">
            <div className="flex items-center gap-1">
              <Label>Cloud Mirror — network folder for finished sessions (optional)</Label>
              <InfoTip label="What Cloud Mirror copies">
                After each processing step, the session&apos;s{" "}
                <strong>transcript, summary, action items, decisions,
                and requirements</strong> are copied here into a subfolder
                named for its <strong>client</strong> (sessions without a
                client go to <code className="text-[11px]">Unfiled</code>).
                The <strong>raw audio and session file stay on local disk</strong>
                {" "}— they&apos;re what stalled Google Drive in earlier builds
                and nobody reads a WAV from a shared drive anyway. Copies
                run in the background with retries; a slow or briefly-
                offline drive delays the text copy, never the recording. A
                client&apos;s explicit Designated Folder (Clients view)
                wins over this root and follows the same text-only rule.
              </InfoTip>
            </div>
            <div className="flex gap-2">
              <Input
                value={settings.cloud_mirror_dir || ""}
                onChange={(e) => update("cloud_mirror_dir", e.target.value)}
                placeholder="G:\My Drive\MRv2  (empty = off)"
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
                      defaultPath: settings.cloud_mirror_dir || undefined,
                      title: "Choose cloud mirror folder",
                    });
                    if (typeof picked === "string" && picked) {
                      update("cloud_mirror_dir", picked);
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
            <p className="text-xs text-muted-foreground">
              Copies finished session text (never audio) here in the
              background, organized by client.
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Session Archive (v2.20: cross-device library sync via a
          user-owned synced folder — see server.py's _session_archive_dir
          "three-location rule" docstring for how this differs from
          Cloud Mirror above). */}
      <Card>
        <CardHeader>
          <CardTitle>Session Archive</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-2">
            <div className="flex items-center gap-1">
              <Label>
                Roaming folder for session files (iCloud / OneDrive / Google
                Drive) — optional
              </Label>
              <InfoTip label="How Session Archive syncing works">
                Each machine records to its own local disk — a Mac and a PC
                never share a filesystem. Point this at a folder your sync
                client (iCloud Drive, OneDrive, Google Drive) already keeps
                in sync between your machines, and every processed
                meeting&apos;s <strong>transcript, summary, action items,
                decisions, and requirements</strong> is copied there in the
                background so the other machine sees the same library.{" "}
                <strong>Audio is never copied here</strong> — only the small
                session file. The folder must already exist; it won&apos;t
                be created for you, so a typo&apos;d path or an unmounted
                drive fails loudly on Save instead of quietly starting an
                empty archive. Click <strong>Save Settings</strong> below
                after entering a path.
              </InfoTip>
            </div>
            <div className="flex gap-2">
              <Input
                value={settings.session_archive_dir || ""}
                onChange={(e) => update("session_archive_dir", e.target.value)}
                placeholder="~/iCloud Drive/MRv2 Archive  (empty = off)"
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
                      defaultPath: settings.session_archive_dir || undefined,
                      title: "Choose Session Archive folder",
                    });
                    if (typeof picked === "string" && picked) {
                      update("session_archive_dir", picked);
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
            <p className="text-xs text-muted-foreground">
              Syncs session text between your machines via your own cloud
              folder. Audio is never copied here.
            </p>
          </div>
          <SessionArchiveStatusPanel savedAt={settingsSavedAt} />
        </CardContent>
      </Card>

      <AppUpdatesCard />

      {/* Models */}
      <Card>
        <CardHeader>
          <CardTitle>AI Models</CardTitle>
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

      </>)}

      {tab === "integrations" && (<>
      {/* Summary Templates */}
      <SummaryTemplatesCard />

      {/* AI assistant access — MCP + REST/OpenAPI. Lives here rather
          than under Data & Diagnostics because it is an integration,
          and because the Chrome extension card (its closest sibling in
          kind) is on this tab too. */}
      <AiAccessCard />

      {/* SA Tools Portal */}
      <Card>
        <CardHeader>
          <CardTitle>SA Tools Portal</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <Label>Portal base URL</Label>
          <Input
            value={settings.portal_url || ""}
            onChange={(e) => update("portal_url", e.target.value)}
            placeholder="https://…execute-api…amazonaws.com/prod"
          />
          <p className="text-xs text-muted-foreground">
            The engagement-register push target. A setting rather than a
            constant because the portal has dev and prod hosts; leave
            empty to turn the integration off. Projects are bound to
            portal opportunities from the Engagements tab — each
            project&apos;s edit token is stored in the OS keychain, not
            in any config file.
          </p>
        </CardContent>
      </Card>

      {/* Email */}
      <Card>
        <CardHeader>
          <CardTitle>Email (Outlook)</CardTitle>
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
          <CardTitle>Calendar</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="calendar-source">Calendar source</Label>
            <Select
              value={settings.calendar_source || "auto"}
              onValueChange={(v) => v && update("calendar_source", v)}
            >
              <SelectTrigger id="calendar-source" className="w-72">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Automatic</SelectItem>
                <SelectItem value="extension">Chrome extension only</SelectItem>
                <SelectItem value="outlook">Local calendar only</SelectItem>
                <SelectItem value="off">Off</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              {settings.calendar_source === "extension"
                // The second sentence is the rest of the same promise.
                // "Never contacts Outlook" used to be true of the
                // calendar only: Draft follow-up emails still launched
                // classic desktop Outlook (or Mail.app on macOS) and
                // filed drafts into a mailbox this user never opens.
                // It now hands back compose links instead, and the
                // setting says so rather than leaving it a surprise.
                ? "Never contacts Outlook — the Record tab shows only what the Chrome extension has scraped from Outlook Web. Use this if Outlook keeps asking you to sign in; switching here stops those sign-in prompts for good. Draft follow-up emails also stops using a desktop mail client: it gives you one Outlook Web compose link per recipient, which you open in your browser."
                : settings.calendar_source === "outlook"
                ? "Uses only the local calendar (Outlook COM on Windows, Calendar app on macOS). Chrome extension events are ignored even if the extension is connected."
                : settings.calendar_source === "off"
                ? "No calendar data at all. The Upcoming Meetings panel stays empty and auto-record has nothing to trigger from."
                : "Uses the local calendar, plus anything the Chrome extension finds. If Outlook keeps asking you to sign in, switch to “Chrome extension only” below."}
            </p>
          </div>
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

      <ChromeExtensionCard />

      </>)}

      {tab === "recording" && (<>
      {/* Workflow */}
      <Card>
        <CardHeader>
          <CardTitle>Workflow</CardTitle>
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
            label="Fast live transcript (speech-boundary chunking)"
            description="Shows live text within ~1-3 seconds of someone finishing a sentence instead of waiting for a fixed 15-second window to fill. Turn off only if you hit a transcription bug and want to fall back to the older, slower fixed-window behavior."
            checked={settings.live_vad_enabled}
            onChange={(v) => update("live_vad_enabled", v)}
          />
          <Toggle
            label="Label individual speakers in the live transcript"
            description="Tags each far-end voice as Speaker 1, Speaker 2, and so on while you record, instead of a single shared label for everyone on the call. It works from very short clips, so it can occasionally split one person across several labels or guess a saved name for the wrong voice. Turn it off if the live labels look wrong — recording, the live transcript, and the full speaker identification that runs after you stop are all unaffected."
            checked={settings.live_speaker_split_enabled}
            onChange={(v) => update("live_speaker_split_enabled", v)}
          />
          <div className="space-y-2">
            <Label htmlFor="diarization-device">Speaker identification device</Label>
            <Select
              value={settings.diarization_device || "auto"}
              onValueChange={(v) => v && update("diarization_device", v)}
            >
              <SelectTrigger id="diarization-device" className="w-64">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Auto (recommended)</SelectItem>
                <SelectItem value="cpu">CPU (avoids a known GPU conflict)</SelectItem>
                <SelectItem value="cuda">GPU / CUDA (force)</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Controls what runs speaker identification (who-said-what)
              after a recording stops. Auto uses your GPU when available
              and is fastest. CPU is slower but avoids a known crash on
              some machines where the GPU is used by transcription and
              speaker identification at the same time, right after you
              stop recording. If recordings have been silently vanishing
              a few seconds after you hit Stop, try CPU here.
            </p>
          </div>
          <Toggle
            label="Echo cancellation (experimental)"
            description="Cleans up the recording if you use a mic and speakers instead of a headset — normally, unmuting lets the other person's voice come back out of your speakers and get picked up a second time on your mic, duplicating their words in the transcript under your name. Runs after you stop recording, before the transcript is generated; if the cleanup doesn't look safe it's automatically skipped and your original audio is used untouched. Off by default while this is still being validated."
            checked={settings.echo_cancellation_enabled}
            onChange={(v) => update("echo_cancellation_enabled", v)}
          />
          <div className="space-y-1">
            <Label htmlFor="auto-screenshot-int">Auto-screenshot during recording</Label>
            <div className="flex items-center gap-2">
              <Input
                id="auto-screenshot-int"
                type="number"
                min={0}
                max={60}
                className="w-24"
                value={settings.auto_screenshot_interval_minutes}
                onChange={(e) =>
                  update(
                    "auto_screenshot_interval_minutes",
                    Math.max(0, Math.min(60, parseInt(e.target.value) || 0))
                  )
                }
              />
              <span className="text-sm text-muted-foreground">
                minutes between captures (0 = off — use the Screenshot
                button manually). Suggested: 3.
              </span>
            </div>
            <div className="text-xs text-muted-foreground">
              Captures a full-screen PNG and attaches it to the session
              so the summarizer gets visual context. Capture is
              opportunistic — if your screen is locked the capture is
              skipped silently.
            </div>
          </div>
          <Toggle
            label="Live Co-Pilot (beta)"
            description="Every ~45s during a recording, asks the configured LLM for three short bullet lists (clarifying questions, risks, suggested follow-ups) based on the last few minutes of conversation. Requires Live transcription to also be on. Costs an LLM call per tick — about $0.10–$0.20 per hour on Anthropic Haiku."
            checked={settings.live_copilot_enabled}
            onChange={(v) => update("live_copilot_enabled", v)}
          />
          <Toggle
            label="Today / Daily Briefing tab"
            description="Adds a 'Today' tab (and makes it the default landing view) that imports your Microsoft 365 Copilot scheduled-prompt briefing and renders it as an interactive dashboard — top priority, agenda, action items, FYI. Off by default: it assumes you run a daily Copilot scheduled prompt and paste its output in. Turn it on only if you have that setup."
            checked={settings.today_view_enabled}
            onChange={(v) => update("today_view_enabled", v)}
          />
          <Toggle
            label="Auto pre-meeting brief"
            description="A few minutes before each calendar meeting, automatically generate a prep brief from your prior sessions with that client and fire a notification when it's ready. Runs on a backend timer (works even if the app isn't focused). Costs one LLM call per meeting."
            checked={settings.auto_prep_brief_enabled}
            onChange={(v) => update("auto_prep_brief_enabled", v)}
          />
          {settings.auto_prep_brief_enabled && (
            <div className="flex items-center gap-2 pl-1 -mt-1">
              <Label htmlFor="prep-lead" className="text-sm text-muted-foreground">
                Generate
              </Label>
              <Input
                id="prep-lead"
                type="number"
                min={1}
                max={120}
                value={settings.auto_prep_brief_lead_min}
                onChange={(e) =>
                  update("auto_prep_brief_lead_min",
                    Math.max(1, Math.min(120, Number(e.target.value) || 10)))}
                className="h-8 w-20"
              />
              <span className="text-sm text-muted-foreground">
                minutes before the meeting starts
              </span>
            </div>
          )}
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
            <CardTitle>
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
          <CardTitle>Auto-stop</CardTitle>
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

      </>)}

      {tab === "data" && (<>
      {/* Diagnostics — health checks + log tail */}
      <DiagnosticsCard />

      {/* One-click support bundle — replaces the hand-written .bat
          scripts field debugging used to need. */}
      <DiagnosticsExportCard />

      {/* Domain terminology — biases transcription + fixes mis-hears */}
      <TerminologyCard />

      {/* Known speakers (cross-session voice fingerprints) */}
      <KnownSpeakersSection />

      {/* Semantic search index (cross-session vector retrieval) */}
      <SemanticIndexSection />

      {/* Retention */}
      <Card>
        <CardHeader>
          <CardTitle>Retention</CardTitle>
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

          {/* Ghost sessions: session_*.json with no matching WAV on disk.
              Accumulate after a backend crash mid-recording / mid-finalize
              (v2.11.1's JSON-first writes leave the stub behind). Auto-
              purged at 14 days; this button surfaces the manual cleanup. */}
          {ghostCount != null && ghostCount > 0 && (
            <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-950/40">
              <div className="font-medium">
                {ghostCount} session{ghostCount === 1 ? "" : "s"} with no audio file
              </div>
              <div className="mt-1 text-muted-foreground">
                These show up in the Sessions list but have no WAV on disk —
                they'll fail to process. Usually left over from a backend
                crash mid-recording. Stubs older than 14 days are
                auto-purged at startup.
              </div>
              <Button
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={purgeGhosts}
                disabled={purging}
              >
                {purging ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Trash2 className="h-4 w-4 mr-2" />
                )}
                Delete {ghostCount} ghost session{ghostCount === 1 ? "" : "s"}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      </>)}

      {/* Save bar — sticky, mirrors the tab bar's fix above. -mb-16/pb-16
          cancels out visually (the button row keeps its own py-3 in the
          inner div) but lets the bar's opaque background extend down
          through the scroll container's `pb-16` in page.tsx, so it's
          flush with the real bottom of the scrollport instead of parking
          64px above it with content scrolling visibly underneath.
          Coupled to page.tsx's `pb-16` on the `overflow-y-auto` container:
          if that padding value ever changes, this offset must change
          with it. */}
      {/* Mirror of the tab bar above: -bottom-16 cancels page.tsx's
          pb-16 so the bar pins flush to the bottom of the scroll
          viewport (measured: viewport bottom 880px, bar bottom 816px
          before this), and -mb-16/pb-16 extend the background across
          that band. */}
      <div className="sticky -bottom-16 z-10 -mx-6 -mb-16 border-t border-border bg-background px-6 pb-2">
        <div className="mx-auto max-w-3xl flex justify-end gap-2 py-2">
          <Button onClick={save} disabled={saving}>
            {saving ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Save className="h-4 w-4 mr-2" />}
            Save Settings
          </Button>
        </div>
      </div>
    </div>
  );
}

// Chrome extension setup card.
//
// The Meeting Recorder Chrome extension (chrome-extension/ at repo
// root, bundled into chrome-extension.zip in the release archive)
// is the v2.16+ replacement for the Playwright-based OWA scrape that
// fought Microsoft's enterprise-tenant automation detection through
// the entire v2.15.x dot-release saga without ever winning. The
// extension runs in the user's REAL Chrome — Microsoft's detection
// doesn't fire — so the user's real session cookies authenticate
// the scrape.
//
// Setup requires the user to paste two values into the extension's
// settings: the backend URL and the per-launch auth token. The
// frontend already knows both via api.ts's getBaseUrl + auth helpers,
// so this card just surfaces them with Copy buttons and links to
// installation instructions.
/**
 * AI assistant access — the front door for the MCP server and the
 * REST/OpenAPI surface.
 *
 * Both existed and worked long before this card did; neither was
 * mentioned anywhere in the app, the root README, or docs/. The owner
 * of the project did not know the MCP server was there. A capability
 * nobody can find is worth roughly nothing, so this card exists to be
 * the place you look.
 *
 * Deliberately vendor-neutral: MCP is an open protocol and the same
 * server serves Cursor, VS Code, Zed and others, so the client picker
 * lists them rather than implying this is a Claude-only feature.
 */
function AiAccessCard() {
  // Client list and snippet construction live in @/lib/mcp-config so
  // the string the user pastes can be tested without rendering this
  // screen. v2.72.0 built it inline and shipped a POSIX line
  // continuation that broke every Windows paste; see its tests.
  const [client, setClient] = useState("claude-code");
  const [backendUrl, setBackendUrl] = useState("http://127.0.0.1:17645");
  const [token, setToken] = useState("");
  const [tokenVisible, setTokenVisible] = useState(false);
  const [copyMsg, setCopyMsg] = useState("");

  // null = still loading OR the backend couldn't be reached; `mcpLoading`
  // separates those, because "we don't know yet" and "we asked and the
  // answer is no" must not render the same.
  const [mcp, setMcp] = useState<McpStatus | null>(null);
  const [mcpLoading, setMcpLoading] = useState(true);
  const [installing, setInstalling] = useState(false);
  const [installErr, setInstallErr] = useState("");

  // Per-client setup state, keyed by client id.
  const [clients, setClients] = useState<McpClientState[]>([]);
  const [clientsLoading, setClientsLoading] = useState(false);
  const [settingUp, setSettingUp] = useState(false);
  const [setupMsg, setSetupMsg] = useState("");
  const [setupOk, setSetupOk] = useState(true);

  const loadClients = useCallback(async () => {
    setClientsLoading(true);
    try {
      const res = await api.getMcpClients();
      setClients(res.clients || []);
    } catch {
      setClients([]);
    } finally {
      setClientsLoading(false);
    }
  }, []);

  const loadMcp = useCallback(async () => {
    setMcpLoading(true);
    try {
      setMcp(await api.getMcpStatus());
    } catch {
      setMcp(null);
    } finally {
      setMcpLoading(false);
    }
  }, []);

  useEffect(() => {
    loadMcp();
  }, [loadMcp]);

  useEffect(() => {
    if (mcp?.ready) loadClients();
  }, [mcp?.ready, loadClients]);

  useEffect(() => {
    (async () => {
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        try {
          const port = await invoke<number>("get_backend_port");
          setBackendUrl(`http://127.0.0.1:${port}`);
        } catch { /* keep the default port */ }
        try {
          setToken((await invoke<string>("get_backend_token")) || "");
        } catch { setToken(""); }
      } catch {
        setToken("(no token — dev mode)");
      }
    })();
  }, []);

  const copy = async (label: string, val: string) => {
    try {
      await navigator.clipboard.writeText(val);
      setCopyMsg(`${label} copied.`);
      setTimeout(() => setCopyMsg(""), 2000);
    } catch {
      setCopyMsg(`Copy failed — select the ${label} text and copy manually.`);
    }
  };

  const turnOn = async () => {
    setInstalling(true);
    setInstallErr("");
    try {
      const res = await api.installMcpSdk();
      setMcp(res.status);
      if (res.ok) {
        toast.success("AI assistant access is on. Copy the config below into your AI tool.");
      } else {
        // pip's own last lines say more than any message this component
        // could invent — offline, blocked index, version conflict.
        setInstallErr(res.output || "pip failed with no output.");
      }
    } catch (e) {
      setInstallErr(e instanceof Error ? e.message : String(e));
    } finally {
      setInstalling(false);
    }
  };

  const clientState = (id: string) =>
    clients.find((c) => c.client === id)?.state ?? "absent";

  const setUpClient = async (id: string) => {
    setSettingUp(true);
    setSetupMsg("");
    try {
      const res = await api.setUpMcpClient(id);
      setSetupOk(res.ok);
      if (res.ok) {
        setSetupMsg(
          `Written to ${res.path}. Quit ${mcpClient(id).label} completely and reopen it.`);
        toast.success(`${mcpClient(id).label} is set up.`);
        await loadClients();
      } else {
        // The backend's message names the actual problem — an
        // unparseable config it deliberately did NOT overwrite, or a
        // permission error.
        setSetupMsg(res.error || "Couldn't write the config.");
      }
    } catch (e) {
      setSetupOk(false);
      setSetupMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setSettingUp(false);
    }
  };

  // The two paths a client needs. Resolved by the backend from the
  // interpreter it is itself running on, so they are correct for THIS
  // machine — before v2.72 this card printed a placeholder that could
  // not exist on a machine that installed the app rather than cloning
  // the repo, and there was no way for the user to work out the real one.
  const PY = mcp?.python ?? "";
  const LAUNCHER = mcp?.launcher ?? "";
  const active = mcpClient(client);
  const activeClient = clients.find((c) => c.client === client);
  const snippet = mcpConfigSnippet(client, PY, LAUNCHER);

  return (
    <Card>
      <CardHeader>
        <CardTitle>AI assistant access</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Let an AI assistant search your meetings, answer questions from
          your Knowledge Folders, and list what you still owe — read-only,
          on this machine, while the app is running. MCP is an open
          protocol, so this is not limited to any one vendor.
        </p>

        {mcpLoading ? (
          <p className="text-sm text-muted-foreground">
            <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />
            Checking…
          </p>
        ) : !mcp ? (
          <p className="text-sm text-muted-foreground">
            Couldn&apos;t reach the backend to check. Reopen Settings once the
            app has finished starting.
          </p>
        ) : !mcp.bundled ? (
          // A dev checkout that was never run through zip-bundle.py, or a
          // partial extraction. Nothing to click — say what the state is
          // rather than offering a button that cannot help.
          <div className="space-y-2 rounded-md border border-dashed p-3">
            <p className="text-sm">
              This build doesn&apos;t carry the MCP server files.
            </p>
            <p className="text-xs text-muted-foreground">
              Expected in a checkout run straight from source. Set it up by
              hand from <code>mcp-server/</code> — see{" "}
              <code>docs/ai-integrations.md</code>. Installed builds ship the
              files and turn this on with one click.
            </p>
          </div>
        ) : !mcp.installed ? (
          <div className="space-y-2 rounded-md border p-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium">Not turned on yet</p>
                <p className="text-xs text-muted-foreground">
                  One click installs the MCP protocol library into the
                  app&apos;s own Python. Nothing else to download, no separate
                  virtualenv, and the app keeps working either way.
                </p>
              </div>
              <Button type="button" size="sm" onClick={turnOn} disabled={installing}>
                {installing ? (
                  <><Loader2 className="mr-1 h-3 w-3 animate-spin" /> Turning on…</>
                ) : (
                  "Turn on"
                )}
              </Button>
            </div>
            {installing && (
              <p className="text-xs text-muted-foreground">
                Downloading — usually under a minute on a normal connection.
              </p>
            )}
            {installErr && (
              <div className="space-y-1">
                <p className="text-xs text-destructive">
                  <AlertTriangle className="mr-1 inline h-3 w-3" />
                  Couldn&apos;t turn it on. The installer said:
                </p>
                <Textarea readOnly value={installErr} rows={6}
                          className="font-mono text-xs" />
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-1">
            <p className="text-sm">
              <CheckCircle2 className="mr-1 inline h-4 w-4 text-green-600" />
              Ready. Set up the tools you use below.
            </p>
            {/* The one fact that settles "did it work?". Without it the
                only feedback loop was restart-and-hope, which is
                exactly how an evening gets lost. */}
            <p className="text-xs text-muted-foreground">
              {mcp.last_client_seen_at
                ? `An AI assistant last used this at ${new Date(mcp.last_client_seen_at).toLocaleTimeString()}.`
                : "No AI assistant has used this since the app started — that is expected until you set one up and restart it."}
            </p>
          </div>
        )}

        {mcp?.ready && (<>
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>Your AI tool</Label>
            {clientsLoading && (
              <span className="text-xs text-muted-foreground">
                <Loader2 className="mr-1 inline h-3 w-3 animate-spin" />
                checking
              </span>
            )}
          </div>
          {/* Each tool carries ITS OWN state. v2.72 showed one command
              and one JSON block side by side and never said that
              setting up one does not set up another — a user ran the
              Claude Code command, restarted everything, and found
              Claude Desktop still blind. */}
          <div className="flex flex-wrap gap-2">
            {MCP_CLIENTS.map((c) => {
              const st = clientState(c.id);
              return (
                <Button
                  key={c.id}
                  type="button"
                  size="sm"
                  variant={c.id === client ? "default" : "outline"}
                  onClick={() => setClient(c.id)}
                >
                  {st === "current" && (
                    <CheckCircle2 className="mr-1 h-3 w-3 text-green-600" />
                  )}
                  {st === "stale" && (
                    <AlertTriangle className="mr-1 h-3 w-3 text-amber-600" />
                  )}
                  {c.label}
                </Button>
              );
            })}
          </div>
          <p className="text-xs text-muted-foreground">
            Every tool is configured separately — setting up one does not
            set up the others.
          </p>
        </div>

        {activeClient?.writable ? (
          <div className="space-y-2 rounded-md border p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-medium">
                  {clientState(client) === "current"
                    ? `${active.label} is set up`
                    : clientState(client) === "stale"
                      ? `${active.label} is set up, but pointing somewhere else`
                      : clientState(client) === "unreadable"
                        ? `${active.label}'s config can't be read`
                        : `${active.label} isn't set up yet`}
                </p>
                <p className="break-all text-xs text-muted-foreground">
                  {activeClient.path}
                </p>
              </div>
              <Button type="button" size="sm" disabled={settingUp}
                      variant={clientState(client) === "current" ? "outline" : "default"}
                      onClick={() => setUpClient(client)}>
                {settingUp ? (
                  <><Loader2 className="mr-1 h-3 w-3 animate-spin" /> Writing…</>
                ) : clientState(client) === "current" ? (
                  "Rewrite"
                ) : clientState(client) === "stale" ? (
                  "Update paths"
                ) : (
                  `Set up ${active.label}`
                )}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {clientState(client) === "stale"
                ? "It points at paths that have moved, so its tools will fail. Updating rewrites them."
                : "Writes the config for you — your other servers and settings are kept, and the file is backed up first."}
            </p>
            {/* The restart is where this actually goes wrong. A user
                spent an evening here: the config was correct, but the
                client had been running since before it was written, so
                it was holding a stale copy. Closing the window is not
                enough — Claude Desktop runs a dozen processes and keeps
                one in the tray. */}
            {clientState(client) !== "absent" && (
              <p className="text-xs text-muted-foreground">
                <strong>Then restart {active.label} completely.</strong>{" "}
                Closing the window is not enough — quit it from the
                system tray (right-click the icon → Quit), or end it in
                Task Manager. A client that was already running is still
                holding the old config.
              </p>
            )}
            {setupMsg && (
              <p className={`text-xs ${setupOk ? "text-muted-foreground" : "text-destructive"}`}>
                {!setupOk && <AlertTriangle className="mr-1 inline h-3 w-3" />}
                {setupMsg}
              </p>
            )}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            {active.label} has to be set up by hand — it keeps its config
            somewhere the app shouldn&apos;t write to. Use the snippet below.
          </p>
        )}

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label>
              {activeClient?.writable
                ? "Or do it by hand"
                : active.kind === "cli"
                  ? "Run this once"
                  : "Add this to the client's MCP config"}
            </Label>
            <Button type="button" size="sm" variant="outline"
                    onClick={() => copy("Config", snippet)}>
              <Copy className="mr-1 h-3 w-3" /> Copy
            </Button>
          </div>
          <Textarea readOnly value={snippet} rows={active.kind === "cli" ? 3 : 8}
                    className="font-mono text-xs" />
          <p className="text-xs text-muted-foreground">
            {active.where}
          </p>
          <p className="text-xs text-muted-foreground">
            The paths are already filled in for this machine — paste it
            as-is. Your assistant finds the app&apos;s address and access
            token by itself, so there is nothing secret to copy here.
            Full details, including every client&apos;s config location:{" "}
            <code>docs/ai-integrations.md</code>.
          </p>
        </div>
        </>)}

        <div className="space-y-2 border-t pt-4">
          <Label>For tools that don&apos;t speak MCP</Label>
          <p className="text-xs text-muted-foreground">
            The backend is a REST API with an OpenAPI spec — usable by any
            assistant that can call HTTP.
          </p>
          <div className="flex items-center gap-2">
            <Input readOnly value={`${backendUrl}/openapi.json`}
                   className="font-mono text-xs" />
            <Button type="button" size="sm" variant="outline"
                    onClick={() => copy("Spec URL", `${backendUrl}/openapi.json`)}>
              <Copy className="h-3 w-3" />
            </Button>
          </div>
          <div className="flex items-center gap-2">
            <Input readOnly type={tokenVisible ? "text" : "password"}
                   value={token} className="font-mono text-xs" />
            <Button type="button" size="sm" variant="outline"
                    onClick={() => setTokenVisible((v) => !v)}>
              {tokenVisible ? "Hide" : "Show"}
            </Button>
            <Button type="button" size="sm" variant="outline"
                    onClick={() => copy("Token", token)}>
              <Copy className="h-3 w-3" />
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            <AlertTriangle className="mr-1 inline h-3 w-3" />
            This token reads every transcript and document in your archive.
            Treat it like a password — don&apos;t paste it into a shared doc
            or a cloud tool other people can read. The API is localhost-only,
            so a cloud-hosted assistant cannot reach it without a tunnel.
          </p>
        </div>

        {copyMsg && <p className="text-xs text-muted-foreground">{copyMsg}</p>}
      </CardContent>
    </Card>
  );
}

function ChromeExtensionCard() {
  const [backendUrl, setBackendUrl] = useState<string>("");
  const [token, setToken] = useState<string>("");
  const [tokenVisible, setTokenVisible] = useState(false);
  const [copyMsg, setCopyMsg] = useState<string>("");

  // Bundled-vs-last-seen version tracking (v2.28.1: the app had no way
  // to detect a stale, still-installed extension — see AGENTS.md's
  // build item #1/#2). `extInfo` is null while loading OR when the
  // backend couldn't be reached; distinguish those with `extLoading`.
  const [extInfo, setExtInfo] = useState<Awaited<ReturnType<typeof api.getExtensionInfo>> | null>(null);
  const [extLoading, setExtLoading] = useState(true);
  const [installing, setInstalling] = useState(false);
  const [installMsg, setInstallMsg] = useState<string>("");

  const loadExtInfo = useCallback(async () => {
    setExtLoading(true);
    try {
      setExtInfo(await api.getExtensionInfo());
    } catch {
      setExtInfo(null);
    } finally {
      setExtLoading(false);
    }
  }, []);

  useEffect(() => {
    loadExtInfo();
  }, [loadExtInfo]);

  const installFiles = async () => {
    setInstalling(true);
    setInstallMsg("");
    try {
      const result = await api.installExtensionFiles();
      setInstallMsg(
        `Wrote ${result.file_count} file${result.file_count === 1 ? "" : "s"} to ${result.path}.`);
      await loadExtInfo();
    } catch (e) {
      setInstallMsg(e instanceof Error ? `Install failed: ${e.message}` : "Install failed.");
    } finally {
      setInstalling(false);
    }
  };

  useEffect(() => {
    (async () => {
      try {
        // getBaseUrl / getAuthToken aren't exported by api.ts at top
        // level; pull them via the same plumbing that the request()
        // helper does. Direct fetch against the local Tauri command
        // surface returns the port + token.
        const { invoke } = await import("@tauri-apps/api/core");
        try {
          const port = await invoke<number>("get_backend_port");
          setBackendUrl(`http://127.0.0.1:${port}`);
        } catch {
          setBackendUrl("http://127.0.0.1:17645");
        }
        try {
          const t = await invoke<string>("get_backend_token");
          setToken(t || "");
        } catch {
          setToken("");
        }
      } catch {
        // Running outside Tauri (dev mode). Show placeholders.
        setBackendUrl("http://127.0.0.1:17645");
        setToken("(no token — dev mode)");
      }
    })();
  }, []);

  const copy = async (label: string, val: string) => {
    try {
      await navigator.clipboard.writeText(val);
      setCopyMsg(`${label} copied.`);
      setTimeout(() => setCopyMsg(""), 2000);
    } catch {
      setCopyMsg(`Copy failed — select and Ctrl+C the ${label} field.`);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Chrome Extension</CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          The Meeting Recorder Chrome extension pulls today&apos;s
          Outlook calendar + Teams Activity from your real Chrome and
          POSTs it here. Use this when the v2.15.x in-app sync was
          bouncing to login.microsoftonline.com on every attempt — the
          extension runs in YOUR browser, so Microsoft&apos;s automation
          detection doesn&apos;t fire.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-md border p-3 space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">Bundled version</span>
            <span className="font-mono">
              {extLoading ? "…" : extInfo?.bundled_version ?? "unavailable"}
            </span>
          </div>
          <div className="flex items-center justify-between gap-3 text-sm">
            <span className="text-muted-foreground shrink-0">Last seen</span>
            <span className="font-mono text-right">
              {extLoading
                ? "…"
                : !extInfo?.last_seen_at
                  ? "never posted"
                  : extInfo.last_seen_version
                    ? `${extInfo.last_seen_version} · ${new Date(extInfo.last_seen_at).toLocaleString()}`
                    : `unknown / pre-1.2.0 · ${new Date(extInfo.last_seen_at).toLocaleString()}`}
            </span>
          </div>

          {!extLoading && extInfo?.status === "update_available" && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 text-xs text-amber-700 dark:text-amber-400">
              <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <span>
                The extension that last posted (<strong>{extInfo.last_seen_version}</strong>) is
                older than the version this app ships (<strong>{extInfo.bundled_version}</strong>).
                Click <strong>Install / Update extension files</strong> below, then see{" "}
                <strong>Updating after a new release</strong> below — clicking{" "}
                <strong>Reload</strong> in <code className="text-[11px]">chrome://extensions</code> only
                picks up the new files if that card was already loaded from the stable folder;
                otherwise you need to remove it and Load unpacked again.
              </span>
            </div>
          )}
          {!extLoading && extInfo?.status === "unknown_version" && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 text-xs text-amber-700 dark:text-amber-400">
              <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <span>
                The extension that last posted didn&apos;t report a version at all (extensions
                before 1.2.0 didn&apos;t send one). Install/update below to make sure you&apos;re
                on the current build.
              </span>
            </div>
          )}
          {!extLoading && extInfo?.status === "never_posted" && (
            <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-2 text-xs text-muted-foreground">
              <HelpCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <span>The extension has never posted anything to this app yet. Follow the first-time setup below.</span>
            </div>
          )}
          {!extLoading && extInfo?.status === "up_to_date" && (
            <div className="flex items-center gap-2 text-xs text-emerald-700 dark:text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
              <span>Up to date.</span>
            </div>
          )}
          {!extLoading && extInfo?.status === "unknown" && (
            <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-2 text-xs text-muted-foreground">
              <HelpCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <span>This build doesn&apos;t carry a bundled extension (dev build) — install status can&apos;t be judged.</span>
            </div>
          )}

          <Button type="button" size="sm" onClick={installFiles} disabled={installing}>
            {installing
              ? <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              : <DownloadCloud className="h-4 w-4 mr-2" />}
            Install / Update extension files
          </Button>
          {installMsg && <p className="text-xs text-muted-foreground">{installMsg}</p>}

          {extInfo?.install_path && (
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">Stable install folder</Label>
              <div className="flex gap-2">
                <Input value={extInfo.install_path} readOnly className="font-mono text-xs" />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => copy("Install path", extInfo.install_path)}
                >
                  <Copy className="h-3.5 w-3.5" />
                </Button>
              </div>
              <p className="text-[11px] text-muted-foreground">
                This path never changes between releases — load it unpacked in Chrome ONCE, and
                every future click on <strong>Install / Update</strong> rewrites the same folder
                in place.
              </p>
            </div>
          )}
        </div>

        <div className="space-y-2">
          <Label>Backend URL</Label>
          <div className="flex gap-2">
            <Input value={backendUrl} readOnly className="font-mono text-sm" />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => copy("Backend URL", backendUrl)}
            >
              Copy
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            v2.16+: port stays at <code className="text-[11px]">17645</code> across
            recorder restarts (unless another app is holding it, then we
            fall back to a random free port). You only need to re-paste
            into the extension on the rare fallback case.
          </p>
        </div>

        <div className="space-y-2">
          <Label>Auth token</Label>
          <div className="flex gap-2">
            <Input
              type={tokenVisible ? "text" : "password"}
              value={token}
              readOnly
              className="font-mono text-sm"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setTokenVisible(!tokenVisible)}
            >
              {tokenVisible ? "Hide" : "Show"}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => copy("Token", token)}
            >
              Copy
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            v2.16+: persisted across launches at <code className="text-[11px]">%LOCALAPPDATA%\MeetingRecorder\extension-token</code>.
            Delete that file to rotate. Paste once into the extension&apos;s
            Settings → Auth token and it survives recorder restarts.
          </p>
        </div>

        {copyMsg && (
          <p className="text-xs text-emerald-700">{copyMsg}</p>
        )}

        <div className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground space-y-1">
          <p className="font-medium text-foreground">First-time setup:</p>
          <p>1. Click <strong>Install / Update extension files</strong> above.</p>
          <p>2. In Chrome, open <code>chrome://extensions</code>, enable <strong>Developer mode</strong> (top-right toggle).</p>
          <p>3. Click <strong>Load unpacked</strong>, select the stable install folder shown above.</p>
          <p>4. Pin the Meeting Recorder extension icon to your toolbar.</p>
          <p>5. Click the icon → <strong>Settings</strong>, paste the URL + token from above, click <strong>Save</strong>.</p>
          <p>6. Anytime you want a fresh brief: click the extension icon → <strong>Capture &amp; Send</strong>. Briefing appears in the Today tab.</p>
          <p className="font-medium text-foreground pt-2">Updating after a new release:</p>
          <p>
            Click <strong>Install / Update extension files</strong> above first — it always
            refreshes the stable install folder shown above with the latest files, regardless
            of which case below applies to you.
          </p>
          <p>
            <strong>If the Meeting Recorder card in <code>chrome://extensions</code> was already
            loaded from that stable folder</strong> (you followed first-time setup above and
            never re-picked a different folder): click <strong>Reload</strong> on it — same
            folder, no re-picking a directory.
          </p>
          <p>
            <strong>If this is your first update, or you&apos;re not sure where Chrome loaded it
            from</strong> (for example you originally unzipped the extension somewhere else):
            remove the existing Meeting Recorder card in <code>chrome://extensions</code>, then
            click <strong>Load unpacked</strong> again and pick the stable install folder shown
            above. We can&apos;t see which folder Chrome has loaded, so we can&apos;t pick this
            for you — Reload alone silently keeps you on the old version if the card points
            somewhere else.
          </p>
          <p>
            Either way, confirm the version number shown on the card in{" "}
            <code>chrome://extensions</code> afterward matches the bundled version above.
          </p>
        </div>
      </CardContent>
    </Card>
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

  // "Test connection" probe. Fires a 1-token chat completion against
  // whatever's currently configured (whether it's saved or just edited
  // in-memory — but the BACKEND test only sees saved values, so the
  // toast nudges the user to Save first if dirty). Result lives in
  // local component state, not the settings draft, so it doesn't
  // disappear on re-render and doesn't dirty the form.
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<
    | { ok: true; latency_ms: number; reply: string; provider: string; model: string }
    | { ok: false; error: string; latency_ms: number; provider: string; model: string }
    | null
  >(null);
  const runConnectionTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await api.testLLMConnection();
      if (res.ok) {
        setTestResult({
          ok: true,
          latency_ms: res.latency_ms,
          reply: res.reply || "",
          provider: res.provider,
          model: res.model,
        });
      } else {
        setTestResult({
          ok: false,
          latency_ms: res.latency_ms,
          error: res.error || "Unknown error",
          provider: res.provider,
          model: res.model,
        });
      }
    } catch (e) {
      // Network-level failure (backend down, auth, etc.) — distinct
      // from a provider-level failure that the endpoint itself
      // returns. Surface the message verbatim.
      setTestResult({
        ok: false,
        latency_ms: 0,
        error: e instanceof Error ? e.message : String(e),
        provider: settings.ai_provider,
        model: settings.claude_model,
      });
    } finally {
      setTesting(false);
    }
  };

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

  // Live-fetch the model list for any provider that exposes one
  // (Anthropic, Gemini, Groq, Ollama, generic OpenAI-compat). New
  // model releases (Gemini 2.5 Flash, Claude Haiku 4.5, etc.) appear
  // in the dropdown automatically — no app update needed. Per-preset
  // hardcoded lists (GEMINI_MODELS, GROQ_MODELS, …) survive as
  // fallbacks for offline / bad-key cases.
  //
  // Re-runs when the user switches preset OR edits the base URL (so
  // pointing Ollama at a different host re-discovers locally
  // installed models). Saving the API key doesn't trigger a refetch
  // because the user might be mid-edit; refetch happens on next
  // settings open / preset change.
  const [liveProviderModels, setLiveProviderModels] = useState<
    { value: string; label: string }[] | null
  >(null);
  useEffect(() => {
    if (preset === "openrouter") {
      // OpenRouter has its own live fetch above; don't double-fire.
      setLiveProviderModels(null);
      return;
    }
    if (preset === "custom") {
      // Custom URLs may be anything; only fetch when the user has
      // actually pasted a URL.
      if (!settings.openai_base_url) {
        setLiveProviderModels(null);
        return;
      }
    }
    let cancelled = false;
    api
      .getAvailableModels(
        settings.ai_provider || "anthropic",
        settings.openai_base_url || undefined,
      )
      .then((r) => {
        if (cancelled) return;
        if (r.models && r.models.length) {
          setLiveProviderModels(r.models);
        } else {
          // Empty list means the live fetch failed (no key, network
          // down, etc.). Keep null so the hardcoded fallback wins.
          setLiveProviderModels(null);
        }
      })
      .catch(() => {
        if (!cancelled) setLiveProviderModels(null);
      });
    return () => {
      cancelled = true;
    };
  }, [preset, settings.ai_provider, settings.openai_base_url]);

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

  // Which preset list (if any) this provider uses. The hardcoded
  // ANTHROPIC_MODELS / OLLAMA_MODELS / etc. are FALLBACKS — the live
  // fetch above wins when it returns a non-empty list, so new model
  // releases appear without an app update. OpenRouter keeps its own
  // dedicated path (already merges live + passthrough above). Custom
  // gets no list — the user types a model id directly.
  const liveOrFallback = (
    fallback: { value: string; label: string }[]
  ) => (liveProviderModels && liveProviderModels.length
    ? liveProviderModels
    : fallback);
  const presetModels = preset === "anthropic" ? liveOrFallback(ANTHROPIC_MODELS)
    : preset === "openrouter" ? openrouterModels
    : preset === "ollama" ? liveOrFallback(OLLAMA_MODELS)
    : preset === "groq" ? liveOrFallback(GROQ_MODELS)
    : preset === "gemini" ? liveOrFallback(GEMINI_MODELS)
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
            <SelectItem value="gemini">Google Gemini — free tier (Gemini 2.5 Flash, 2.5 Pro, …)</SelectItem>
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

      {/* Test connection — sanity check the configured provider BEFORE
          the next summarize/extract fails with an opaque error toast.
          Only tests SAVED settings — the backend reads its loaded
          config, so a dirty draft would test stale values. */}
      <div className="mt-2 space-y-2">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={runConnectionTest}
            disabled={testing}
          >
            {testing ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : null}
            Test connection
          </Button>
          <span className="text-xs text-muted-foreground">
            Fires a 1-token chat completion against the saved provider.
            Save settings first if you just edited them.
          </span>
        </div>
        {testResult && testResult.ok && (
          <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
            <div className="font-medium">
              ✓ Connected · {testResult.latency_ms} ms
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              {testResult.provider} / {testResult.model}
              {testResult.reply ? ` — replied "${testResult.reply}"` : ""}
            </div>
          </div>
        )}
        {testResult && !testResult.ok && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40">
            <div className="font-medium">
              ✗ Could not reach {testResult.provider} / {testResult.model}
            </div>
            <div className="mt-1 break-words text-xs">
              {testResult.error}
            </div>
          </div>
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
// Override panel for the Live Co-Pilot's tick model. Mirrors the main
// AIProviderSection: same preset switcher (Anthropic / Groq / Gemini /
// OpenRouter / Ollama / Custom), same live model-discovery dropdown,
// same Test connection button — just wired to the `live_*` settings
// keys and ``scope=live`` on the backend probes so it tests its own
// config in isolation from the main summarizer.
//
// Empty `live_ai_provider` = "use the main provider" (Phase A behavior).
function liveProviderPresetFromSettings(s: Settings): ProviderPreset {
  if (s.live_ai_provider === "anthropic") return "anthropic";
  if (s.live_ai_provider !== "openai") return "anthropic";
  const base = (s.live_openai_base_url || "").toLowerCase();
  if (base.includes("openrouter")) return "openrouter";
  if (base.includes("groq.com")) return "groq";
  if (base.includes("generativelanguage.googleapis")) return "gemini";
  if (base.includes("localhost") || base.includes("127.0.0.1")) return "ollama";
  return "custom";
}

function LiveCoPilotModelCard({
  settings, update,
}: {
  settings: Settings;
  update: <K extends keyof Settings>(key: K, value: Settings[K]) => void;
}) {
  const useOverride = (settings.live_ai_provider || "").trim() !== "";
  const livePreset = liveProviderPresetFromSettings(settings);

  // Live model-discovery — same shape as AIProviderSection's, but
  // scope="live" so the backend reads live_anthropic_api_key /
  // live_openai_api_key / live_openai_base_url for the fetch. Returns
  // null on empty/error so the hardcoded fallback list wins (same UX
  // contract as the main section).
  const [liveModels, setLiveModels] = useState<
    { value: string; label: string }[] | null
  >(null);
  useEffect(() => {
    if (!useOverride) {
      setLiveModels(null);
      return;
    }
    if (livePreset === "custom" && !settings.live_openai_base_url) {
      setLiveModels(null);
      return;
    }
    let cancelled = false;
    api
      .getAvailableModels(
        settings.live_ai_provider || "anthropic",
        settings.live_openai_base_url || undefined,
        "live",
      )
      .then((r) => {
        if (cancelled) return;
        if (r.models && r.models.length) {
          setLiveModels(r.models);
        } else {
          setLiveModels(null);
        }
      })
      .catch(() => {
        if (!cancelled) setLiveModels(null);
      });
    return () => {
      cancelled = true;
    };
  }, [useOverride, livePreset, settings.live_ai_provider,
      settings.live_openai_base_url]);

  // Pick a preset's hardcoded fallback list — same as the main
  // section. OpenRouter and Custom don't have a curated list here
  // (OpenRouter has 200+ models; Custom is by definition unknown).
  const liveOrFallback = (
    fallback: { value: string; label: string }[]
  ) => (liveModels && liveModels.length ? liveModels : fallback);
  const livePresetModels =
    livePreset === "anthropic" ? liveOrFallback(ANTHROPIC_MODELS)
    : livePreset === "ollama" ? liveOrFallback(OLLAMA_MODELS)
    : livePreset === "groq" ? liveOrFallback(GROQ_MODELS)
    : livePreset === "gemini" ? liveOrFallback(GEMINI_MODELS)
    : livePreset === "openrouter" ? (liveModels || [])
    : null;
  const liveModelIsPreset = livePresetModels
    ? livePresetModels.some((m) => m.value === settings.live_claude_model)
    : false;

  // Apply a preset — populates live_ai_provider, live_openai_base_url,
  // and (when sensible) live_claude_model. Mirrors applyPreset in the
  // main section.
  const applyLivePreset = (next: ProviderPreset) => {
    if (next === "anthropic") {
      update("live_ai_provider", "anthropic");
      update("live_openai_base_url", "");
      if (!ANTHROPIC_MODELS.find((m) => m.value === settings.live_claude_model)) {
        update("live_claude_model", ANTHROPIC_MODELS[0].value);
      }
      return;
    }
    update("live_ai_provider", "openai");
    if (next === "openrouter") {
      update("live_openai_base_url", OPENROUTER_BASE);
    } else if (next === "ollama") {
      update("live_openai_base_url", OLLAMA_BASE);
      if (!OLLAMA_MODELS.find((m) => m.value === settings.live_claude_model)) {
        update("live_claude_model", OLLAMA_MODELS[0].value);
      }
    } else if (next === "groq") {
      update("live_openai_base_url", GROQ_BASE);
      if (!GROQ_MODELS.find((m) => m.value === settings.live_claude_model)) {
        update("live_claude_model", GROQ_MODELS[0].value);
      }
    } else if (next === "gemini") {
      update("live_openai_base_url", GEMINI_BASE);
      if (!GEMINI_MODELS.find((m) => m.value === settings.live_claude_model)) {
        update("live_claude_model", GEMINI_MODELS[0].value);
      }
    } else if (next === "custom") {
      // Leave URL alone so the user fills it in.
    }
  };

  // Test connection — scope="live" so the backend probes the live
  // summarizer. Same UI shape as the main section's Test connection
  // (emerald on success, red on failure, latency in ms, verbatim error).
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<
    | { ok: true; latency_ms: number; reply: string; provider: string; model: string }
    | { ok: false; error: string; latency_ms: number; provider: string; model: string }
    | null
  >(null);
  const runLiveConnectionTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await api.testLLMConnection("live");
      if (res.ok) {
        setTestResult({
          ok: true,
          latency_ms: res.latency_ms,
          reply: res.reply || "",
          provider: res.provider,
          model: res.model,
        });
      } else {
        setTestResult({
          ok: false,
          latency_ms: res.latency_ms,
          error: res.error || "Unknown error",
          provider: res.provider,
          model: res.model,
        });
      }
    } catch (e) {
      setTestResult({
        ok: false,
        latency_ms: 0,
        error: e instanceof Error ? e.message : String(e),
        provider: settings.live_ai_provider || settings.ai_provider,
        model: settings.live_claude_model || settings.claude_model,
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>
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
              // Default to Anthropic so the preset switcher starts on a
              // valid value rather than the bare "openai" mode that
              // previously required the user to fill in everything.
              update("live_ai_provider", "anthropic");
              update("live_openai_base_url", "");
              if (!settings.live_claude_model) {
                update("live_claude_model", ANTHROPIC_MODELS[0].value);
              }
            } else {
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
              <Label>AI Provider</Label>
              <Select
                value={livePreset}
                onValueChange={(v) => v && applyLivePreset(v as ProviderPreset)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="anthropic">Anthropic — Claude (uses Anthropic API key below)</SelectItem>
                  <SelectItem value="groq">Groq — free, fastest hosted inference (Llama, Mixtral, Gemma)</SelectItem>
                  <SelectItem value="gemini">Google Gemini — free tier (Gemini 2.5 Flash, 2.5 Pro, …)</SelectItem>
                  <SelectItem value="openrouter">OpenRouter — free-tier Llama / Gemini / Qwen / DeepSeek</SelectItem>
                  <SelectItem value="ollama">Ollama (local) — free, runs on your machine</SelectItem>
                  <SelectItem value="custom">Custom OpenAI-compatible endpoint</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-[11px] text-muted-foreground">
                {livePreset === "anthropic" && (
                  <>Uses Claude directly. The cheapest Haiku model is the default for tick calls.</>
                )}
                {livePreset === "groq" && (
                  <>
                    Get a free API key at{" "}
                    <a href="https://console.groq.com/keys" className="underline" target="_blank" rel="noreferrer">
                      console.groq.com
                    </a>
                    . Very fast inference (often &lt;1s per tick).
                  </>
                )}
                {livePreset === "gemini" && (
                  <>
                    Get a free API key at{" "}
                    <a href="https://aistudio.google.com/apikey" className="underline" target="_blank" rel="noreferrer">
                      aistudio.google.com
                    </a>
                    . Free tier has daily request limits but ticks are tiny calls — fits fine.
                  </>
                )}
                {livePreset === "openrouter" && (
                  <>
                    Get a free API key at{" "}
                    <a href="https://openrouter.ai/settings/keys" className="underline" target="_blank" rel="noreferrer">
                      openrouter.ai
                    </a>
                    . Free-tier models cap at ~50 req/day; a long meeting (~80 ticks) hits the cap.
                  </>
                )}
                {livePreset === "ollama" && (
                  <>
                    Install Ollama from{" "}
                    <a href="https://ollama.com/download" className="underline" target="_blank" rel="noreferrer">
                      ollama.com
                    </a>{" "}
                    and run <code className="text-[11px]">ollama pull llama3.1</code>.
                    Everything stays on your machine. No API key needed.
                  </>
                )}
                {livePreset === "custom" && (
                  <>Any OpenAI Chat Completions-compatible endpoint — LM Studio, vLLM, LocalAI, etc.</>
                )}
              </p>
            </div>

            {/* Base URL — auto-set by preset; editable for custom. */}
            {(livePreset === "ollama" || livePreset === "custom") && (
              <div className="space-y-2">
                <Label>Base URL</Label>
                <Input
                  value={settings.live_openai_base_url}
                  onChange={(e) => update("live_openai_base_url", e.target.value)}
                  placeholder="http://localhost:11434/v1   or   https://api.example.com/v1"
                  className="font-mono text-sm"
                />
              </div>
            )}

            {/* API key — Anthropic vs OpenAI-compat depending on preset. */}
            {livePreset === "anthropic" && (
              <div className="space-y-2">
                <Label>Anthropic API key (optional)</Label>
                <Input
                  type="password"
                  value={settings.live_anthropic_api_key}
                  onChange={(e) => update("live_anthropic_api_key", e.target.value)}
                  placeholder="Leave blank to reuse the main Anthropic key"
                  autoComplete="off"
                />
              </div>
            )}
            {(livePreset === "groq" || livePreset === "gemini" ||
              livePreset === "openrouter" || livePreset === "ollama" ||
              livePreset === "custom") && (
              <div className="space-y-2">
                <Label>
                  {livePreset === "groq" ? "Groq API key"
                    : livePreset === "gemini" ? "Gemini API key"
                    : livePreset === "openrouter" ? "OpenRouter API key"
                    : "API key"}
                </Label>
                <Input
                  type="password"
                  value={settings.live_openai_api_key}
                  onChange={(e) => update("live_openai_api_key", e.target.value)}
                  placeholder={livePreset === "ollama"
                    ? "any non-empty string"
                    : "sk-..."}
                  autoComplete="off"
                />
              </div>
            )}

            {/* Model picker — dropdown when we have a list, free-form
                input for custom or when the saved model isn't in the
                current list (so power users can paste arbitrary ids). */}
            <div className="space-y-2">
              <Label>Live tick model</Label>
              {livePresetModels && livePresetModels.length && liveModelIsPreset ? (
                <Select
                  value={settings.live_claude_model}
                  onValueChange={(v) => v && update("live_claude_model", v)}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {livePresetModels.map((m) => (
                      <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  value={settings.live_claude_model}
                  onChange={(e) => update("live_claude_model", e.target.value)}
                  placeholder={livePreset === "anthropic"
                    ? "claude-haiku-4-5"
                    : "model id, e.g. llama-3.1-70b-instruct"}
                  className="font-mono text-sm"
                />
              )}
              {livePresetModels && livePresetModels.length > 0 && !liveModelIsPreset && (
                <p className="text-[11px] text-amber-700">
                  Saved model isn&apos;t in the live list — using free-form input.
                  Switch presets or pick from the list below to use the dropdown.
                </p>
              )}
              <p className="text-[11px] text-muted-foreground">
                Leave blank to reuse the main provider&apos;s model id.
                {liveModels && liveModels.length
                  ? " (Live list from provider's /models endpoint.)"
                  : null}
              </p>
            </div>

            {/* Test connection — scope=live so backend probes the live
                summarizer, not the main one. */}
            <div className="flex items-center gap-3">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={runLiveConnectionTest}
                disabled={testing}
              >
                {testing ? "Testing..." : "Test live connection"}
              </Button>
              <span className="text-[11px] text-muted-foreground">
                Fires a 1-token chat completion against the saved LIVE
                provider. Save settings first if you just edited them.
              </span>
            </div>
            {testResult && testResult.ok && (
              <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs">
                <div className="font-medium text-emerald-900">
                  ✓ Connected · {testResult.latency_ms} ms
                </div>
                <div className="text-emerald-800 mt-0.5">
                  {testResult.provider} / {testResult.model}
                  {testResult.reply ? ` — replied "${testResult.reply}"` : ""}
                </div>
              </div>
            )}
            {testResult && !testResult.ok && (
              <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs">
                <div className="font-medium text-red-900">
                  ✗ Could not reach {testResult.provider} / {testResult.model}
                </div>
                <div className="text-red-800 mt-0.5 font-mono break-words">
                  {testResult.error}
                </div>
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
        <CardTitle>Auto-record skip patterns</CardTitle>
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

// Session Archive status readout + "Sync now". Deliberately fetches its
// own status independent of the Settings form's dirty/clean state — the
// folder path in the form above may be mid-edit and unsaved, but this
// panel always reflects what's ACTUALLY configured server-side right
// now, same as ClientExportStatus does for Designated Folders in
// clients-view.tsx. `savedAt` is bumped by the parent after every
// successful Save Settings so a folder change refreshes this without
// requiring the user to leave and re-enter the tab.
function SessionArchiveStatusPanel({ savedAt }: { savedAt: number }) {
  const [status, setStatus] = useState<ArchiveStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.getArchiveStatus());
    } catch {
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadStatus(); }, [loadStatus, savedAt]);

  const syncNow = async () => {
    setSyncing(true);
    try {
      const res = await api.syncArchive();
      setStatus(res);
      toast.success(
        res.queued
          ? `Queued ${res.queued} session${res.queued === 1 ? "" : "s"} to copy into the archive`
          : "Everything is already in the archive"
      );
    } catch (e) {
      toast.error(`Sync failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSyncing(false);
    }
  };

  if (loading || !status || !status.folder) {
    // Nothing configured (or the check itself failed) — no readout to
    // show. The input + explanation above already cover the empty case.
    return null;
  }

  // Clients & templates roaming line (field report 2026-08-07):
  // client_configs.json / summary_templates.json ride along with the
  // Session Archive too (see backend/services/shared_state_sync.py) but
  // are a separate, smaller sync with their own last-writer-wins-by-mtime
  // rule — worth one line so a user whose new client isn't showing up on
  // the other machine yet can see why, rather than the readout only ever
  // talking about session counts.
  const sharedState = status.shared_state ?? {};
  const sharedRows = Object.values(sharedState);
  const sharedReasons = sharedRows
    .map((r) => r.reason)
    .filter((r): r is string => !!r);
  // Foreign per-machine folder paths healed by the last reconcile sweep
  // (field report 2026-08-07: a Windows client_configs.json roamed a
  // `G:\My Drive\...` export folder onto the user's Mac, which then kept
  // re-queuing exports against a drive letter that can never exist
  // there). Summed across rows even though only client_configs.json
  // ever carries this key, so the count stays correct if that changes.
  const sanitizedClearedCount = sharedRows.reduce(
    (sum, r) => sum + (r.sanitized_cleared?.length ?? 0), 0
  );
  let sharedLine = "Clients & templates: in sync";
  if (sharedRows.some((r) => r.direction === "pull")) {
    sharedLine = "Clients & templates: will pull from archive";
  } else if (sharedRows.some((r) => r.direction === "push")) {
    sharedLine = "Clients & templates: will push to archive";
  }

  return (
    <div className="flex items-center justify-between gap-2 rounded-2xl border border-border bg-muted/30 px-3.5 py-3">
      <div className="text-xs min-w-0 space-y-1.5">
        <div className="text-muted-foreground">
          <strong className="text-foreground">{status.sessions_in_archive}</strong>{" "}
          session{status.sessions_in_archive === 1 ? "" : "s"} in shared archive
          {" · "}
          <strong className="text-foreground">{status.sessions_local}</strong>{" "}
          local
          {" · "}
          <strong className="text-foreground">{status.pending}</strong>{" "}
          pending
        </div>
        {sharedRows.length > 0 && (
          <div className="text-muted-foreground">{sharedLine}</div>
        )}
        <div className="flex flex-wrap gap-1.5">
          {!status.folder_present && (
            <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-amber-700 dark:text-amber-300">
              <span aria-hidden className="font-bold leading-none">⚠</span>
              Folder not reachable right now — sync client offline, drive
              unmounted, or the path changed. Nothing can copy until it&apos;s
              back.
            </span>
          )}
          {sharedReasons.map((reason, i) => (
            <span key={i} className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-amber-700 dark:text-amber-300">
              <span aria-hidden className="font-bold leading-none">⚠</span>
              {reason}
            </span>
          ))}
          {sanitizedClearedCount > 0 && (
            <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-amber-700 dark:text-amber-300">
              <span aria-hidden className="font-bold leading-none">⚠</span>
              Cleared {sanitizedClearedCount} folder path{sanitizedClearedCount === 1 ? "" : "s"} that
              belong{sanitizedClearedCount === 1 ? "s" : ""} to another machine
            </span>
          )}
        </div>
      </div>
      <Button
        size="sm" variant="outline" onClick={syncNow}
        disabled={syncing || !status.folder_present}
        title="Copy any local sessions missing from the archive"
      >
        {syncing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Sync now"}
      </Button>
    </div>
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
        <CardTitle>App Updates</CardTitle>
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
            <CardTitle>Summary Templates</CardTitle>
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
            <CardTitle>{title}</CardTitle>
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
        <CardTitle>
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

// Human-readable "how long ago" for the crash-recency line — deliberately
// coarse (days, not hours/minutes): the point is letting the user judge
// "is this the crash I already fixed" at a glance, not a live-ticking
// clock.
function formatDaysAgo(d: Date): string {
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "1 day ago";
  return `${days} days ago`;
}

// System health panel. Surfaces the signals we kept having to dig out of
// backend.log by hand — live-model reachability (the silent Ollama
// failure), provider config, mic/loopback, recordings-dir writability —
// plus a log tail so the user never needs PowerShell.
/**
 * Export diagnostics — one zip a user can attach to a bug report.
 *
 * Replaces the five separate hand-written .bat scripts that field
 * debugging used to require the user to run by hand.
 *
 * The contents list is shown BEFORE the export (from the preview
 * endpoint) and again AFTER it, the second time read back out of the
 * finished archive — so the user never has to wonder what they just
 * shared. Same button-plus-result-line shape as the Chrome Extension
 * card above.
 */
function DiagnosticsExportCard() {
  const [preview, setPreview] = useState<import("@/lib/api").DiagnosticsExportPreview | null>(null);
  const [result, setResult] = useState<import("@/lib/api").DiagnosticsExport | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    (async () => {
      try {
        setPreview(await api.getDiagnosticsExportPreview());
      } catch {
        // A backend too old to have the preview endpoint still exports;
        // the contents list just isn't available up front.
        setPreview(null);
      }
    })();
  }, []);

  const runExport = async () => {
    setBusy(true);
    setMsg("");
    try {
      const res = await api.exportDiagnostics();
      setResult(res);
      setMsg(`Wrote ${res.filename} (${(res.bytes / 1024).toFixed(0)} KB, ${res.members.length} files).`);
    } catch (e) {
      setMsg("");
      toast.error(`Export failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const showInFolder = async () => {
    if (!result?.path) return;
    const folder = result.path.replace(/[\\/][^\\/]+$/, "");
    try {
      await api.openFolder({ kind: "path", path: folder });
    } catch (e) {
      toast.error(`Couldn't open the folder: ${e instanceof Error ? e.message : e}`);
    }
  };

  const copyPath = () => {
    if (!result?.path) return;
    navigator.clipboard?.writeText(result.path).then(
      () => toast.success("Path copied"),
      () => toast.error("Couldn't copy"),
    );
  };

  // After an export, describe what's actually in the archive; before
  // one, describe what would be.
  const contents = result ?? preview;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Export diagnostics</CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          Bundles everything a bug report needs into one zip: the
          structured event log, recent backend and crash logs, versions,
          your OS and audio devices, and your settings with secrets
          redacted. Nothing here needs a terminal.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {contents && (
          <div className="rounded-md border p-3 space-y-2">
            <Label className="text-[11px] text-muted-foreground">
              {result ? "What this zip contains" : "What the zip will contain"}
            </Label>
            <ul className="space-y-1.5">
              {contents.members.map((m) => (
                <li key={m} className="text-xs">
                  <code className="font-mono text-[11px]">{m}</code>
                  {contents.descriptions?.[m] && (
                    <span className="text-muted-foreground">
                      {" "}— {contents.descriptions[m]}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {contents?.excluded && contents.excluded.length > 0 && (
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2.5 py-2">
            <div className="flex items-start gap-2 text-xs text-emerald-700 dark:text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              <div>
                <div className="font-medium">Deliberately not included</div>
                <ul className="mt-1 space-y-0.5">
                  {contents.excluded.map((x) => (
                    <li key={x}>· {x}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        )}

        <Button type="button" size="sm" onClick={runExport} disabled={busy}>
          {busy
            ? <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            : <DownloadCloud className="h-4 w-4 mr-2" />}
          Export diagnostics
        </Button>
        {msg && <p className="text-xs text-muted-foreground">{msg}</p>}

        {result?.path && (
          <div className="space-y-1">
            <Label className="text-[11px] text-muted-foreground">Saved to</Label>
            <div className="flex gap-2">
              <Input value={result.path} readOnly className="font-mono text-xs" />
              <Button type="button" variant="outline" size="sm" onClick={copyPath}>
                <Copy className="h-3.5 w-3.5" />
              </Button>
              <Button type="button" variant="outline" size="sm" onClick={showInFolder}>
                Show
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function DiagnosticsCard() {
  const [diag, setDiag] = useState<import("@/lib/api").Diagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [showLog, setShowLog] = useState(false);
  const [showCrash, setShowCrash] = useState(false);

  const refresh = async () => {
    setLoading(true);
    try {
      setDiag(await api.getDiagnostics());
    } catch (e) {
      toast.error(`Diagnostics failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const dot = (status: string) => {
    const cls =
      status === "ok" ? "bg-green-500"
        : status === "warn" ? "bg-amber-500"
          : status === "error" ? "bg-red-500"
            : "bg-zinc-400";
    return <span className={`inline-block h-2.5 w-2.5 rounded-full ${cls} shrink-0`} />;
  };

  const copyLog = () => {
    if (!diag?.log_tail) return;
    navigator.clipboard?.writeText(diag.log_tail).then(
      () => toast.success("Log tail copied"),
      () => toast.error("Couldn't copy"),
    );
  };

  // crash.log holds the faulthandler dump from a NATIVE crash (the
  // Windows 0xC0000005 / 3221225477 exit that's been unexplained since
  // v2.0.18). The file is append-only and never deleted, so its mere
  // presence would make this banner permanent — a crash from weeks ago,
  // long since fixed, would nag forever. The backend now reports
  // RECENCY (last_crash_at + a 7-day crash_is_recent threshold), so the
  // warning banner only shows for a crash that's still likely relevant.
  // "Show/Copy crash log" stay available whenever there's ANY history —
  // even an old crash is useful context when diagnosing a new one.
  const crashTail = (diag?.crash_tail || "").trim();
  const hasCrashHistory = crashTail.length > 0;
  const crashIsRecent = !!diag?.crash_is_recent;
  const lastCrashAt = diag?.last_crash_at
    ? new Date(diag.last_crash_at)
    : null;
  const lastCrashLabel = lastCrashAt
    ? `${lastCrashAt.toLocaleString()} (${formatDaysAgo(lastCrashAt)})`
    : null;

  const copyCrash = () => {
    if (!crashTail) return;
    navigator.clipboard?.writeText(crashTail).then(
      () => toast.success("Crash log copied"),
      () => toast.error("Couldn't copy"),
    );
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Diagnostics</CardTitle>
        <Button size="sm" variant="outline" onClick={refresh} disabled={loading}>
          {loading
            ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
            : <RotateCcw className="h-4 w-4 mr-1.5" />}
          Refresh
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Live health of the things that fail quietly — your Live Co-Pilot
          model (is Ollama running?), AI provider config, microphone +
          system-audio devices, and whether the recordings folder is
          writable.
        </p>

        {diag && (
          <div className="space-y-1.5">
            {(Array.isArray(diag.checks) ? diag.checks : []).map((c) => (
              <div key={c.id} className="flex items-start gap-2.5 text-sm">
                <span className="mt-1.5">{dot(c.status)}</span>
                <div className="min-w-0">
                  <span className="font-medium text-foreground">{c.label}</span>
                  {c.detail && (
                    <span className="text-muted-foreground"> — {c.detail}</span>
                  )}
                </div>
              </div>
            ))}
            {!Array.isArray(diag.checks) && (
              <div className="text-sm text-muted-foreground">
                Checks unavailable — the diagnostics response was incomplete.
              </div>
            )}
          </div>
        )}

        {diag && (
          <div className="space-y-2 pt-1">
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setShowLog((v) => !v)}
              >
                {showLog ? "Hide" : "Show"} recent backend log
              </Button>
              {showLog && (
                <Button size="sm" variant="ghost" onClick={copyLog}>
                  Copy log
                </Button>
              )}
            </div>
            {showLog && (
              <pre className="max-h-80 overflow-auto rounded-md border bg-muted/40 p-3 text-[11px] leading-relaxed font-mono whitespace-pre-wrap break-words">
                {diag.log_tail || "(log empty)"}
              </pre>
            )}
          </div>
        )}

        {hasCrashHistory && (
          <div
            className={
              crashIsRecent
                ? "space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950/40"
                : "space-y-2 rounded-md border border-border bg-muted/30 p-3"
            }
          >
            <div className="text-sm font-medium">
              {crashIsRecent
                ? "The backend crashed recently"
                : "The backend has crashed before"}
            </div>
            <div className="text-xs text-muted-foreground">
              {crashIsRecent ? (
                <>
                  A native crash dump was captured in <code>crash.log</code>.
                  This is the traceback the backend&apos;s own log can&apos;t
                  hold — copy it into a bug report and it says exactly where
                  the process died.
                </>
              ) : (
                <>
                  No recent crashes — this is history from an earlier build,
                  kept for reference. <code>crash.log</code> is append-only
                  and never cleared, so old entries stay available without
                  keeping this warning up indefinitely.
                </>
              )}
              {lastCrashLabel && (
                <div className="mt-1">Last crash: {lastCrashLabel}</div>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setShowCrash((v) => !v)}
              >
                {showCrash ? "Hide" : "Show"} crash log
              </Button>
              <Button size="sm" variant="ghost" onClick={copyCrash}>
                Copy crash log
              </Button>
            </div>
            {showCrash && (
              <pre className="max-h-80 overflow-auto rounded-md border bg-background/60 p-3 text-[11px] leading-relaxed font-mono whitespace-pre-wrap break-words">
                {crashTail}
              </pre>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// Domain terminology editor. Two plain-text editors (terms one-per-line;
// corrections as "wrong = canonical" per line) kept deliberately simple
// — the value is in the seeded vocabulary, not a fancy UI. Biases Whisper
// transcription toward the user's jargon and corrects known mis-hears.
function TerminologyCard() {
  const [termsText, setTermsText] = useState("");
  const [corrText, setCorrText] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const hydrate = (t: import("@/lib/api").Terminology) => {
    setTermsText((t.terms || []).join("\n"));
    setCorrText(
      Object.entries(t.corrections || {})
        .map(([wrong, canon]) => `${wrong} = ${canon}`)
        .join("\n"),
    );
  };

  useEffect(() => {
    api.getTerminology()
      .then(hydrate)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const parseCorrections = (text: string): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const idx = line.indexOf("=");
      if (idx < 0) continue;
      const wrong = line.slice(0, idx).trim().toLowerCase();
      const canon = line.slice(idx + 1).trim();
      if (wrong && canon) out[wrong] = canon;
    }
    return out;
  };

  const save = async () => {
    setSaving(true);
    try {
      const terms = termsText
        .split("\n").map((s) => s.trim()).filter(Boolean);
      const corrections = parseCorrections(corrText);
      const saved = await api.putTerminology({ terms, corrections });
      hydrate(saved);
      toast.success("Terminology saved", {
        description: "Applies to your next recording's transcription.",
      });
    } catch (e) {
      toast.error(`Save failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    const ok = await confirmDialog(
      "Replace your terms and corrections with the built-in SA / CCaaS / cloud / sales vocabulary? This can't be undone.",
      { title: "Reset terminology to defaults?" },
    );
    if (!ok) return;
    setSaving(true);
    try {
      const r = await api.resetTerminology();
      hydrate(r);
      toast.success("Terminology reset to defaults");
    } catch (e) {
      toast.error(`Reset failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Domain terminology</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Biases transcription toward your jargon and fixes known mis-hears.
          Whisper otherwise mangles dense terms — &quot;Genesys&quot; →
          &quot;Genesis&quot;, &quot;UCCX&quot; → &quot;you see ex&quot;,
          &quot;CCaaS&quot; → &quot;see-cass&quot; — which then poisons every
          summary and extraction. Seeded with a curated Solutions Architect /
          CCaaS / cloud / sales vocabulary; edit freely. Applies to the next
          recording you process.
        </p>

        {loading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading glossary…
          </div>
        ) : (
          <>
            <div className="space-y-1.5">
              <Label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Terms (one per line)
              </Label>
              <p className="text-[11px] text-muted-foreground">
                Proper nouns + acronyms the transcriber should recognize.
                These bias Whisper toward the right spelling.
              </p>
              <Textarea
                value={termsText}
                onChange={(e) => setTermsText(e.target.value)}
                className="min-h-[160px] max-h-[320px] font-mono text-xs"
                placeholder="Amazon Connect&#10;Genesys Cloud&#10;CCaaS&#10;MEDDIC"
              />
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Corrections (one per line: wrong = canonical)
              </Label>
              <p className="text-[11px] text-muted-foreground">
                Specific mis-hears to fix after transcription. Left side is
                matched case-insensitively; right side is the replacement.
              </p>
              <Textarea
                value={corrText}
                onChange={(e) => setCorrText(e.target.value)}
                className="min-h-[140px] max-h-[320px] font-mono text-xs"
                placeholder="genesis = Genesys&#10;you see ex = UCCX&#10;see-cass = CCaaS"
              />
            </div>

            <div className="flex items-center gap-2">
              <Button onClick={save} disabled={saving} size="sm">
                {saving
                  ? <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                  : <Save className="h-4 w-4 mr-1.5" />}
                Save terminology
              </Button>
              <Button onClick={reset} disabled={saving} size="sm" variant="outline">
                <RotateCcw className="h-4 w-4 mr-1.5" />
                Reset to defaults
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
