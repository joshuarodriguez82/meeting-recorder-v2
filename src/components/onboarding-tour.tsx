"use client";

/**
 * First-run guided tour. A read-only, multi-step walkthrough that orients
 * a brand-new user (no API keys, no sessions) on how to set up and use
 * the app. It explains each piece and links to where the action happens
 * (Settings / Record) rather than embedding live controls — the user
 * performs the steps themselves.
 *
 * Trigger (owned by page.tsx): auto-shows on true first run — no usable
 * AI key configured AND zero sessions on disk — unless the user has
 * dismissed it before (localStorage flag). Re-openable anytime from the
 * Help tab regardless of the flag.
 */

import { useState } from "react";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  Mic, KeyRound, Users, Sparkles, CheckCircle2, ArrowRight, ArrowLeft,
  ExternalLink, Rocket, FileText, MessageSquare,
} from "lucide-react";

const ONBOARDING_FLAG = "mr_onboarding_dismissed_v1";

export function onboardingDismissed(): boolean {
  try {
    return localStorage.getItem(ONBOARDING_FLAG) === "1";
  } catch {
    return false;
  }
}

function markDismissed() {
  try {
    localStorage.setItem(ONBOARDING_FLAG, "1");
  } catch {
    /* localStorage unavailable — worst case the tour shows again, harmless */
  }
}

interface Props {
  open: boolean;
  onClose: () => void;
  // Jump to a sidebar destination (e.g. "settings", "record") and close.
  onNavigate: (id: string) => void;
}

type Step = {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  body: React.ReactNode;
  cta?: { label: string; nav: string };
};

export function OnboardingTour({ open, onClose, onNavigate }: Props) {
  const [i, setI] = useState(0);

  const steps: Step[] = [
    {
      icon: Rocket,
      title: "Welcome to Meeting Recorder",
      body: (
        <>
          <p>
            This app records your meetings, transcribes them <strong>on your
            machine</strong> with Whisper, identifies speakers, and uses AI to
            pull out a summary, action items, decisions, and requirements.
          </p>
          <p className="mt-2">
            Your audio and transcripts never leave your computer — only the
            short summarization call goes to your chosen AI provider (and with
            local Ollama, even that stays on-device).
          </p>
          <p className="mt-2 text-muted-foreground">
            This quick tour points you at the few things to set up first.
            You can skip it and re-open it anytime from the Help tab.
          </p>
        </>
      ),
    },
    {
      icon: KeyRound,
      title: "Step 1 — Choose an AI provider",
      body: (
        <>
          <p>
            The summaries and extractions need an AI model. In{" "}
            <strong>Settings → AI Provider</strong>, pick one:
          </p>
          <ul className="list-disc pl-5 mt-2 space-y-1">
            <li>
              <strong>Anthropic / Claude</strong> — best quality. Get a key at{" "}
              <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer" className="text-primary hover:underline">
                console.anthropic.com <ExternalLink className="inline h-3 w-3" />
              </a>. Pennies per meeting.
            </li>
            <li>
              <strong>OpenRouter free tier</strong> — $0, rate-limited. Good to start.
            </li>
            <li>
              <strong>Ollama</strong> — fully local, free, no key. Needs Ollama
              running on your machine.
            </li>
          </ul>
          <p className="mt-2 text-muted-foreground">
            Paste the key in Settings and use the Diagnostics panel there to
            confirm it&apos;s working (green check).
          </p>
        </>
      ),
      cta: { label: "Open Settings", nav: "settings" },
    },
    {
      icon: Users,
      title: "Step 2 — Add a HuggingFace token (speakers)",
      body: (
        <>
          <p>
            Speaker identification (who said what) uses Pyannote, which needs a
            free HuggingFace token.
          </p>
          <ol className="list-decimal pl-5 mt-2 space-y-1">
            <li>
              Create a token at{" "}
              <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer" className="text-primary hover:underline">
                huggingface.co/settings/tokens <ExternalLink className="inline h-3 w-3" />
              </a>.
            </li>
            <li>
              Accept the model terms for{" "}
              <a href="https://huggingface.co/pyannote/speaker-diarization-3.1" target="_blank" rel="noreferrer" className="text-primary hover:underline">
                pyannote/speaker-diarization-3.1
              </a>{" "}
              and{" "}
              <a href="https://huggingface.co/pyannote/segmentation-3.0" target="_blank" rel="noreferrer" className="text-primary hover:underline">
                segmentation-3.0
              </a>.
            </li>
            <li>Paste the token into Settings and restart the app once.</li>
          </ol>
        </>
      ),
      cta: { label: "Open Settings", nav: "settings" },
    },
    {
      icon: Mic,
      title: "Step 3 — Pick your audio devices",
      body: (
        <>
          <p>
            On the <strong>Record</strong> tab, choose your{" "}
            <strong>microphone</strong> and a <strong>System Audio
            (loopback)</strong> device so the app captures both your voice and
            the far end of the call.
          </p>
          <p className="mt-2">
            In a physical conference room with everyone on speakers, turn on{" "}
            <strong>Conference room mode</strong> — it captures only the mic and
            avoids a double-capture echo.
          </p>
          <p className="mt-2 text-muted-foreground">
            Your choices are saved by device name, so they survive reboots and
            USB re-plugs.
          </p>
        </>
      ),
      cta: { label: "Go to Record", nav: "record" },
    },
    {
      icon: CheckCircle2,
      title: "Step 4 — Record your first meeting",
      body: (
        <>
          <p>
            On the Record tab, either pick a meeting from{" "}
            <strong>Upcoming Meetings</strong> (from your Outlook calendar) or
            type a name, then click <strong>Start Recording</strong>.
          </p>
          <p className="mt-2">
            When you click <strong>Stop</strong>, the app automatically
            transcribes, identifies speakers, and extracts your summary, action
            items, and decisions — no extra clicks. The finished notes appear in{" "}
            <strong>Sessions</strong>.
          </p>
        </>
      ),
    },
    {
      icon: FileText,
      title: "Step 5 — Pick the template that matches the meeting",
      body: (
        <>
          <p>
            Before you record — or any time afterwards — set the{" "}
            <strong>Template</strong>. It decides what the summary keeps.
            Run a pricing call through a technical template and you get a
            faithful record of the architecture and no trace of the
            discount that was offered out loud.
          </p>
          <p className="mt-2">
            Eighteen ship with the app, grouped by the job you do:
          </p>
          <ul className="list-disc pl-5 mt-2 space-y-1">
            <li><strong>Pre-sales</strong> — Requirements Gathering, Design
              Review, Stakeholder Update…</li>
            <li><strong>Delivery</strong> — Delivery Kickoff, UAT &amp; Defect
              Triage, Go-Live Readiness, Hypercare Review…</li>
            <li><strong>Account management</strong> — Qualification Call,
              Pricing &amp; Commercial, Account Review / QBR,
              Sales-to-Delivery Handoff…</li>
          </ul>
          <p className="mt-2 text-muted-foreground">
            Picked the wrong one? Change it on the session and re-run
            Summarize. Nothing is lost.
          </p>
        </>
      ),
      cta: { label: "See the templates", nav: "settings" },
    },
    {
      icon: Users,
      title: "Step 6 — Tag the client, so it adds up",
      body: (
        <>
          <p>
            Set <strong>Client</strong> (and Project) on each session. It
            takes a second and it is what turns a pile of recordings into
            an account history.
          </p>
          <p className="mt-2">Once meetings are tagged you get:</p>
          <ul className="list-disc pl-5 mt-2 space-y-1">
            <li><strong>Engagements</strong> — one register per client of
              commitments, decisions, requirements and defects, rolled up
              across every meeting.</li>
            <li><strong>Insights</strong> — what is overdue, what was
              promised and never followed up.</li>
            <li><strong>Search that knows who you mean</strong> — ask about
              one client and get only their meetings.</li>
          </ul>
          <p className="mt-2 text-muted-foreground">
            Spell it the same way every time. A client name spelled two
            ways is two clients, and half your history goes to the one you
            are not looking at.
          </p>
        </>
      ),
    },
    {
      icon: MessageSquare,
      title: "Step 7 — Ask your AI assistant about it all",
      body: (
        <>
          <p>
            <strong>Settings → Templates &amp; Integrations → AI assistant
            access.</strong> Turn it on, pick your tool, click Set up. The
            app writes that tool&apos;s config for you.
          </p>
          <p className="mt-2">
            Then ask Claude Desktop, Claude Code, Cursor or VS Code things
            like <em>&quot;what do I still owe anyone?&quot;</em> or{" "}
            <em>&quot;what did we promise this client that we haven&apos;t
            delivered?&quot;</em> — answered from every meeting you have
            recorded, read-only.
          </p>
          <p className="mt-2">
            Two things that trip people up:{" "}
            <strong>each tool is set up separately</strong>, and after
            setting one up you must <strong>quit it completely</strong> and
            reopen — closing the window is not enough on Windows. The card
            tells you when an assistant last used the app, so you can see
            whether it worked instead of guessing.
          </p>
          <p className="mt-2 text-muted-foreground">
            Only works in tools running on this computer. claude.ai in a
            browser and Claude on your phone cannot reach it — your
            meetings never leave this machine.
          </p>
        </>
      ),
      cta: { label: "Set it up", nav: "settings" },
    },
    {
      icon: Sparkles,
      title: "Optional — power features",
      body: (
        <>
          <p>These are off by default. Turn on what fits how you work:</p>
          <ul className="list-disc pl-5 mt-2 space-y-1">
            <li><strong>Live Co-Pilot</strong> — real-time clarifying questions, risks, and follow-ups during a call.</li>
            <li><strong>Auto pre-meeting brief</strong> — a brief from your prior sessions, ready before each meeting.</li>
            <li><strong>Today / Daily Briefing</strong> — imports your M365 Copilot morning briefing into a dashboard.</li>
            <li><strong>Domain terminology</strong> — already seeded with CCaaS/cloud/sales vocab so transcripts spell your jargon right.</li>
            <li><strong>Diagnostics</strong> — Settings panel showing system health if anything misbehaves.</li>
          </ul>
          <p className="mt-2 text-muted-foreground">
            All in Settings. The Help tab has the full guide for each.
          </p>
        </>
      ),
      cta: { label: "Open Settings", nav: "settings" },
    },
  ];

  const step = steps[i];
  const isLast = i === steps.length - 1;
  const Icon = step.icon;

  const finish = () => {
    markDismissed();
    onClose();
  };

  const go = (nav: string) => {
    markDismissed();
    onNavigate(nav);
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) finish(); }}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Icon className="h-5 w-5 text-primary" />
            {step.title}
          </DialogTitle>
        </DialogHeader>

        <div className="text-sm leading-relaxed py-1 min-h-[180px]">
          {step.body}
        </div>

        {/* Step dots */}
        <div className="flex items-center justify-center gap-1.5 py-1">
          {steps.map((_, idx) => (
            <span
              key={idx}
              className={
                "h-1.5 rounded-full transition-all " +
                (idx === i ? "w-5 bg-primary" : "w-1.5 bg-muted-foreground/30")
              }
            />
          ))}
        </div>

        <DialogFooter className="flex-row items-center justify-between gap-2 sm:justify-between">
          <Button variant="ghost" size="sm" onClick={finish}>
            Skip setup
          </Button>
          <div className="flex items-center gap-2">
            {step.cta && (
              <Button variant="outline" size="sm" onClick={() => go(step.cta!.nav)}>
                {step.cta.label}
              </Button>
            )}
            {i > 0 && (
              <Button variant="ghost" size="sm" onClick={() => setI(i - 1)}>
                <ArrowLeft className="h-4 w-4 mr-1" /> Back
              </Button>
            )}
            {isLast ? (
              <Button size="sm" onClick={finish}>
                <CheckCircle2 className="h-4 w-4 mr-1.5" /> Done
              </Button>
            ) : (
              <Button size="sm" onClick={() => setI(i + 1)}>
                Next <ArrowRight className="h-4 w-4 ml-1" />
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
