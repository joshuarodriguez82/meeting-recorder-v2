# v2.27.0 — no more Outlook sign-in prompts, and a much cleaner interface

## Install (macOS)

> v2.27.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.27.0_universal.zip`.
>
> Still unsigned for Gatekeeper purposes. First launch needs the
> Gatekeeper bypass — pick whichever path you prefer:
>
> **Path A — System Settings (no Terminal):** double-click the `.zip`
> in Finder (Archive Utility auto-extracts to `Meeting Recorder.app`),
> drag the `.app` to `/Applications`, double-click, dismiss the
> "damaged" warning, then **System Settings → Privacy & Security →
> Open Anyway**, double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_2.27.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.27.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Stop the Outlook sign-in prompt

Opening the Record tab triggered a Microsoft sign-in dialog every time,
and the calendar panel stayed empty.

Both had the same cause. The Upcoming Meetings endpoint **waited for
Outlook first — up to 45 seconds — and only then fetched the meetings
the Chrome extension had already scraped.** So a prompting Outlook
blocked the whole request, and events sitting ready on disk were withheld
the entire time. Merely *checking* whether Outlook is available calls
`Dispatch("Outlook.Application")`, which launches Outlook, which is
enough to trigger a locked-down tenant's sign-in challenge.

**Settings → Templates & Integrations → Calendar source:**

- **Automatic** — local calendar plus anything the extension finds
- **Chrome extension only** — never contacts Outlook at all
- **Local calendar only**
- **Off**

Pick **Chrome extension only** if Outlook keeps asking you to sign in.
Nothing in the app will touch Outlook in that mode — every entry point is
gated at a single choke point, with tests that fail if any code path
reaches Outlook when it shouldn't.

Both sources are now also fetched **concurrently**, so even on Automatic
a slow Outlook can no longer delay extension events.

### A calendar bug that had been hiding

The availability check returned a bare `true`/`false`, but the app read a
`.available` property off it — which was therefore always undefined. The
Record tab has been concluding your calendar was unavailable regardless
of reality. Now fixed.

## A cleaner, crisper interface

**The app stopped shouting.** Long grey explanations that dominated
Settings and Record now sit behind an ⓘ icon next to the thing they
explain. Nothing was deleted — every word is preserved, one click away.
The result: Settings fits four cards where it fit two, and Upcoming
Meetings is visible on the Record tab without scrolling.

**Headings look like headings.** Section titles, field labels, input text
and help text were all within a couple of pixels of each other, so
nothing anchored the eye. There's now a real scale, applied through the
shared components so it's consistent everywhere rather than
screen-by-screen.

**The Settings bars are smaller.** The tab bar and Save bar shrank by
roughly a quarter and a half respectively — measured in a real browser,
not eyeballed — while still sitting flush to the edges.

**Switching Settings tabs starts at the top.** All four tabs share one
scroll area, so scrolling deep into Setup and clicking Data &
Diagnostics used to leave you halfway down the new tab.

**The session status icons are proper icons**, not emoji. They remain a
fixed six-slot cluster where a faded slot means that step didn't
complete — a partial set is how a mid-pipeline failure gets spotted, so
the meaning is unchanged.

## A failed request no longer blanks a tab

Several views read fields straight off API responses with no guard. A
partial or malformed response — exactly what a half-started backend
returns — took the entire view to a blank error screen.

Those reads are now guarded, and every view is wrapped in an error
boundary so a failure is contained with a retry button instead of
emptying the window.

The wording matters here and was chosen deliberately: a response that
couldn't be read now says **"unavailable"**, never "none found". A thing
you couldn't read must never look like a thing that isn't there.

## The test that would have caught yesterday's outage

v2.26.0 shipped a backend that could not start, and **495 tests passed on
it.** Nothing in the suite ever constructed the app and started it — the
tests covered the parts and never the wiring.

There is now a boot smoke test that builds the real app, asserts every
service it should create actually exists, and drives eight endpoints
looking for server errors. The list of services it checks is derived from
the startup code itself, so it can't drift as services are added.

It was verified by reintroducing the exact defect and confirming the test
fails — a regression test that can't fail is decoration.

## For contributors

`tools/ui-harness/` renders the app headlessly against a stub backend and
measures or screenshots it. It exists because two layout fixes were
shipped blind and both were wrong. Not wired into CI — a flaky screenshot
job is worse than none — but it's there when a change needs looking at.

Dependency scanning (`pip-audit`, `npm audit`, `cargo audit`) runs on
pull requests and weekly, non-blocking.
