"use client";

import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";

type Section = { id: string; title: string; content: React.ReactNode };

const SECTIONS: Section[] = [
  {
    id: "getting-started",
    title: "Getting Started",
    content: (
      <>
        <p>
          Meeting Recorder captures meetings, transcribes them with Whisper (on your machine),
          identifies speakers with Pyannote (on your machine), and uses Claude to extract
          summaries, action items, requirements, and decisions. All audio and transcripts stay
          local — only Claude calls leave your machine.
        </p>

        <p className="font-medium mt-4">First-run setup — you need two tokens:</p>

        <div className="rounded-md border bg-muted/40 p-4 space-y-3 text-sm">
          <div>
            <div className="font-medium">1. Anthropic API Key (powers AI extraction)</div>
            <ol className="list-decimal pl-5 mt-1 space-y-0.5 text-muted-foreground">
              <li>Sign up at <a href="https://console.anthropic.com" target="_blank" rel="noreferrer" className="text-primary hover:underline">console.anthropic.com</a></li>
              <li>Billing → Buy credits → add $5-10 (~$0.05 per meeting on Haiku)</li>
              <li><a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer" className="text-primary hover:underline">Settings → API Keys</a> → Create Key</li>
              <li>Copy the <code className="text-[11px]">sk-ant-api03-...</code> value</li>
            </ol>
          </div>
          <div>
            <div className="font-medium">2. HuggingFace Token (powers speaker identification)</div>
            <ol className="list-decimal pl-5 mt-1 space-y-0.5 text-muted-foreground">
              <li>Sign up at <a href="https://huggingface.co/join" target="_blank" rel="noreferrer" className="text-primary hover:underline">huggingface.co/join</a> (free)</li>
              <li><a href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer" className="text-primary hover:underline">Settings → Access Tokens</a> → Create new token → Type: <strong>Read</strong></li>
              <li>Copy the <code className="text-[11px]">hf_...</code> value</li>
              <li>
                <strong className="text-foreground">Critical:</strong> accept model terms on both pyannote pages, otherwise speaker diarization will 403:
                <ul className="list-disc pl-5 mt-0.5">
                  <li><a href="https://huggingface.co/pyannote/speaker-diarization-3.1" target="_blank" rel="noreferrer" className="text-primary hover:underline">pyannote/speaker-diarization-3.1</a></li>
                  <li><a href="https://huggingface.co/pyannote/segmentation-3.0" target="_blank" rel="noreferrer" className="text-primary hover:underline">pyannote/segmentation-3.0</a></li>
                </ul>
              </li>
            </ol>
          </div>
          <div>
            <div className="font-medium">3. Paste both into Settings</div>
            <p className="text-muted-foreground pl-5">
              Click Save. Restart the app so the backend picks up the new keys and downloads the
              pyannote models into cache (~200 MB, one-time, on first Process).
            </p>
          </div>
        </div>

        <p className="mt-4 font-medium">Then:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Select your microphone and a loopback device for System Audio (Record tab)</li>
          <li>Pick a Whisper model (see &quot;Whisper Models&quot; section below)</li>
          <li>(Optional) Email recipient, auto-process toggles, retention policy</li>
          <li>(Optional) Settings → Transcription Acceleration → enable GPU if you have an NVIDIA / AMD / Intel GPU (3-10× faster transcription)</li>
        </ul>

        <Tip>
          Requires <strong>Classic Outlook</strong> for calendar + email. New Outlook blocks COM
          automation.
        </Tip>
        <Tip>
          The tokens are stored in <code>%LOCALAPPDATA%\MeetingRecorder\config.env</code> on
          this machine only — they never roam to other laptops and never leave to the network
          except when you actually make an AI extraction call.
        </Tip>
      </>
    ),
  },
  {
    id: "recording",
    title: "Recording",
    content: (
      <>
        <ol className="list-decimal pl-5 space-y-2">
          <li>Pick a meeting from <strong>Upcoming Meetings</strong> or type a name manually.</li>
          <li>Tag it with Client and Project (autocompletes from previously-used tags).</li>
          <li>Pick a Template: General, Requirements Gathering, Design Review, Sprint Planning, Stakeholder Update.</li>
          <li>Select your mic + System Audio loopback device.</li>
          <li>Click <strong>Start Recording</strong> (bottom of the Audio Devices card).</li>
          <li>When done, <strong>Stop</strong>. A &quot;Just Recorded&quot; card appears with an <strong>Open Session</strong> button.</li>
        </ol>
        <Tip>
          <strong>Audio device selection is persistent.</strong> Your mic + loopback choices are
          saved by device name (not index) so they survive reboots, USB re-plugs, and index
          shuffling across audio APIs.
        </Tip>
        <Tip>
          <strong>Automatic host-API fallback.</strong> If your selected mic refuses to open under
          WASAPI (common with webcams like Insta360 when another app is using them), the backend
          silently retries the same physical device under MME → DirectSound → WDM-KS before giving
          up. The error toast only appears if every host API fails.
        </Tip>
        <Warn>
          If System Audio is &quot;Skip&quot;, only your voice is captured — not the other participants.
          Enable Stereo Mix in Windows Sound settings or install VB-Cable.
        </Warn>
      </>
    ),
  },
  {
    id: "session-detail",
    title: "Session Detail",
    content: (
      <>
        <p>
          Clicking any session anywhere in the app (Sessions list, Follow-Ups, Decisions, Search,
          Clients) opens its <strong>Session Detail</strong> dialog with tabs:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Overview</strong> — audio player (scrub/replay), edit meeting name, template, client, project tags, and run any AI extraction.</li>
          <li><strong>Transcript</strong> — full speaker-labeled timestamped transcript.</li>
          <li><strong>Speakers</strong> — who was detected, how many segments each spoke. <strong>Click any speaker name to rename them</strong> — the new label flows into the transcript and all future AI outputs.</li>
          <li><strong>Summary</strong> — AI summary tailored to the template.</li>
          <li><strong>Actions</strong> — extracted action items (owner + task + due date).</li>
          <li><strong>Decisions</strong> — ADR-style decision log.</li>
          <li><strong>Requirements</strong> — FR/NFR tables.</li>
        </ul>
        <Tip>
          Tabs are disabled if their content hasn&apos;t been generated yet. Go to the Overview tab and
          click the matching AI Action button to generate it.
        </Tip>
        <Tip>
          <strong>Playback:</strong> the Overview tab shows an inline audio player so you can scrub
          through the recording — useful for verifying who said what, or spotting details the
          transcript missed.
        </Tip>
      </>
    ),
  },
  {
    id: "ai-actions",
    title: "AI Extractions",
    content: (
      <>
        <p>
          In the Session Detail <strong>Overview</strong> tab:
        </p>
        <div className="grid grid-cols-1 gap-3 mt-3">
          <BtnDesc emoji="⚙" name="Process" desc="Transcribes audio with Whisper + identifies speakers with Pyannote." />
          <BtnDesc emoji="✨" name="Summarize" desc="Template-aware summary via Claude." />
          <BtnDesc emoji="📋" name="Action Items" desc="Owner, task, due date, decisions, open questions." />
          <BtnDesc emoji="🎯" name="Decisions" desc="ADR log: Decided, Rationale, Alternatives, Owner, Impact." />
          <BtnDesc emoji="📝" name="Requirements" desc="FR/NFR tables with priority and owner." />
        </div>
        <Tip>
          Status icons in session rows (🎤 ⚙ ✨ 📋 🎯 📝) show at a glance what&apos;s been extracted.
          Hover any icon for a tooltip.
        </Tip>
      </>
    ),
  },
  {
    id: "notes",
    title: "Session Notes",
    content: (
      <>
        <p>
          Every session has a <strong>Notes</strong> tab (next to Overview in the session dialog).
          Use it for everything the transcript can&apos;t capture on its own:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>Off-call context — what was said in the hallway after the call ended, what someone
            told you in a private chat, decisions that happened over email.</li>
          <li>Reminders to yourself — &quot;call Ricoh Friday about pricing&quot;, &quot;don&apos;t forget
            the SOW needs legal review before sending.&quot;</li>
          <li>Your own follow-ups — commitments you made that weren&apos;t verbalized on-mic.</li>
          <li>Corrections — if the transcript mis-heard a proper noun or decision, note the truth
            and Claude will weight it over the transcript.</li>
        </ul>
        <p className="mt-3">
          Notes are fed into every AI extraction — summary, action items, decisions, requirements.
          Claude treats them as <strong>authoritative context</strong> since you know things the audio
          doesn&apos;t. Re-run any extraction after editing notes to pick up the update.
        </p>
        <Tip>
          You can edit notes <strong>before or after</strong> processing. If you edit after, hit
          <strong> Re-process</strong> (or just re-run the specific extraction) to regenerate with
          the new context.
        </Tip>
      </>
    ),
  },
  {
    id: "knowledge",
    title: "Knowledge Base",
    content: (
      <>
        <p>Every recording becomes part of a searchable knowledge base:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Sessions</strong> — full history, filter by name/client/project, bulk-process, click any row to open. Hover a row to see a pencil icon for inline rename.</li>
          <li><strong>Follow-Ups</strong> — action items grouped by (meeting, owner). Five tasks for one person from one meeting = one expandable card. Click the external-link icon to jump to the source session&apos;s Actions tab.</li>
          <li><strong>Decisions</strong> — ADR log, click a decision to see full context. &quot;Open Meeting →&quot; to jump in.</li>
          <li><strong>Search</strong> — type a phrase, it searches every transcript + summary + extraction. Click a result to open that session.</li>
          <li><strong>Clients</strong> — create clients, nest projects inside each one, tag meetings manually or via <strong>AI Suggest</strong>. Each client has a dashboard with stats, meetings, and a chip row for drilling into individual projects.</li>
          <li><strong>Prep Brief</strong> — filter by client and (optionally) project. The project dropdown is scoped to the selected client, so you can never cross-contaminate contexts. Generates a pre-meeting brief with recent context, open items, risks, and suggested discussion points.</li>
        </ul>
      </>
    ),
  },
  {
    id: "clients",
    title: "Clients & Projects",
    content: (
      <>
        <p>
          Clients are the top-level organizational unit. Projects live <strong>inside</strong>{" "}
          clients — every project belongs to exactly one client. Tag meetings to unlock the Client
          Dashboard, filtered Follow-Ups, and Prep Briefs.
        </p>
        <p><strong>Client workflow:</strong></p>
        <ol className="list-decimal pl-5 space-y-2">
          <li>Clients tab → click <strong>+</strong> (top of the client list) to create a new client.</li>
          <li>You&apos;ll be prompted to tag meetings. Select the ones that belong, click <strong>Apply Tag</strong>.</li>
          <li>Use <strong>AI Suggest</strong> to let Claude scan your history and propose which meetings likely belong to this client. High-confidence ones are pre-selected.</li>
          <li>Use <strong>Rename</strong> to rename the client across every tagged meeting at once.</li>
        </ol>
        <p className="mt-3"><strong>Projects (nested under the selected client):</strong></p>
        <ol className="list-decimal pl-5 space-y-2">
          <li>Pick a client, then click <strong>+ New Project</strong> in the Projects card.</li>
          <li>Projects appear as chips — click one to filter the meetings list and stat cards to just that project.</li>
          <li>Click <strong>Tag to {"{project}"}</strong> to assign meetings. The dialog only shows meetings that already belong to the current client — projects can never cross client boundaries.</li>
          <li>Click <strong>Rename Project</strong> to rename within just this client (a project with the same name under a different client is unaffected).</li>
        </ol>
        <Tip>
          You can also tag Client + Project directly in the Record view, in any session&apos;s
          Overview tab, or when stopping a recording — all three converge on the same session
          metadata.
        </Tip>
      </>
    ),
  },
  {
    id: "workflow",
    title: "Workflow Automation",
    content: (
      <>
        <p>In <strong>Settings &gt; Workflow</strong>:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Auto-process after stop</strong> — transcribe → speaker-diarize → summary → action items → decisions → requirements all run automatically when you stop recording. Each stage is independent; a failure on one (rate-limit on Claude, etc.) doesn&apos;t stop the others.</li>
          <li><strong>Auto-draft follow-up email</strong> — after processing, Claude generates a per-attendee follow-up email with their specific action items and meeting context, then creates Outlook drafts (one per person) in your Drafts folder. You review and hit Send. Requires Classic Outlook.</li>
          <li><strong>Launch on Windows startup</strong> — adds a shortcut to the Startup folder (PowerShell is spawned invisibly — no black console flash since v2.0.12).</li>
        </ul>
        <Tip>
          You can also trigger follow-up drafts manually from the Session Overview tab — click
          <strong> Draft follow-up emails</strong>. This is the best path if you want to review
          action items before Claude drafts the emails.
        </Tip>
      </>
    ),
  },
  {
    id: "notifications",
    title: "Notifications & Reliability",
    content: (
      <>
        <p className="font-medium">Unprocessed session alerts</p>
        <p>
          If you record a meeting but don&apos;t process it (or auto-process is off), the sidebar
          shows a badge with the unprocessed count. On the first time a new unprocessed session
          appears, Windows fires a toast notification so you don&apos;t forget — click it to jump
          straight to the Sessions list.
        </p>
        <p className="font-medium mt-4">Crash recovery (v2.0.10+)</p>
        <p>
          If the backend dies mid-stop (power loss, force-quit, OS kill), the next backend start
          scans for orphan <code>_recording_&lt;ID&gt;.wav</code> / <code>_loopback_&lt;ID&gt;.wav</code>
          temp files and merges them into proper sessions labeled &quot;Recovered Session &lt;ID&gt;&quot;.
          Transcribe them like any other session. The audio is intact.
        </p>
        <p className="font-medium mt-4">Streaming merge (v2.0.10+)</p>
        <p>
          The stop handler streams mic + loopback through a 10-second-block merge pipeline instead of
          loading both WAVs fully into RAM. Peak memory is ~10 MB regardless of recording length — a
          3-hour meeting is no more memory-intensive than a 3-minute one. The access-violation crashes
          on long recordings are gone.
        </p>
        <p className="font-medium mt-4">GPU transcription (v2.0.11+)</p>
        <p>
          When CUDA torch is installed and a CUDA-capable GPU is detected, <code>faster-whisper</code>
          runs on GPU with <code>float16</code> precision — 36-minute meetings transcribe in under a
          minute on an RTX 2070 SUPER. CPU-only machines are unchanged: <code>int8</code> on CPU, same
          behaviour as ever.
        </p>
        <Tip>
          Check <code>%LOCALAPPDATA%\MeetingRecorder\backend.log</code> for the device the backend
          actually picked — the line reads <code>faster-whisper model loaded on cuda (float16)</code>
          or <code>... on cpu (int8)</code>.
        </Tip>
      </>
    ),
  },
  {
    id: "retention",
    title: "Retention",
    content: (
      <>
        <p>
          Audio WAV files are bulky (~230 MB per hour). Retention auto-deletes old audio while
          keeping transcripts, summaries, and everything else forever.
        </p>
        <p>Two thresholds in <strong>Settings &gt; Retention</strong>:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Processed audio</strong> — delete N days after transcription (default 7).</li>
          <li><strong>Unprocessed audio</strong> — delete M days after recording (default 30).</li>
        </ul>
        <Tip>
          Click <strong>Clean up now</strong> to run retention immediately.
          Transcripts / summaries / action items / decisions / requirements are <strong>never</strong> deleted.
        </Tip>
      </>
    ),
  },
  {
    id: "whisper-models",
    title: "Whisper Models",
    content: (
      <>
        <p>
          Whisper transcribes your audio entirely on your machine — no cloud call, no data leaves.
          Picking a model is a tradeoff between speed and transcript quality. Change in{" "}
          <strong>Settings → AI Models → Whisper Model</strong>.
        </p>
        <table className="w-full text-xs mt-3 border-collapse">
          <thead>
            <tr className="border-b text-left">
              <th className="py-1.5 pr-3">Model</th>
              <th className="py-1.5 pr-3">Size</th>
              <th className="py-1.5 pr-3">Speed (30-min on CPU)</th>
              <th className="py-1.5">Quality</th>
            </tr>
          </thead>
          <tbody className="text-muted-foreground">
            <tr className="border-b">
              <td className="py-1.5 pr-3"><code>tiny</code></td>
              <td className="py-1.5 pr-3">~39 MB</td>
              <td className="py-1.5 pr-3">~30s</td>
              <td className="py-1.5">Rough — misses words, bad at names</td>
            </tr>
            <tr className="border-b">
              <td className="py-1.5 pr-3"><code>base</code></td>
              <td className="py-1.5 pr-3">~74 MB</td>
              <td className="py-1.5 pr-3">~1 min</td>
              <td className="py-1.5">Fair — noticeable errors</td>
            </tr>
            <tr className="border-b bg-primary/5">
              <td className="py-1.5 pr-3"><code>small</code> <strong className="text-primary">(default)</strong></td>
              <td className="py-1.5 pr-3">~244 MB</td>
              <td className="py-1.5 pr-3">~2-3 min</td>
              <td className="py-1.5 text-foreground">Good — the sweet spot for most meetings</td>
            </tr>
            <tr className="border-b">
              <td className="py-1.5 pr-3"><code>medium</code></td>
              <td className="py-1.5 pr-3">~769 MB</td>
              <td className="py-1.5 pr-3">~5-7 min</td>
              <td className="py-1.5">Very good — noisy rooms, phone audio, light accents</td>
            </tr>
            <tr>
              <td className="py-1.5 pr-3"><code>large</code></td>
              <td className="py-1.5 pr-3">~1.5 GB</td>
              <td className="py-1.5 pr-3">~10-15 min</td>
              <td className="py-1.5">Best — nails proper nouns, heavy accents, jargon</td>
            </tr>
          </tbody>
        </table>
        <Tip>
          <strong>With GPU acceleration enabled</strong>, everything above is roughly 10× faster.
          <code>large</code> becomes ~1-2 min for a 30-min meeting, which makes it essentially
          free to use — jump straight to <code>large</code> if you&apos;ve enabled CUDA or DirectML.
        </Tip>
        <p className="mt-3">
          Models are downloaded on first use into <code>%USERPROFILE%\.cache\huggingface</code>,
          which is one-time per model. Switching models is just a Settings change + app restart.
        </p>
      </>
    ),
  },
  {
    id: "cost",
    title: "Cost",
    content: (
      <>
        <p>
          Default Claude model is <strong>Haiku 4.5</strong> (~$1/M input, $5/M output). A full
          extraction pipeline for a typical meeting costs under $0.05. Hundreds of meetings per
          year for a few dollars.
        </p>
        <Tip>
          For dense design reviews where nuance matters, switch to Sonnet 4.5 in Settings. ~4× the
          cost, but higher quality. Switch back for routine meetings.
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
              System Audio wasn&apos;t a loopback device. Enable Stereo Mix in Windows Sound settings,
              or install VB-Cable. Then pick it as System Audio in the Record view.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">Calendar shows no meetings</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Requires Classic Outlook (New Outlook doesn&apos;t support COM). Switch in Outlook settings.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">Models failed to load</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Invalid HuggingFace token or un-accepted pyannote terms. Visit
              huggingface.co/pyannote/speaker-diarization-3.1 and huggingface.co/pyannote/segmentation-3.0,
              click &quot;Agree and access repository&quot;, then restart.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">App is slow to start</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              The backend starts in ~2 seconds; AI models load lazily on first use (Whisper +
              Pyannote each take a few seconds). Calendar + audio device enumeration is pre-warmed
              in background threads at launch and cached for 5 min — first UI render should be
              near-instant after the first visit. Check <code>%APPDATA%\MeetingRecorder\backend.log</code>
              if it still feels slow.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">Mic fails to open</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              The backend automatically retries across sample rates AND host APIs (WASAPI → MME →
              DirectSound → WDM-KS). If every attempt fails the device is truly busy or unplugged
              — close other apps using it (Teams, Zoom, Windows Camera) or pick a different mic.
              Audio device choices persist across launches so you only select once.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">Stop button says &quot;fetch failed&quot;</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Every stream close/terminate call is now wrapped with a 2-3s timeout. If a driver
              deadlocks the backend abandons it and returns anyway. If you still hit this, the
              backend log will show which operation exceeded its timeout.
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
          <div className="space-y-3 text-sm leading-relaxed">{section.content}</div>
        </CardContent>
      </Card>
    </div>
  );
}

function Tip({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-primary/30 bg-accent/50 px-4 py-3 my-3 text-sm">
      <span className="font-medium text-primary">💡 Tip </span>{children}
    </div>
  );
}

function Warn({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40 px-4 py-3 my-3 text-sm text-amber-900 dark:text-amber-200">
      <span className="font-medium">⚠ Note </span>{children}
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
