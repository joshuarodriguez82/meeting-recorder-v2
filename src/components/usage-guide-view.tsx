"use client";

import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";

type Section = {
  id: string;
  title: string;
  content: React.ReactNode;
};

const SECTIONS: Section[] = [
  {
    id: "getting-started",
    title: "Getting Started",
    content: (
      <>
        <p>
          Meeting Recorder is a local desktop tool that captures meetings, transcribes them with AI,
          and extracts structured notes — summaries, action items, requirements, and decisions.
        </p>
        <p>Before you record, configure in <strong>Settings</strong>:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>API keys — Anthropic (Claude) and HuggingFace (speaker ID)</li>
          <li>Audio devices — microphone and a loopback device for System Audio</li>
          <li>Default email recipient (optional)</li>
        </ul>
        <Tip>
          <strong>Classic Outlook required</strong> for calendar + email features. New Outlook
          doesn&apos;t support COM automation.
        </Tip>
      </>
    ),
  },
  {
    id: "recording",
    title: "Recording a Meeting",
    content: (
      <>
        <ol className="list-decimal pl-5 space-y-2">
          <li>Pick a meeting from <strong>Today&apos;s Meetings</strong> or type a name.</li>
          <li>Tag it with Client/Project to power Dashboard and Follow-Ups.</li>
          <li>Pick a Template (General, Requirements, Design Review, etc.).</li>
          <li>Choose your mic + System Audio (loopback).</li>
          <li>Hit <strong>Start Recording</strong>.</li>
          <li>When done, <strong>Stop</strong> — audio is saved and the session opens.</li>
        </ol>
        <Warn>
          If System Audio is set to &quot;Skip&quot; or not a loopback device, only your voice is
          captured. The other participants won&apos;t be on the recording.
        </Warn>
      </>
    ),
  },
  {
    id: "extract",
    title: "AI Extraction",
    content: (
      <>
        <p>After recording stops, click buttons in order — or turn on <strong>Auto-Process</strong> in Settings:</p>
        <div className="grid grid-cols-1 gap-3 mt-3">
          <BtnDesc emoji="⚙" name="Process" desc="Transcribes audio + identifies speakers." />
          <BtnDesc emoji="✨" name="Summarize" desc="Template-tailored AI summary." />
          <BtnDesc emoji="📋" name="Action Items" desc="Owner, description, due date, plus open questions." />
          <BtnDesc emoji="📝" name="Requirements" desc="FR/NFR tables with priority and owner." />
          <BtnDesc emoji="🎯" name="Decisions" desc="ADR-style decision log (Decided, Rationale, Alternatives, Owner, Impact)." />
        </div>
      </>
    ),
  },
  {
    id: "knowledge",
    title: "Knowledge Base",
    content: (
      <>
        <p>Every recording becomes searchable. Use the sidebar:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Sessions</strong> — all past recordings, bulk-process, delete.</li>
          <li><strong>Follow-Ups</strong> — every action item filterable by owner/client/status.</li>
          <li><strong>Decisions</strong> — auto-generated ADR log across time.</li>
          <li><strong>Search</strong> — full-text across every transcript.</li>
          <li><strong>Clients</strong> — per-client dashboard with stats.</li>
        </ul>
      </>
    ),
  },
  {
    id: "workflow",
    title: "Workflow Automation",
    content: (
      <>
        <p>In <strong>Settings &gt; Workflow</strong>, turn the app into fire-and-forget:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Auto-process after stop</li>
          <li>Auto-draft follow-up email to attendees</li>
          <li>Launch on Windows startup</li>
        </ul>
      </>
    ),
  },
  {
    id: "retention",
    title: "Retention",
    content: (
      <>
        <p>
          Meeting audio is bulky — a 1-hour meeting is ~230 MB. Over time the folder grows.
        </p>
        <p>
          In <strong>Settings &gt; Retention</strong>, enable automatic cleanup with separate
          thresholds for processed (default 7 days) and unprocessed (30 days) audio.
        </p>
        <Tip>
          Transcripts, summaries, action items, and decisions are <strong>never</strong> deleted —
          only the raw WAV files. Everything stays searchable.
        </Tip>
      </>
    ),
  },
  {
    id: "cost",
    title: "Cost",
    content: (
      <>
        <p>
          Default is Claude <strong>Haiku 4.5</strong> (~$1/M input + $5/M output tokens). A typical
          meeting with all extractions costs under $0.05. Hundreds of meetings per year for a few dollars.
        </p>
        <Tip>
          For complex design reviews where nuance matters, switch to Sonnet 4.5 in Settings.
          ~4× the cost but higher quality.
        </Tip>
      </>
    ),
  },
  {
    id: "troubleshoot",
    title: "Troubleshooting",
    content: (
      <>
        <dl className="space-y-4">
          <div>
            <dt className="font-medium text-sm">Only my voice was recorded</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              System Audio isn&apos;t a loopback device. Enable Stereo Mix in Windows Sound settings
              (Recording tab &rarr; right-click &rarr; Show Disabled Devices &rarr; Enable), or install
              VB-Cable.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">Calendar shows no meetings</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Requires Classic Outlook. Switch from New Outlook in Outlook settings.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">Models failed to load</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Invalid HuggingFace token or you haven&apos;t accepted pyannote terms. Visit
              huggingface.co/pyannote/speaker-diarization-3.1 and huggingface.co/pyannote/segmentation-3.0,
              click &quot;Agree and access repository&quot;, then restart the app.
            </dd>
          </div>
        </dl>
      </>
    ),
  },
];

export function UsageGuideView() {
  const [active, setActive] = useState(SECTIONS[0].id);
  const section = SECTIONS.find((s) => s.id === active) || SECTIONS[0];

  return (
    <div className="mx-auto max-w-6xl grid grid-cols-1 md:grid-cols-[220px_1fr] gap-6">
      <nav className="space-y-0.5">
        <div className="mb-2 px-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          Topics
        </div>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            onClick={() => setActive(s.id)}
            className={`flex w-full rounded-md px-2.5 py-2 text-sm text-left transition-colors ${
              active === s.id
                ? "bg-accent text-accent-foreground font-medium"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
            }`}
          >
            {s.title}
          </button>
        ))}
      </nav>

      <Card>
        <CardContent className="p-8 space-y-4 prose-sm max-w-none">
          <h2 className="text-xl font-bold text-primary mb-4">{section.title}</h2>
          <div className="space-y-3 text-sm leading-relaxed">
            {section.content}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function Tip({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-primary/30 bg-accent/50 px-4 py-3 my-3 text-sm">
      <span className="font-medium text-primary">💡 Tip </span>
      {children}
    </div>
  );
}

function Warn({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40 px-4 py-3 my-3 text-sm text-amber-900 dark:text-amber-200">
      <span className="font-medium">⚠ Note </span>
      {children}
    </div>
  );
}

function BtnDesc({ emoji, name, desc }: { emoji: string; name: string; desc: string }) {
  return (
    <div className="flex items-start gap-3">
      <div className="inline-flex items-center gap-1.5 rounded-md bg-primary text-primary-foreground px-2.5 py-1 text-xs font-medium shrink-0">
        <span>{emoji}</span>{name}
      </div>
      <span className="text-sm text-muted-foreground">{desc}</span>
    </div>
  );
}
