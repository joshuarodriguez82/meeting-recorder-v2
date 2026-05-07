# v2.2.4 — Pre-meeting briefs, commitments tracker, and trackable follow-ups

The first release since v2.2.1 — bundles three patch releases worth of
work into one DMG/MSI. Highlights: Claude-generated meeting briefs on
your calendar tiles, a cross-meeting commitments tracker, follow-ups
you can actually check off, decisions with a real lifecycle, and the
auto-stop watchdog so you stop accidentally recording for 8 hours.

> ## ⚠️ macOS install — READ THIS FIRST
>
> ### Step 1: download the .dmg
>
> v2.2.4 ships **a single universal `.dmg`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.2.4_universal.dmg`.
>
> ### Step 2: bypass Gatekeeper
>
> The build is **unsigned** (no Apple Developer cert yet), so macOS will
> say *"damaged and can't be opened"* when you double-click. It is **not**
> damaged — that's the quarantine attribute your browser added on
> download. Pick whichever path is easier; both work, both are one-time.
>
> **Path A — System Settings (no Terminal):**
>
> 1. Double-click the DMG, drag the app to **Applications**.
> 2. Double-click `Meeting Recorder` in Applications. macOS refuses
>    with the "damaged" warning. Click Done.
> 3. Open **System Settings → Privacy & Security**. Scroll to the
>    Security section. Click **Open Anyway** next to the Meeting
>    Recorder message.
> 4. Re-double-click the app. macOS asks once more — click Open. Done.
>
> **Path B — Terminal:**
>
> ```sh
> xattr -cr ~/Downloads/Meeting*.dmg
> open ~/Downloads/Meeting*.dmg
> # In Finder: drag the app icon to Applications.
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> macOS treats the app as trusted on every subsequent launch. **Right-
> click → Open does not work** on macOS Sequoia / Sonoma.
>
> **Windows users** — none of this Gatekeeper stuff applies. Download
> the `.msi` or `.exe` and double-click.

## What's new since v2.2.1

### Pre-meeting briefs on calendar tiles

Click **Brief** on any upcoming meeting in the Record view. A modal
opens with a Claude-generated brief drawn from prior sessions tagged to
the same client:

- Meeting context header (subject, time, attendees, identified
  client/project, last meeting in scope)
- The story so far — bullets across prior calls with `[id]` citations
  that click through to the source session
- Hot topics likely to come up — themes that recurred 2+ times
- Open commitments to/from this account — pulled live from the tracker
- Questions to drive the meeting — calibrated to unblock open work

**Use for recording** hands off to the normal Record flow with client +
project pre-resolved (skips a round-trip).

### Cross-meeting commitments tracker

A new top-level **Commitments** tab. Auto-mined from every session as
soon as it finishes processing — Claude pulls out promises ("we'll send
the SOW Friday", "they'll review the diagram next week") and labels
each as customer-side or internal, with an owner, a quote for context,
and a due date when one was mentioned.

Each row supports:
- **Mark delivered** with an optional resolution note
- **Dismiss** (e.g., the LLM hallucinated a commitment that isn't real)
- Filters by client, project, owner, side, and status (active /
  delivered / dismissed / overdue)

The pre-meeting brief reads from this same tracker to surface open
items right before you walk into the room.

### Follow-ups you can actually check off

The Follow-Ups tab now has a working `○ → ✓` toggle on each item.
Done items get a strikethrough, the **Open / Done / All** filter
respects it, and state survives restarts via a per-session sidecar
(`session_<id>.item_status.json` — small, atomic, won't fight a
re-extraction unless the LLM majorly rewords the item).

### Decisions get a real lifecycle

Decisions in the Decisions tab now have three states: **Active**,
**Implemented**, **Superseded**. Status badge in the list, dropdown
in the detail panel, dedicated filter at the top with per-state
counts. Same sidecar mechanism as follow-ups.

### Auto-stop watchdog

Three independent triggers for the "I forgot the recording was running
for 6 hours" failure mode, configurable in **Settings → Auto-stop**:

| Trigger | What it watches | Default |
|---|---|---|
| **Silence (dead air)** | Mic + loopback RMS below threshold for N min | Warn at 5 min, auto-stop opt-in |
| **Meeting overrun** | Wall clock past the calendar event's scheduled end + N min | Warn at 5 min after end, auto-stop opt-in |
| **Hard cap** | Total recording duration | Always-on safety net at 4 hours; auto-stops |

Warnings render as an amber banner under the recording bar and fire a
native OS notification (once per warning code per recording — no
spam). Auto-stops use the same code path as the Stop button so the
audio file finalises cleanly and the post-stop processing chain runs
as normal. Calendar-aware overrun detection threads the scheduled end
time from the **Use** button on a calendar tile through to the
backend; ad-hoc recordings skip the overrun trigger.

### Semantic index self-maintains

You should never need to click **Index N sessions** again. Both
`/process` and `/process_full` auto-index on completion (the
auto-process-after-stop flow used to silently skip indexing), and
the backend runs a background backfill pass on every boot for any
session whose embedding sidecar is missing. The manual button still
exists in Settings → Semantic Index but is now only useful if you
want to force results before the background pass catches up.

### Universal2 macOS DMG

One DMG runs on both Apple Silicon and Intel. v2.2.0 published an
Apple-Silicon-only build because GitHub deprecated free-tier
`macos-13` runners and the Intel matrix entry kept failing to
allocate. v2.2.4 builds a universal2 (fat) binary on a single
`macos-14` runner. Install instructions collapse to one filename.

### Dependency self-heal on backend boot

If any of the feature-critical Python packages (sentence-transformers,
faiss, pyannote) is missing — usually because you upgraded over a
pre-v2.2.0 venv that the bootstrap thought was already populated —
the backend re-runs `pip install -r requirements-{cpu,mac}.txt`
against the running interpreter on boot and continues. ~10ms when
everything's already installed; one-time ~30s pause when a real
repair is needed.

## Config storage

Five new lines in `config.env` for the auto-stop watchdog:

```
SILENCE_WARN_MIN=5
SILENCE_STOP_MIN=0
OVERRUN_WARN_MIN=5
OVERRUN_STOP_MIN=0
HARD_CAP_HOURS=4
```

Existing installs migrate to the defaults automatically.

Per-session sidecar files live next to the session pickles in your
recordings folder — `session_<id>.commitments.json` (commitments
tracker rows) and `session_<id>.item_status.json` (follow-up done /
decision lifecycle overlays). Both are deleted automatically when you
delete a session.

## Bundles everything from v2.2.2 and v2.2.3

v2.2.2 (semantic index auto-build, universal2 DMG, dependency
self-heal) and v2.2.3 (auto-stop watchdog) were committed and notes
written, but the tags were never pushed to GitHub — there was no
v2.2.2 or v2.2.3 release. v2.2.4 is the first DMG/MSI that ships any
of that work.
