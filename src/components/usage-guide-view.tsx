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
          identifies speakers with Pyannote (on your machine), and sends the transcript to an
          LLM (Claude by default, but also OpenRouter / Ollama / any OpenAI-compatible endpoint)
          to extract summaries, action items, requirements, and decisions. All audio and
          transcripts stay local — only the LLM call leaves your machine, and with Ollama even
          that stays local.
        </p>

        <p className="font-medium mt-4">First-run setup:</p>

        <div className="rounded-md border bg-muted/40 p-4 space-y-3 text-sm">
          <div>
            <div className="font-medium">1. HuggingFace Token (required — powers speaker identification)</div>
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
            <div className="font-medium">2. Pick an AI Provider (for summaries + extractions)</div>
            <p className="text-muted-foreground pl-5 mb-2">
              In Settings you&apos;ll see a provider dropdown. Pick one and fill in just the fields it
              shows — the others disappear.
            </p>
            <ul className="list-disc pl-5 space-y-1 text-muted-foreground">
              <li><strong>Anthropic</strong> (paid) — best quality. Get a key at{" "}
                <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer" className="text-primary hover:underline">console.anthropic.com</a> +
                add ~$5-10 credit. Paste into &quot;Anthropic API Key&quot;.</li>
              <li><strong>OpenRouter</strong> (free tier!) — Llama 3.3 70B, Gemini 2.0 Flash,
                Qwen 2.5 72B, DeepSeek R1, and more, all free (rate-limited to ~50 requests/day).
                Get a key at{" "}
                <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noreferrer" className="text-primary hover:underline">openrouter.ai/settings/keys</a>.</li>
              <li><strong>Ollama</strong> (local, free, offline) — install{" "}
                <a href="https://ollama.com/download" target="_blank" rel="noreferrer" className="text-primary hover:underline">ollama.com</a>,
                run <code className="text-[11px]">ollama pull llama3.1</code> (or any model), pick
                Ollama in the dropdown. No API key needed, nothing leaves your machine.</li>
              <li><strong>Custom OpenAI-compatible</strong> — any LM Studio / vLLM / Groq /
                Together / LocalAI / self-hosted endpoint.</li>
            </ul>
          </div>
          <div>
            <div className="font-medium">3. Click Save</div>
            <p className="text-muted-foreground pl-5">
              Restart the app so the backend picks up the new provider + key and downloads the
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
          API keys (HuggingFace, Anthropic, OpenRouter) are stored in your OS&apos;s native
          credential vault — Windows Credential Manager on Windows, Keychain on macOS — not in
          plaintext on disk. Other settings live in{" "}
          <code>%LOCALAPPDATA%\MeetingRecorder\config.env</code>{" "}
          (Win) or{" "}
          <code>~/Library/Application Support/MeetingRecorder/config.env</code>{" "}
          (Mac). Existing keys from older versions migrate into the keychain automatically the
          first time the new build reads them.
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
          <li>Pick a meeting from <strong>Upcoming Meetings</strong> (shows the next 7 days from
            your Classic Outlook calendar) or type a name manually.</li>
          <li>Tag with Client, then Project. <strong>Project autocomplete is scoped to the
            selected client</strong> — you&apos;ll only see projects that were previously tagged
            under that client, so you can&apos;t accidentally cross-tag. Changing the Client clears
            the Project field.</li>
          <li>Pick a Template (see <strong>Summary Templates</strong> section — defaults include
            General, Requirements Gathering, Design Review, Sprint Planning, Stakeholder Update,
            plus any custom templates you&apos;ve added).</li>
          <li>Select your mic + System Audio loopback device.</li>
          <li>Click <strong>Start Recording</strong> (bottom of the Audio Devices card).</li>
          <li>When done, <strong>Stop</strong>. A &quot;Just Recorded&quot; card appears with an <strong>Open Session</strong> button.</li>
        </ol>
        <Tip>
          <strong>Calendar list survives nav switches.</strong> Clicking between Record / Sessions /
          Clients won&apos;t drop the loaded meetings. Silent auto-refresh kicks in when the window
          regains focus — useful if you just accepted a meeting in Outlook.
        </Tip>
        <Tip>
          <strong>Client auto-tagging from attendees.</strong> Clicking <strong>Use</strong> on a
          calendar meeting will pre-fill the Client field when the meeting&apos;s attendee email
          domains match an existing client you&apos;ve tagged before. It learns from your own
          history — no configuration. First time you tag meetings with <code>@acme.com</code>
          attendees to &quot;Acme,&quot; the next one auto-fills. Ties or new domains → leaves Client
          blank, you pick manually like today.
        </Tip>
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
        <Tip>
          <strong>Auto-record from calendar</strong> (toggle in the Upcoming Meetings card
          header). When on, the app watches your calendar and starts a recording automatically
          at each meeting&apos;s scheduled start time, with the meeting name, attendees, and
          scheduled end already wired up — so the auto-stop watchdog catches overrun + silence
          for free. Filters out all-day events and only fires for events that include a
          Teams / Zoom / Meet / Webex / GoToMeeting / BlueJeans / Whereby link. Manual
          recordings always win: while you&apos;re already recording (manually or auto), no new
          auto-start fires, and a calendar meeting that opens during a manual recording is
          marked handled so it won&apos;t pounce the moment you hit Stop. The toggle state
          persists across restarts.
        </Tip>
        <Tip>
          <strong>Screenshots during a recording.</strong> While recording,
          a <strong>Screenshot</strong> button sits next to Stop. Click it
          to capture your screen — on a multi-monitor setup a small
          chooser asks which display. Each shot is saved with the meeting
          and, on processing, sent to Claude as visual context so the
          summary can reference what was on screen (diagrams, dashboards,
          slides). Review them anytime on the session&apos;s new
          <strong>Screenshots</strong> tab. macOS asks for Screen
          Recording permission the first time.
        </Tip>
        <Tip>
          <strong>Click a calendar meeting to expand it.</strong> The
          chevron on each Upcoming Meeting opens its attendees, the
          invite&apos;s agenda/body, and a one-click <strong>Join
          meeting</strong> button (the join link is auto-detected from the
          invite). The agenda is also fed into the AI <strong>Brief</strong>
          so it&apos;s tailored to what that specific meeting is about.
          Details load on demand to keep the list fast.
        </Tip>
        <Tip>
          <strong>Never auto-record a meeting.</strong> Each calendar tile
          has a <strong>No auto</strong> toggle. Flag a meeting (e.g. a
          recurring 1:1) and it&apos;s permanently skipped by auto-record,
          including every future occurrence. Survives restarts.
        </Tip>
        <Tip>
          <strong>Conference room mode</strong> (toggle below the device selectors): turns off
          system-audio loopback entirely and captures only the mic. Use it when you&apos;re in a
          physical conference room with the laptop on the table, the remote party is on
          speakers, and your mic is picking everyone up acoustically. Avoids the double-capture
          (mic + loopback both recording the same far-end audio) that produces a 1–2s echo on
          playback in that setup. Live transcript labels segments as <strong>Room</strong>
          rather than <strong>You</strong>, and the post-stop pyannote pass diarizes everyone
          into proper SPEAKER_00 / SPEAKER_01 / … labels you can rename afterwards. The toggle
          persists across sessions in localStorage.
        </Tip>
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
          <li><strong>Speakers</strong> — who was detected, how many segments each spoke. <strong>Click any speaker name to rename them</strong> — the new label flows into the transcript and all future AI outputs. Speakers who introduce themselves (&quot;Hi, I&apos;m Sarah&quot;) or are called on by name are now auto-named and their voiceprint saved automatically, so they&apos;re recognized in future meetings with no manual labeling.</li>
          <li><strong>Screenshots</strong> — any screenshots you captured during the meeting; click a thumbnail to enlarge. These are also sent to Claude as visual context for the summary.</li>
          <li><strong>Summary</strong> — AI summary tailored to the template.</li>
          <li><strong>Actions</strong> — extracted action items (owner + task + due date).</li>
          <li><strong>Decisions</strong> — ADR-style decision log.</li>
          <li><strong>Requirements</strong> — FR/NFR tables.</li>
        </ul>
        <Tip>
          <strong>Auto-process is on by default.</strong> When you Stop a
          recording the full pipeline (transcribe → speakers → summary →
          actions → decisions → requirements → commitments) now runs
          automatically — no need to open each session and click Process.
          Toggle it under Settings → &quot;Auto-process after recording
          stops&quot; if you&apos;d rather do it manually.
        </Tip>
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
          <li><strong>Sessions</strong> — full history, filter by name/client/project, bulk-process, click any row to open. Hover a row to see a pencil icon for inline rename. Top-right has two utilities: <strong>Open Recordings Folder</strong> (jumps to <code>%LOCALAPPDATA%\MeetingRecorder\recordings</code> in Explorer) and <strong>Load Session</strong> (imports an external <code>.wav</code> / <code>.mp3</code> / <code>.m4a</code> / <code>.flac</code> file — it&apos;s copied into the recordings folder and becomes a session you can transcribe like any other).</li>
          <li><strong>Follow-Ups</strong> — action items grouped by (meeting, owner). Five tasks for one person from one meeting = one expandable card. Click the external-link icon to jump to the source session&apos;s Actions tab. Each item has a <strong>checkbox</strong> to mark it done — state persists in a per-session sidecar so it survives re-processing.</li>
          <li><strong>Commitments</strong> — see the dedicated <strong>&quot;Tracking Commitments, Follow-Ups &amp; Decisions&quot;</strong> section below. Cross-meeting tracker for things people promised, with status, due dates, and overdue alerts.</li>
          <li><strong>Decisions</strong> — ADR log, click a decision to see full context. &quot;Open Meeting →&quot; to jump in. Status can be set to <strong>active</strong> (default), <strong>implemented</strong>, or <strong>superseded</strong> directly from the row — the state shows in the badge alongside.</li>
          <li><strong>Search</strong> — type a phrase, it searches every transcript + summary + extraction. Click a result to open that session.</li>
          <li><strong>Clients</strong> — create clients, nest projects inside each one, tag meetings manually or via <strong>AI Suggest</strong>. Each client has a dashboard with stats, meetings, and a chip row for drilling into individual projects.</li>
          <li><strong>Prep Brief</strong> — filter by client and (optionally) project. The project dropdown is scoped to the selected client, so you can never cross-contaminate contexts. Generates a pre-meeting brief with recent context, open items, risks, and suggested discussion points.</li>
        </ul>
      </>
    ),
  },
  {
    id: "tracking-items",
    title: "Tracking Commitments, Follow-Ups & Decisions",
    content: (
      <>
        <p>
          Three sidebar sections give you cross-meeting views over the items Claude extracts
          when you process a recording. They&apos;re related but distinct — pick the right
          tool for the job:
        </p>
        <div className="grid grid-cols-1 gap-3 mt-3">
          <div className="border rounded-md p-3 space-y-2">
            <p className="font-medium">Follow-Ups — &quot;what action items haven&apos;t I closed?&quot;</p>
            <p className="text-sm text-muted-foreground">
              Action items extracted from each session, grouped by (meeting, owner). Best for
              the personal-task view: open the section, scan what&apos;s on your plate, check
              boxes as you finish each item. State persists per item in a sidecar JSON
              (<code>session_&lt;id&gt;.item_status.json</code>) so it survives re-processing
              of the parent session — re-running Action Items won&apos;t un-check items
              you&apos;ve already marked done.
            </p>
            <p className="text-sm text-muted-foreground">
              <strong>Per-item state:</strong> done / not done (a single checkbox).
            </p>
          </div>
          <div className="border rounded-md p-3 space-y-2">
            <p className="font-medium">Commitments — &quot;who promised me what and is it overdue?&quot;</p>
            <p className="text-sm text-muted-foreground">
              A richer cross-meeting tracker for things people promised on-mic with the
              metadata you need to chase them: WHO promised (with inferred email), WHAT
              (paraphrased + verbatim quote), source session + timestamp, DUE date resolved
              from natural-language phrases (&quot;by Friday&quot;, &quot;end of next week&quot;), SIDE
              (internal vs customer), and a status state machine.
            </p>
            <p className="text-sm text-muted-foreground">
              <strong>Per-item state:</strong> active (default — includes awaiting + overdue),
              awaiting (in-flight, before due date), overdue (past due, still awaiting),
              delivered (you marked it done), dismissed (you decided it&apos;s no longer
              relevant). Filter the view by any of those plus &quot;all,&quot; and by side
              (what they owe me vs what I owe them).
            </p>
            <p className="text-sm text-muted-foreground">
              <strong>Updating:</strong> open the Commitments tab, find the row, click the
              status badge to flip it. Optimistic UI — the change is reflected immediately
              and saved to the sidecar JSON (<code>session_&lt;id&gt;.commitments.json</code>)
              in the background.
            </p>
            <p className="text-sm text-muted-foreground italic">
              Commitments are a <em>superset</em> of action items. Claude re-runs a richer
              extraction over the raw transcript when you process a session, because the
              standard Action Items extraction throws away the verbatim quote and inferred
              due date that the tracker uniquely needs.
            </p>
          </div>
          <div className="border rounded-md p-3 space-y-2">
            <p className="font-medium">Decisions — &quot;what did we agree to, and is it still the plan?&quot;</p>
            <p className="text-sm text-muted-foreground">
              ADR-style decision log. Each entry has Decided / Rationale / Alternatives /
              Owner / Impact, extracted by Claude during processing. Cross-meeting view in
              the sidebar; per-session view in the Decisions tab of the session dialog.
            </p>
            <p className="text-sm text-muted-foreground">
              <strong>Per-item state:</strong> active (default), implemented, superseded.
              &quot;Superseded&quot; is the right call when a later decision overrules an earlier
              one — keeps the audit trail without cluttering &quot;current plan&quot; views.
            </p>
            <p className="text-sm text-muted-foreground">
              <strong>Updating:</strong> click the status badge on the row to cycle through
              the three options. Like commitments, stored in a per-session sidecar
              (<code>session_&lt;id&gt;.item_status.json</code>) so re-processing the session
              doesn&apos;t wipe your state.
            </p>
          </div>
        </div>
        <Tip>
          <strong>State survives sync.</strong> All three state sidecars
          (<code>*.item_status.json</code> and <code>*.commitments.json</code>) live in the
          recordings folder, so when you point the recordings folder at OneDrive / iCloud,
          your &quot;Mike still owes me the SOW&quot; tracking and your &quot;item X done&quot;
          checkboxes follow you across devices automatically. See Sync Across Devices.
        </Tip>
        <Tip>
          <strong>Status updates don&apos;t trigger re-processing.</strong> Marking a follow-up
          done or flipping a decision to &quot;superseded&quot; only updates the sidecar — it
          doesn&apos;t re-run Whisper / Claude. Cheap, instant, no Claude tokens burned.
        </Tip>
        <Warn>
          The state is keyed by a stable hash of the item&apos;s text. If you edit the
          underlying markdown that Claude wrote (in the session detail Actions / Decisions
          tab) and the text changes substantively, the hash changes and the &quot;done&quot;
          state for the old text won&apos;t map to the new one. Re-check the box after large
          edits.
        </Warn>
      </>
    ),
  },
  {
    id: "engagements",
    title: "Engagements (per-client register + Excel export)",
    content: (
      <>
        <p>
          The <strong>Engagements</strong> tab rolls every meeting for one client up
          into a single living register, and exports it to a hand-editable Excel
          workbook. Where Follow-Ups / Decisions are per-item cross-meeting views,
          this is the <em>engagement-level</em> picture: &quot;across every ACME
          call, what are all the requirements, decisions, action items, and open
          questions — deduped, with which meeting each came from?&quot;
        </p>
        <p className="font-medium mt-3">Using it:</p>
        <ol className="list-decimal pl-5 space-y-1">
          <li>Pick a <strong>client</strong> (and optionally a <strong>project</strong>) from the dropdowns.</li>
          <li>
            The register renders: Requirements / Decisions / Action Items / Open
            Questions, each item showing status, a &quot;from notes&quot; marker when it
            came from your session notes rather than the transcript, and provenance
            (how many meetings, last seen, which sessions).
          </li>
          <li>
            Click <strong>Export to Excel</strong> — a workbook is written to the
            client&apos;s export folder (or the recordings folder if none is set)
            and the path is shown in a toast.
          </li>
        </ol>
        <p className="text-sm text-muted-foreground mt-3">
          The workbook has an Overview sheet, one sheet per record type, and a
          <strong> Changes since last export</strong> sheet. It&apos;s designed to be
          hand-edited: every record sheet has two human-owned columns —
          <strong> Status</strong> and <strong>Notes</strong> — that are <em>never</em>
          overwritten. Re-export after the next meeting and it regenerates the same
          file in place, carrying your Status/Notes forward (matched by a durable
          record id, with a text fallback so a re-extraction can&apos;t orphan them).
          An item that&apos;s no longer detected isn&apos;t deleted — it stays, flagged
          &quot;carried over (last seen …)&quot;, with your edits intact.
        </p>
        <Tip>
          <strong>Structured records are created automatically</strong> when a session
          is processed (the same auto-process that produces the summary). The
          register reads those. Sessions recorded by an older build won&apos;t have
          them until reprocessed.
        </Tip>
        <Warn>
          If the workbook is open in Excel (or mid-OneDrive-sync) when you re-export,
          it will <strong>not</strong> overwrite it — it writes a dated
          &quot;(conflicted …)&quot; copy and warns you in the toast, so hand-entered
          Status/Notes are never destroyed. Close the file and re-export to merge.
        </Warn>
      </>
    ),
  },
  {
    id: "insights",
    title: "Insights",
    content: (
      <>
        <p>
          The <strong>Insights</strong> tab is a cross-meeting trend dashboard. Pick a date
          window (or leave it open) and optionally scope to one client; the page renders three
          aggregated views computed live over your session library:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>Time allocation</strong> — total recorded time bucketed by client and by
            summary template, so you can see where your meeting hours are actually going.
          </li>
          <li>
            <strong>Recurring topics</strong> — common phrases extracted from summaries,
            grouped per client. Useful for noticing which threads keep coming back without
            being closed out.
          </li>
          <li>
            <strong>Open loops</strong> — follow-ups and decisions that are still unchecked
            past a staleness threshold (30 days by default). Pulled from the same
            per-item state you maintain in the Follow-Ups / Decisions tabs, so checking an
            item off there removes it here.
          </li>
        </ul>
        <Tip>
          Everything is computed on demand from the session JSONs you already have on disk —
          no separate index to build, no separate database to back up. Open Insights and the
          aggregation runs in under a second on a typical library.
        </Tip>
      </>
    ),
  },
  {
    id: "live-and-search",
    title: "Live Transcription, Search & Q&A",
    content: (
      <>
        <p className="font-medium">Live transcription panel (during recording)</p>
        <p>
          A rolling preview of what&apos;s being said appears next to the recording controls
          while audio is being captured. Each segment is tagged{" "}
          <strong>You</strong> (your mic) or <strong>Them</strong> (system audio — everyone
          else on the call). The two streams are transcribed independently rather than mixed,
          which keeps your voice from drowning out the far end. Segments arrive in 15-second
          windows — boundary words occasionally split across two segments, which is why the
          canonical post-stop transcript (run by <strong>Process</strong>) is what gets
          persisted. Disable in{" "}
          <strong>Settings → Workflow → Live transcription during recording</strong>{" "}
          on slower machines or when the live preview is just noise.
        </p>

        <p className="font-medium mt-4">In-call search (during recording)</p>
        <p>
          A second panel appears beside the live transcript while
          you&apos;re recording, with a toggle for two scopes:
        </p>
        <ul className="list-disc list-inside ml-2 mt-1">
          <li>
            <strong>This call</strong> (default) — instant text find
            through the live transcript above. Type a phrase, hit Find,
            and matching segments highlight with timestamps and the You
            / Them speaker tag. No AI call, no API cost, no latency.
            Best for &ldquo;did anyone mention X yet?&rdquo; or &ldquo;what
            was that company name?&rdquo;.
          </li>
          <li>
            <strong>This call (AI)</strong> — Claude answers a question
            grounded in the live transcript so far. Useful when the
            answer isn&apos;t a literal phrase someone said (&ldquo;what
            number did she mention for Q3?&rdquo;,
            &ldquo;summarize what we&apos;ve discussed about the timeline&rdquo;).
            Costs an API call. The most recent ~8 KB of transcript is
            sent as context — older callers get truncated to keep the
            prompt cheap.
          </li>
          <li>
            <strong>Past meetings</strong> — semantic search across your
            indexed history, answered by Claude with citations. Same
            pipeline as the standalone Q&amp;A tab. Click-through to
            source sessions is disabled during a recording so you
            don&apos;t navigate away mid-call — the cited session IDs are
            shown for follow-up afterward.
          </li>
        </ul>

        <p className="font-medium mt-4">Speaker bleed reduction (mic duck)</p>
        <p>
          If you record over speakers (vs. headphones), your mic picks
          up the far end&apos;s voice and pollutes the live &ldquo;You&rdquo;
          transcript with garbled echoes. The live pipeline now applies
          a duck gate — when system audio is loud, the mic stream
          feeding the live transcript is attenuated so the loopback
          channel (which already captures the far end cleanly) carries
          the signal. The mic recording on disk is untouched, so the
          canonical post-stop transcript still has full-fidelity audio
          for proper speaker diarization.
        </p>

        <p className="font-medium mt-4">Free AI providers</p>
        <p>
          Settings → AI Provider now includes presets for{" "}
          <strong>Groq</strong> (free, fastest hosted inference),{" "}
          <strong>Google Gemini</strong> (free tier), in addition to
          existing <strong>OpenRouter</strong> (free-tier Llama / Qwen /
          DeepSeek) and <strong>Ollama</strong> (fully local). Each
          preset auto-fills the base URL and a default model so you
          only need to paste the API key (Ollama doesn&apos;t even
          need that). Sign-up links are listed inline.
        </p>

        <p className="font-medium mt-4">Semantic search across meetings</p>
        <p>
          The <strong>Search</strong> tab in the Knowledge Base now does meaning-based retrieval,
          not just keyword match. Type <em>&quot;billing concerns from finance&quot;</em> and you get hits
          where someone said <em>&quot;the CFO is worried about invoice timing&quot;</em> — wording you
          never typed. Powered by a local 22 MB sentence-transformer (MiniLM-L6); no embeddings
          ever leave the machine. Filters by client / project apply on top of the semantic ranking.
        </p>

        <p className="font-medium mt-4">Cross-meeting Q&A with citations</p>
        <p>
          New <strong>Q&A</strong> tab. Ask a natural-language question (&quot;what did we decide
          about the migration phasing?&quot;) and the answer streams back inline with citations
          like <code>[ABC123 @ 12:34]</code> — click the citation to jump to that moment in
          the source session. Each turn is independent (no multi-turn memory yet); scope by
          client or project to keep retrieval focused on one account.
        </p>

        <p className="font-medium mt-4">Cross-session speaker fingerprinting</p>
        <p>
          When you rename a speaker (e.g. SPEAKER_01 → &quot;Maria Chen&quot;), the embedding
          fingerprint is saved. Future sessions where Maria speaks will auto-label her — no
          re-tagging. Manage the roster in <strong>Settings → Known Speakers</strong>: rename,
          delete, or merge two profiles that ended up as separate entries (typical when the same
          person was labeled differently before enough samples accumulated). Profiles are stored
          locally in <code>speaker_profiles.json</code>; nothing leaves the machine.
        </p>
      </>
    ),
  },
  {
    id: "live-copilot",
    title: "Live Co-Pilot (beta)",
    content: (
      <>
        <p>
          A panel that watches the live transcript while you&apos;re recording
          and, every ~45 seconds, asks the configured LLM for three short
          bullet lists based on the last ~10 minutes of conversation:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>Clarifying questions</strong> — what to ask now to fill
            gaps the room hasn&apos;t addressed.
          </li>
          <li>
            <strong>Risks &amp; assumptions</strong> — unspoken assumptions
            or flags worth surfacing.
          </li>
          <li>
            <strong>Suggested follow-ups</strong> — concrete next steps.
          </li>
        </ul>
        <p>
          Each list is capped at three bullets per tick. The panel keeps a
          scrolling history of every tick during the call so you can scroll
          back to earlier suggestions; nothing is overwritten. <strong>Every
          tick is also saved with the session</strong>, so after the meeting
          ends you can open the saved session and find the full coaching
          record in the <strong>Co-Pilot</strong> tab — alongside the
          transcript, summary, and screenshots.
        </p>

        <p className="font-medium pt-2">Turning it on</p>
        <p>The co-pilot is opt-in — there are two ways to enable it:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>Settings → Workflow → Live Co-Pilot (beta)</strong>.
            Persists across restarts.
          </li>
          <li>
            <strong>The Co-Pilot switch in the recording bar</strong> (next
            to Screenshot / Stop). Flip on or off mid-call without leaving
            the page; safe to toggle while recording.
          </li>
        </ul>
        <p>
          Requires <strong>Live transcription</strong> to also be on — the
          co-pilot reads from the same segment stream.
        </p>

        <p className="font-medium pt-2">Cost &amp; the optional cheap model</p>
        <p>
          By default each tick costs an LLM call on your main provider —
          about <strong>$0.10–$0.20 per hour</strong> of meeting on Anthropic
          Haiku. To drop the live side to $0 you can override just the
          co-pilot&apos;s model in{" "}
          <strong>Settings → Live Co-Pilot model</strong> (the card appears
          when the feature is on):
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>Ollama</strong> (local, free): install from{" "}
            <a
              href="https://ollama.com/download"
              className="underline"
              target="_blank"
              rel="noreferrer"
            >
              ollama.com
            </a>
            , run <code>ollama pull llama3.1</code>, set base URL to{" "}
            <code>http://localhost:11434/v1</code>.
          </li>
          <li>
            <strong>OpenRouter free tier</strong>: free models with ~50
            requests/day cap (one long meeting can hit it).
          </li>
        </ul>
        <p>
          Post-meeting summaries stay on whatever your main AI Provider is —
          the override only affects the 45-second tick calls.
        </p>

        <p className="font-medium pt-2">Controls inside the panel</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>Refresh now</strong> — force an immediate tick (e.g.
            after a key moment in the meeting).
          </li>
          <li>
            <strong>Pause</strong> — stop the 45-second timer (the panel
            stays visible; toggle back on to resume).
          </li>
        </ul>
      </>
    ),
  },
  {
    id: "ollama-setup",
    title: "Running Ollama locally (for $0 Live Co-Pilot ticks)",
    content: (
      <>
        <p>
          Ollama runs an LLM on your own machine, so the Live Co-Pilot&apos;s
          45-second tick calls cost <strong>nothing</strong>. The
          post-meeting summary stays on your main provider (Anthropic
          Haiku, recommended for vision + quality), only the live side
          moves to local inference.
        </p>

        <p className="font-medium pt-2">Install Ollama</p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <strong>Windows / macOS:</strong> download the installer
            from{" "}
            <a
              href="https://ollama.com/download"
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              ollama.com/download
            </a>{" "}
            and run it. Ollama installs as a background service and
            auto-starts at login — you should see a llama icon in the
            system tray (Windows) or menu bar (macOS).
          </li>
          <li>
            <strong>Verify it&apos;s running</strong> by visiting{" "}
            <code className="text-[11px]">http://localhost:11434</code>{" "}
            in your browser. You should see
            &ldquo;Ollama is running&rdquo;.
          </li>
        </ul>

        <p className="font-medium pt-2">Pull a model</p>
        <p>
          Open a terminal (PowerShell on Windows, Terminal on macOS) and
          run one of:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            <code className="text-[11px]">ollama pull llama3.1</code> —
            ~5 GB, our default recommendation. Good JSON compliance,
            3–8&nbsp;s per tick on modern hardware.
          </li>
          <li>
            <code className="text-[11px]">ollama pull qwen2.5:7b</code>{" "}
            — slightly better at &ldquo;what&apos;s the unspoken
            assumption&rdquo; style prompts; similar size and speed.
          </li>
          <li>
            <code className="text-[11px]">ollama pull llama3.2:3b</code>{" "}
            — fallback for older machines. ~2 GB, 1–3&nbsp;s per tick,
            noticeably weaker quality.
          </li>
        </ul>
        <p>
          Once the pull finishes you can sanity-check with{" "}
          <code className="text-[11px]">
            ollama run llama3.1 &quot;Return JSON: {"{ok:true}"}&quot;
          </code>
          .
        </p>

        <p className="font-medium pt-2">Point Meeting Recorder at Ollama</p>
        <ol className="list-decimal pl-5 space-y-1">
          <li>
            <strong>Settings → Workflow → Live Co-Pilot (beta)</strong>{" "}
            → flip on.
          </li>
          <li>
            A new <strong>Live Co-Pilot model</strong> card appears
            immediately below the Workflow card. Enable{" "}
            <em>Use a different model for live ticks</em>.
          </li>
          <li>
            Provider: <strong>OpenAI-compatible</strong>.
          </li>
          <li>
            Model id: the exact name from{" "}
            <code className="text-[11px]">ollama list</code> (typically{" "}
            <code className="text-[11px]">llama3.1</code> or{" "}
            <code className="text-[11px]">llama3.1:latest</code>).
          </li>
          <li>
            Base URL:{" "}
            <code className="text-[11px]">http://localhost:11434/v1</code>
            .
          </li>
          <li>
            API key: anything non-empty (e.g.{" "}
            <code className="text-[11px]">ollama</code>). Ollama ignores
            it; the OpenAI SDK we use just won&apos;t construct without
            one.
          </li>
          <li>Save Settings.</li>
        </ol>

        <p className="font-medium pt-2">Verify it&apos;s actually hitting Ollama</p>
        <p>
          Start a recording with Co-Pilot on. The live ticks should
          arrive in the Co-Pilot panel within ~45 seconds. To confirm
          where each tick is going, three checks:
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li>
            Tail{" "}
            <code className="text-[11px]">
              %LOCALAPPDATA%/Ollama/server.log
            </code>{" "}
            (Windows) or{" "}
            <code className="text-[11px]">~/.ollama/logs/server.log</code>{" "}
            (macOS) — every tick produces a{" "}
            <code className="text-[11px]">POST /api/chat</code> line.
          </li>
          <li>
            Run <code className="text-[11px]">ollama ps</code> while a
            tick is in flight — you&apos;ll see your model loaded with
            its memory footprint.
          </li>
          <li>
            Anthropic&apos;s console (
            <a
              href="https://console.anthropic.com"
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              console.anthropic.com
            </a>{" "}
            → Usage) should show <em>no</em> spike during the recording
            and a brief burst right after Stop (the post-meeting
            extractions still run on the main provider).
          </li>
        </ul>

        <p className="font-medium pt-2">Two-provider configuration recap</p>
        <p>
          The right setup for most users is{" "}
          <strong>main provider = Anthropic Claude (Haiku 4.5)</strong>{" "}
          for post-meeting summaries (it has vision and reads
          screenshots) and{" "}
          <strong>Live Co-Pilot override = Ollama / OpenRouter free</strong>{" "}
          for the in-call ticks. If you set <em>both</em> to Ollama, the
          screenshots you capture during the meeting won&apos;t show up
          in the summary — Ollama text models are text-only and the app
          silently drops images for OpenAI-compatible providers.
        </p>
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

        <p className="mt-4 font-medium">Designated Folder (per client auto-export):</p>
        <p>
          Each client has a <strong>Designated Folder</strong> card. Click <strong>Browse…</strong>
          to pick a folder (opens a native Windows folder picker), or paste a path. Once set,
          every session tagged to that client auto-copies its artifacts there after processing:
          WAV (on stop or import), transcript, summary, action items, decisions, and requirements.
          No more manually shuffling files into client folders.
        </p>
        <Tip>
          The copy happens in the background — failures are logged but never block the main
          flow, so a missing network share or a permission glitch won&apos;t stop a recording
          from saving. Leave the field blank to disable auto-routing for that client.
        </Tip>
      </>
    ),
  },
  {
    id: "templates",
    title: "Summary Templates",
    content: (
      <>
        <p>
          <strong>Settings → Summary Templates</strong> is the prompt library that powers the
          Summarize action. Each template is a named prompt Claude (or your chosen provider)
          follows when you click Summarize on a session. Five ship by default — General,
          Requirements Gathering, Design Review, Sprint Planning, Stakeholder Update — and
          every one of them is editable.
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Edit a template</strong> — click the row. Change the prompt to match how
            your meetings actually go (e.g. extend Requirements Gathering to also ask about
            existing IVR providers, current call volume, hours of operation).</li>
          <li><strong>Reset to default</strong> — edited defaults show a small &quot;edited&quot;
            badge. Click the revert icon (🔄) to restore the prompt that shipped with the app.</li>
          <li><strong>New template</strong> — click <strong>New</strong> to add your own. Great
            for recurring meeting types that don&apos;t match a default — &quot;SOW Kickoff&quot;,
            &quot;Customer Onboarding,&quot; &quot;Post-Mortem,&quot; etc. Once saved it appears in
            the Template dropdown in the Record view and the Session Detail dialog.</li>
          <li><strong>Delete / hide</strong> — defaults are <em>hidden</em> when deleted (the
            prompt stays on disk so you can undelete by re-creating with the same name).
            User-created templates are fully removed.</li>
        </ul>
        <Tip>
          Templates are stored in <code>%LOCALAPPDATA%\MeetingRecorder\summary_templates.json</code>.
          Backup that file if you want to move templates between machines.
        </Tip>
        <Tip>
          <strong>Only the Summarize action uses templates.</strong> Action Items, Decisions,
          Requirements, and Prep Brief use fixed prompts that aren&apos;t exposed in the UI —
          those outputs are structured enough that customization rarely helps. Edit the summary
          prompt to focus the free-form narrative.
        </Tip>
      </>
    ),
  },
  {
    id: "ai-provider",
    title: "AI Provider & Models",
    content: (
      <>
        <p>
          <strong>Settings → AI Models → AI Provider</strong> picks which LLM family handles
          summaries, action items, decisions, requirements, speaker identification, and prep
          briefs. Pick one, set the model, click Save.
        </p>

        <div className="rounded-md border bg-muted/40 p-4 space-y-3 text-sm mt-3">
          <div>
            <div className="font-medium">Anthropic — Claude (paid, best quality)</div>
            <p className="text-muted-foreground pl-3">
              Uses the native Anthropic SDK and your <code>sk-ant-api03-...</code> key. Model
              presets: Haiku 4.5 (default, ~$0.05/meeting), Sonnet 4.6 (~4× cost), Opus 4.7
              (~15× cost), Haiku 3.5 (legacy). Pick Haiku unless you have a specific quality
              need.
            </p>
          </div>
          <div>
            <div className="font-medium">OpenRouter — free-tier large models</div>
            <p className="text-muted-foreground pl-3">
              Gateway that exposes free-tier quotas for Llama 3.3 70B, Gemini 2.0 Flash, Qwen
              2.5 72B, DeepSeek R1, and Mistral Small. Get a free key at{" "}
              <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noreferrer" className="text-primary hover:underline">openrouter.ai/settings/keys</a>.
              Free-tier models have rate limits (roughly 50 requests/day across all free models
              combined) but cost $0. Paid models (Claude pass-through, GPT-4o, etc.) are also
              available if you add credits.
            </p>
          </div>
          <div>
            <div className="font-medium">Ollama — local, offline, zero cost</div>
            <p className="text-muted-foreground pl-3">
              Runs a model on your machine. Install Ollama from{" "}
              <a href="https://ollama.com/download" target="_blank" rel="noreferrer" className="text-primary hover:underline">ollama.com</a>,
              run <code className="text-[11px]">ollama pull llama3.1</code> (or any model
              you&apos;ve seen listed on <a href="https://ollama.com/library" target="_blank" rel="noreferrer" className="text-primary hover:underline">ollama.com/library</a>),
              confirm it&apos;s serving on <code>http://localhost:11434</code>, then in Settings
              pick Ollama and type the model tag (e.g. <code>llama3.1</code>, <code>phi3</code>).
              No API key. Nothing leaves your machine. Quality is lower than Claude/GPT-4 but
              zero-friction for sensitive recordings.
            </p>
          </div>
          <div>
            <div className="font-medium">Custom OpenAI-compatible</div>
            <p className="text-muted-foreground pl-3">
              Any service that implements the OpenAI Chat Completions protocol: LM Studio, vLLM,
              LocalAI, Groq, Together.ai, Cerebras, self-hosted. Paste the <code>.../v1</code>
              base URL, the API key the provider gave you, and the model id.
            </p>
          </div>
        </div>

        <Tip>
          <strong>Changing provider doesn&apos;t rewrite old sessions.</strong> Summaries and
          extractions are saved as-is. To re-process an old session with a new provider, open
          it and click the AI Action button again — it&apos;ll overwrite with the new output.
        </Tip>
        <Tip>
          The <strong>Model</strong> field always accepts a free-form value. If a model isn&apos;t
          in the preset dropdown (new OpenRouter release, custom fine-tune, niche Ollama tag),
          pick &quot;Custom (type your own)&quot; from the dropdown and paste the exact model id.
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
          <li><strong>Auto-record from calendar</strong> — see the toggle in the Upcoming Meetings card on the Record view. Starts recording automatically at each meeting&apos;s scheduled start (filters: skip all-day events, require a Teams/Zoom/Meet/Webex/GoToMeeting/BlueJeans/Whereby link). Auto-stop is handled by the existing silence + overrun watchdog with the meeting&apos;s scheduled end pre-filled. Manual recordings always win.</li>
          <li><strong>Auto-process after stop</strong> — transcribe → speaker-diarize → summary → action items → decisions → requirements all run automatically when you stop recording. Each stage is independent; a failure on one (rate-limit on Claude, etc.) doesn&apos;t stop the others.</li>
          <li><strong>Auto-draft follow-up email</strong> — after processing, Claude generates a per-attendee follow-up email with their specific action items and meeting context, then creates Outlook drafts (one per person) in your Drafts folder. You review and hit Send. Requires Classic Outlook.</li>
          <li><strong>Launch on Windows startup</strong> — adds a shortcut to the Startup folder via in-process COM (no subprocess at all since v2.0.14 — eliminates the occasional CMD flash and the AV-kill risk on locked-down laptops).</li>
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
    id: "sync-across-devices",
    title: "Sync Across Devices",
    content: (
      <>
        <p>
          Point the recordings folder at a cloud-synced directory (OneDrive, iCloud Drive,
          Dropbox, Google Drive) and your sessions, clients, and summary templates will roam
          across every device that has the same folder mounted. No login, no backend, no
          subscription — file sync via whatever cloud client you already have.
        </p>
        <p className="font-medium mt-3">Setup on the primary device (where your sessions live today)</p>
        <ol className="list-decimal pl-5 space-y-2">
          <li>Make sure your cloud client (OneDrive, iCloud for Windows, etc.) is signed in
            and syncing.</li>
          <li>In Meeting Recorder, open <strong>Settings &gt; Recordings Folder</strong>.
            Click <strong>Browse…</strong> and pick a folder inside your cloud-synced root —
            something like <code>C:\Users\YOU\OneDrive\MeetingRecorder</code> on Windows or
            <code>~/OneDrive/MeetingRecorder</code> on Mac. Click <strong>Save</strong> at
            the bottom of Settings, then restart the app.</li>
          <li>On Save, the app automatically copies <code>client_configs.json</code> and
            <code>summary_templates.json</code> from your previous recordings folder into the
            new one — so your client list and custom templates carry over. (Copy, not move —
            the old files stay where they are in case you want to revert.)</li>
          <li>Existing session WAVs and JSON files are <em>not</em> moved automatically.
            See &quot;Moving existing recordings&quot; below.</li>
          <li>Wait for the cloud client to upload everything. With long recordings this can
            take a while — WAVs are big (~115 MB per hour at 16 kHz mono).</li>
        </ol>
        <p className="font-medium mt-3">Setup on the secondary device</p>
        <ol className="list-decimal pl-5 space-y-2">
          <li>Install Meeting Recorder. Install / sign in to the same cloud client as the
            primary device.</li>
          <li>Wait for the cloud client to finish syncing down everything the primary device
            uploaded. Verify in File Explorer / Finder that the
            <code>MeetingRecorder/</code> folder is present locally with the session files
            inside.</li>
          <li>Open Settings &gt; Recordings Folder, point at the same synced folder, Save,
            restart.</li>
          <li>Your PC&apos;s clients, templates, and sessions all appear.</li>
        </ol>
        <p className="font-medium mt-3">Moving existing recordings</p>
        <p>
          The folder picker only changes <em>future</em> recording targets. To bring your
          existing library along, copy or move the contents of the old recordings folder into
          the new one — everything in there (<code>session_*.json</code>,
          <code>session_*.wav</code>, <code>*.commitments.json</code>,
          <code>*.item_status.json</code>, <code>*.embeddings.pkl</code>) is portable. The
          sessions list will pick them up on next launch.
        </p>
        <p className="text-sm text-muted-foreground">
          Windows PowerShell (with the app fully quit):
        </p>
        <pre className="text-xs bg-muted/40 rounded p-2 overflow-x-auto"><code>{`# Confirm source — wherever your old recordings_dir was
$source = "C:\\meeting_recorder\\recordings"   # or %LOCALAPPDATA%\\MeetingRecorder\\recordings
$target = "$env:OneDrive\\MeetingRecorder"
Copy-Item -Path "$source\\*" -Destination $target -Recurse -Force`}</code></pre>
        <Tip>
          <strong>Speaker profiles stay per-machine.</strong> Voice fingerprints
          (<code>speaker_profiles.json</code>) live outside the recordings folder and don&apos;t
          sync. They&apos;re tied to the mic hardware that captured them — a profile trained on
          a Yeti mic may not match the same person captured through a MacBook&apos;s built-in
          array, so syncing them across mics would produce false positives. Re-enroll on each
          device as you record there.
        </Tip>
        <Warn>
          <strong>No conflict resolution.</strong> If two devices record at the same instant
          and both write to the same cloud folder, OneDrive / iCloud will produce a conflict
          file (something like <code>session_ABC123-PC.json</code>) rather than merging.
          Session JSONs are tiny so this is rare in practice, but be aware. Sync is not
          real-time — expect minutes of lag.
        </Warn>
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
        <Tip>
          <strong>Retention also sweeps the WAV copies in client Designated Folders.</strong>
          When a recording is processed and exported to <code>C:\Users\...\Clients\Acme</code>,
          both the original under <code>recordings\</code> and the copy in the client folder age
          out on the same schedule. Text exports (transcript, summary, action items) stay behind
          in the client folder so you keep a permanent archive — only the bulky audio goes.
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
          Cost depends on the AI Provider you pick (Settings → AI Provider).
        </p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Anthropic / Claude Haiku 4.5</strong> (default) — ~$1/M input, $5/M output.
            A full extraction pipeline for a typical meeting costs under $0.05. Hundreds of
            meetings per year for a few dollars.</li>
          <li><strong>Anthropic / Claude Sonnet 4.6</strong> — ~4× Haiku cost, higher quality.
            Worth it for dense design reviews where nuance matters.</li>
          <li><strong>OpenRouter free-tier</strong> (Llama 3.3 70B, Gemini 2.0 Flash, Qwen 2.5
            72B, DeepSeek R1, Mistral Small) — $0. Rate-limited to ~50 requests/day across
            free models; you&apos;ll hit the limit if you bulk-process many meetings in one sitting.</li>
          <li><strong>Ollama</strong> (local) — $0, no rate limit, runs on your CPU/GPU. Quality
            varies by model but for summaries of your own meetings it&apos;s typically fine.</li>
        </ul>
        <Tip>
          Transcription (Whisper) and speaker diarization (Pyannote) are already 100% local —
          they don&apos;t cost anything regardless of AI Provider. Only the summary/extraction
          calls route through the chosen provider.
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
            <dt className="font-medium text-sm">Calendar shows no meetings (Windows)</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              The Upcoming Meetings panel shows the next 7 days. If you genuinely have nothing
              scheduled in that window (common late Friday through Sunday), that&apos;s the
              expected empty state. Otherwise: requires Classic Outlook — New Outlook doesn&apos;t
              support the COM API we use. Switch in Outlook settings → File → Info → Toggle off
              &quot;Try the new Outlook&quot;, then restart.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">Calendar shows no meetings (macOS)</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              First check System Settings → Privacy &amp; Security → Calendars. Meeting Recorder
              should be listed with its toggle on. If not, launch the app once to trigger the
              permission prompt and click <strong>Allow</strong>. If the toggle <em>is</em> on
              but calendar still shows no meetings: macOS TCC has a stale code-signature entry
              for an older build. Reset and re-grant:
              <pre className="text-xs bg-muted/40 rounded p-2 mt-2 overflow-x-auto"><code>{`tccutil reset Calendar com.joshuarodriguez.meeting-recorder
# Quit Meeting Recorder (Cmd-Q), then:
open "/Applications/Meeting Recorder.app"
# Click Allow on the prompt; calendar populates within ~30 seconds.`}</code></pre>
              v2.3.5+ ad-hoc signs the .app with an identifier-based Designated Requirement
              so this should be a one-time fix on upgrade from a pre-2.3.5 build.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">&quot;Summarization API call failed&quot; error</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Check Settings → AI Provider. Common causes: empty / invalid API key, wrong
              base URL for custom endpoints, Ollama not running, or OpenRouter free-tier daily
              quota hit. The error toast includes the underlying HTTP status and provider
              message — 401 = bad key, 429 = rate-limited, 404 = model id doesn&apos;t exist.
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
            <dt className="font-medium text-sm">A black console window with &quot;pythonw.exe&quot; in the tab keeps opening</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Fixed in v2.0.19 — caused by certain Intel MKL / OpenMP / CUDA runtimes calling
              <code> AllocConsole()</code> during their init to install a console control handler.
              That creates a visible conhost window titled with the process name. The backend now
              calls <code>FreeConsole()</code> at startup and runs a 2-second watchdog that
              detaches any console a library sneaks in later — the window disappears before you
              see it. If you still see one in v2.0.19+, screenshot it and note the title bar text
              so we can find which DLL is doing it.
            </dd>
          </div>
          <div>
            <dt className="font-medium text-sm">A Windows &quot;pythonw.exe stopped working&quot; dialog pops up</dt>
            <dd className="text-sm text-muted-foreground mt-1">
              Different from the black-console issue above — this one is a Windows Error
              Reporting dialog after a crash. Fixed in v2.0.18: <code>SetErrorMode</code>
              suppresses WER dialogs at startup. The Rust watchdog respawns the backend
              automatically. Check <code>%LOCALAPPDATA%\MeetingRecorder\rust.log</code> for the
              exit code — <code>3221225477</code> / <code>0xC0000005</code> means access
              violation (typically corporate AV / EDR scanning a loading DLL).
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
