# v2.2.0 — Cross-meeting intelligence

The first release after v2.1.0 (macOS support) brings four
roadmap features that turn Meeting Recorder from a per-call tool into
something that actually knows about your meeting history.

## What's new

### Cross-session speaker fingerprinting

Label a speaker once — `John Smith` — and the next meeting where John
talks auto-tags him without you typing the name again. After diarization,
the backend computes per-speaker centroid embeddings (192-dim ECAPA),
matches against a persistent profile store, and surfaces matches as
`Likely match · 87%` confirm/reject prompts in the session detail dialog.

- New **Settings → Known Speakers** card: rename, delete, or merge
  duplicate profiles (useful when "John Smith" and "John S." were both
  created before fingerprinting had a profile to match against).
- Sessions processed before this feature shipped lazily backfill the
  centroid from saved audio the first time you rename a speaker — no
  re-process required.
- All embeddings stored locally in `speaker_profiles.json`. Nothing
  leaves your machine.

### Live transcription

Streaming preview of what's being said while you record. Audio mixes
mic + system-audio loopback (so you see other meeting participants
in real time, not just yourself), runs through Whisper in 15-second
non-overlapping windows, and streams segments to the frontend via SSE.

- New live transcript panel mounts under the recording bar with a
  fixed-height scrollable area and an always-visible scrollbar.
- Speaker labels are NOT applied to live segments (pyannote needs the
  full audio for clustering); the post-stop canonical transcript still
  has them.
- Toggle the whole feature off in **Settings → Workflow → Live
  transcription during recording** — saves CPU on slower machines.

### Semantic search across meetings

The Search tab grew a Keyword | Semantic toggle. Semantic mode ranks
transcript chunks by meaning rather than keyword overlap — finds
matches that share zero words with your query (e.g.
"how should we approach pricing" matches a chunk about charging
strategy that never says "pricing").

- Embeddings are computed by a small (22 MB) MiniLM model that runs
  locally on CPU. Per-session pickle sidecar files store chunk +
  embedding pairs.
- New sessions auto-index when you process them. Older sessions need
  a one-time backfill at **Settings → Semantic Index → Index N
  sessions** (~1s per session on Apple Silicon).
- Results surface a similarity badge color-coded by confidence
  (≥70% strong, 50–70% plausible, <50% weak) and the chunk's
  timestamp range.

### Cross-meeting Q&A (the "Ask" tab)

Ask natural-language questions across your entire meeting corpus.
Backend retrieves the top-K most-similar chunks via the semantic index,
builds a citation-aware prompt, and streams Claude's (or any
OpenAI-compatible model's) answer back token-by-token via SSE.

- Inline citations like `[ABC123 @ 12:34]` get auto-rendered as
  click-to-jump buttons opening the source session.
- Sources strip below your question shows the chunks Claude is reasoning
  over, BEFORE the answer streams — verify what raw material went in.
- Scope filters (client / project) restrict which sessions get
  searched.
- Stop button cancels a long-running answer mid-stream.

## Bug fixes & polish (also Windows)

- **AI Provider dropdown stale-closure bug**: switching from Anthropic
  to OpenRouter was clobbering the provider write because three back-to-
  back `setSettings` calls all spread the same closure-captured object.
  Fixed by switching to functional state updates. (Windows users hit
  this too — the file is shared frontend code.)
- **API Keys section now hides irrelevant fields** based on which AI
  provider is active.
- **Launch-on-startup toggle now actually does something**: it was
  cosmetic before — the setting persisted but no code applied it.
- **Mac-aware GPU acceleration card** replaces the misleading "Use This"
  CUDA button on Mac with an Apple Silicon (MPS) card showing it's
  already active. CUDA install is also rejected backend-side on Mac
  with a clear error.
- **Dev mode prefers `backend/` source over the extracted runtime zip**
  — earlier you'd have to `rm backend-bundle.zip` to test backend
  edits in `npx tauri dev`. Now in debug builds the live source wins
  by default.

## Documentation

- In-app **Usage Guide** got new sections for Live Transcript,
  Known Speakers, Semantic Search, Ask, and macOS specifics.
- README + MAC_SETUP.md updated to mention the new features.

## Breaking changes

None. All new endpoints. Existing sessions remain valid; speaker
fingerprints / semantic embeddings backfill on demand.

## New backend dependency

`sentence-transformers >= 2.7.0` (powers semantic search + Q&A retrieval).
The Windows installer's bootstrap pip install picks it up automatically;
on Mac, run `python3.13 setup.py` once to refresh the venv.

## Distribution

- **Windows**: NSIS `.exe` and MSI `.msi` published under Releases.
- **macOS**: Apple Silicon and Intel `.dmg` files published, unsigned —
  first-launch needs the right-click → Open trick. See
  [MAC_SETUP.md](./MAC_SETUP.md) for the full Mac install walkthrough
  including BlackHole audio routing.
