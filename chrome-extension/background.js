// Meeting Recorder Chrome extension — background service worker.
//
// Orchestrates the capture flow. v1.1 (chrome-extension-v1.1):
//   1. Capture FOUR sources (was two in v1.0):
//      - OWA day view  (calendar events)
//      - Teams Activity (mentions, replies, missed calls)
//      - Outlook inbox (top emails — most needs-response items live here)
//      - Teams Chat    (active 1:1 / group chats with unread)
//   2. Auto-schedule via chrome.alarms (3 default times per day,
//      user-configurable). Service worker wakes up on each alarm,
//      runs the capture, POSTs to the recorder.
//
// All four scrapes run in the USER'S real Chrome. Microsoft trusts the
// session because it's the same browser they signed into. No
// Playwright, no automation flags, no bot detection.
//
// v1.2 — calendar coverage fix (2026-08, field report: extension-only
// `calendar_source` mode showed ZERO upcoming meetings while Outlook
// Web visibly had 9 in the next 7 days and 5 on a single day).
//
// Two defects fixed:
//   1. The OWA day-view scrape above only ever supplies TODAY, so
//      extension-only mode (which never touches Outlook COM/EventKit)
//      structurally could not fill the Record tab's 168h window. Fixed
//      by a SEPARATE calendar capture below that reads the week view
//      (current + next, since a week view alone can miss the tail of
//      a 7-day-forward window depending what day it is).
//   2. The old approach dumped a char-budget of grid TEXT and asked
//      an LLM to regex-guess events out of it — 1 of 5 real meetings
//      survived that in the field. Fixed by reading each event's own
//      `aria-label` (Outlook Web must expose "Subject, 9:30 AM to
//      10:00 AM, ..." there for screen readers — that contract is far
//      more stable than any CSS selector) and parsing it directly, no
//      LLM involved. See "Calendar structured extraction" below.
//
// The calendar capture is independent of the four-source capture
// above: it runs on its own more-frequent alarm (so the Record tab's
// calendar store stays fresh even for a user who never turns on the
// heavier Teams/Inbox/Chat auto-capture — the whole point of
// `calendar_source: "extension"`), and it's ALSO folded into the
// manual "Capture & Send" flow so one button covers everything.
//
// v1.3 — field report 2026-08-14: v1.2's structured scan hit a
// tenant where it matched ZERO elements (`Calendar: 0 events`, no
// further detail) even though the legacy text path still read 1056
// chars off the same OWA tab. The v1.2 design assumed every
// meeting-shaped element carries a time range in its OWN aria-label —
// that assumption doesn't hold everywhere. Since this environment
// cannot sign in to the user's tenant to see why, three changes:
//
//   1. Diagnose calendar capture (options page button): opens the
//      calendar tab, runs a read-only probe (`_calendarDiagnosticProbeFunc`
//      below), and prints exactly what the live DOM looks like —
//      container/event-node counts, every long aria-label verbatim,
//      iframe/shadow-root counts, timed snapshots — so a correct
//      selector can be written without ever seeing the tenant, no
//      DevTools/pasted console script required.
//   2. Harden the scan itself against the likely causes a zero
//      result can't distinguish on its own: same-origin iframes and
//      open shadow roots are now searched (`_calendarDomScanFunc`'s
//      `walk`), a candidate's time no longer has to live in its own
//      label (`findExternalTime` checks a `<time>` descendant, its
//      `datetime`/`data-*` attributes, adjacent siblings, and the
//      ancestor gridcell/column), and the settle loop now retries on
//      CANDIDATE COUNT rather than firing once after a fixed text-size
//      wait (`shouldStopPolling`).
//   3. A `0 events` result is no longer silent about why: `stats` and
//      `zeroReason` (`classifyZeroReason`) distinguish "no candidate
//      elements found" / "page still rendering" / "found N candidates,
//      none had a parseable time" / "found N candidates, all all-day"
//      — each points at a different fix.
//
// v1.4 — two fields that shipped in the captured-event schema and
// were NEVER assigned by anything: `organizer` and `join_url` were
// both hard-coded to "" on every event this file has ever produced.
// Strictly additive; nothing about candidate discovery,
// parseMeetingLabel, TIME_RANGE_RE, the date resolution or the
// merge/dedup changes, and every new extraction returns "" on failure,
// i.e. exactly the value the field already had.
//
//   1. `organizer` is now populated — from data already being scanned.
//      Outlook Web writes "By <name>" into the SAME label tail the
//      date resolver reads, so `extractOrganizerFromLabel` reads that
//      string a second time and lifts the name out of it. The awkward
//      shapes are the whole job: the name routinely contains commas
//      ("Roe, Pat Jr. [US-US]"), so the tail cannot be split on comma,
//      and show-as/recurrence/cancellation words trail it and must not
//      be absorbed. Beyond display this feeds
//      services/follow_up_recipients.py, which resolves an address out
//      of a directory far more often from a full name than from the
//      bare first name an action item usually carries.
//   2. `join_url` is NOT populated, and the reason is now measurable
//      rather than assumed. Both native backends get it from the
//      invite BODY (`_extract_join_url(location, body)`), which this
//      scrape does not have — the aria-label carries "Microsoft Teams
//      Meeting" as a label, never the URL. Whether a link is
//      nonetheless reachable from the elements already scanned is a
//      question about a live tenant, so "Diagnose calendar capture"
//      grew a read-only join-link probe that counts join-shaped
//      anchors and reports how many sit inside — or beside — a
//      meeting-shaped aria-label (see JOIN_URL_PROBE_CONFIG). Until
//      that comes back non-zero the field stays empty and the popup
//      SAYS it is empty; clicking into every event to scrape a detail
//      pane is not a default anyone gets by accident.
//
// v1.5 — v1.4's join-link probe asked the wrong question and then
// stated a confident negative from the answer. It searched for join-
// shaped ANCHOR elements (`a[href]`), found none, and concluded "no
// join-shaped links anywhere in the scanned roots — the grid does not
// expose them; join_url cannot be filled from this DOM". The same
// diagnostic's own `longestLabels` array disproved it in the same
// report: a join URL sat in the aria-label TEXT, between the date
// segment and the `By <organiser>` segment, on the labels this file
// already parses twice.
//
// It varies by provider, and that is the whole explanation: a Teams
// event renders the literal words "Microsoft Teams Meeting" and no
// URL, while an add-in that writes the join link into the event's
// LOCATION field (Zoom's Outlook add-in does) gets that Location
// rendered into the label by OWA. So the link is there for any meeting
// whose organiser used such an add-in, and absent for the rest — which
// is a per-event fact, not a per-tenant impossibility.
//
//   1. `join_url` is now populated from the label text
//      (`extractUrlsFromLabel`), for RECOGNISED CONFERENCING PROVIDERS
//      ONLY (the same four the probe already knows: Teams
//      `meetup-join`, Zoom, Webex, Google Meet — see
//      JOIN_PROVIDER_PATTERNS). Any OTHER https URL in that position is
//      a location, not a meeting to join — a link to a training site
//      in the Location field is where the thing happens, and calling it
//      a join link would send the user somewhere that isn't the
//      meeting. Those land in `location` (a field that also already
//      shipped empty on this path) and never in `join_url`. The two are
//      never blurred: `join_url` means "this URL joins THIS meeting".
//   2. `organizer` handles the shapes the field data actually contains
//      beyond a display name — most importantly an SMTP ADDRESS where a
//      display name normally sits, which is kept verbatim (an address
//      is what `services/follow_up_recipients.py` needs; a name has to
//      be resolved into one and usually can't be).
//   3. The probe's verdict now distinguishes found-in-anchors /
//      found-in-label-text / genuinely absent, and says which places it
//      looked in. A diagnostic that reports "cannot be done" when it
//      means "I looked in one place" is worse than one that says
//      nothing.
//
// Nothing about candidate discovery, `parseMeetingLabel`,
// `TIME_RANGE_RE`, the date resolution, the zero-reason classification
// or the merge/dedup changes. Every new extraction returns "" on
// failure, so a label with no URL produces byte-identical events.
//
// A join URL is a single-use meeting credential: it is never logged,
// never put in a diagnostic report, and never included in an error
// string. The counting in `stats` counts and classifies only, and the
// probe's examples stay host+path SHAPE only (see `redact`).
//
// Still true, and still the point of all of the above: none of the
// live-DOM behavior here has been exercised against a real Outlook
// Web tenant in this change — there is no way to sign in to one from
// this environment. The pure functions (parseMeetingLabel,
// extractEventsFromCandidates, shouldStopPolling, classifyZeroReason)
// are covered by chrome-extension/tests/, including the DOM-walking
// functions run against a simulated fake DOM (fake iframe/shadow-root
// documents); the diagnostic tool above exists specifically because
// live tenant behavior cannot be.

// Per-source config. Teams (Activity + Chat) needs more time than
// OWA because the React tree is much heavier — first paint to
// "fully populated feed" can be 20-30s on a normal connection. Inbox
// gets a higher target because a focused-inbox list of 20+ emails
// is naturally several KB of text. The min-useful floors are tuned
// lower for Teams because some days the Activity feed is genuinely
// empty (no @mentions, no missed calls).
const SOURCES = [
  {
    key: "owa",
    url: "https://outlook.office.com/calendar/view/day",
    label: "OWA",
    maxWaitMs: 25_000,
    targetChars: 1500,
    minUsefulChars: 700,
  },
  {
    key: "teams",
    url: "https://teams.microsoft.com/v2/?clientType=desktop#/activity",
    label: "Teams Activity",
    maxWaitMs: 40_000,
    targetChars: 1200,
    minUsefulChars: 250,
  },
  {
    key: "inbox",
    url: "https://outlook.cloud.microsoft/mail/?folder=focusedinbox",
    label: "Inbox",
    maxWaitMs: 30_000,
    targetChars: 2500,
    minUsefulChars: 800,
  },
  {
    key: "chat",
    url: "https://teams.microsoft.com/v2/?clientType=desktop#/chat",
    label: "Teams Chat",
    maxWaitMs: 40_000,
    targetChars: 1500,
    minUsefulChars: 300,
  },
];

const POLL_MS = 1000;
const STABILITY_POLLS = 3;

// Dedupe: don't run a scheduled capture if one ran in the last hour.
// Manual captures (popup button) bypass this.
const DEDUPE_WINDOW_MS = 60 * 60 * 1000;

// Default scheduled times when user enables auto-capture. Local
// browser time (NOT UTC) — chrome.alarms.when uses ms-since-epoch
// computed from a local Date, which makes per-time-of-day scheduling
// work without the user thinking about timezones.
const DEFAULT_CAPTURE_TIMES = ["08:00", "12:00", "17:00"];

// ── Calendar capture config (v1.2) ──────────────────────────────────
//
// Week view, not day view — see the header comment. Verify this URL
// rather than trusting it: it mirrors the confirmed-working day-view
// URL above (`.../calendar/view/day`) with the same segment swapped
// to `week`, which is the standard Outlook Web pattern, but it has
// NOT been exercised against a live tenant in this change (no way to
// sign in to Outlook Web from this environment).
const CALENDAR_WEEK_URL = "https://outlook.office.com/calendar/view/week";

// A week view is a lot more DOM than one day — give it more time and
// a higher target than OWA_TARGET_CHARS/OWA_MIN_USEFUL_CHARS (1500 /
// 700 in the SOURCES config above), which were sized for a single
// day's grid. These only gate the TEXT fallback path (see
// `settleAndCollectCalendar`); the structured aria-label scan runs
// once the page has settled regardless of char count.
const CALENDAR_MAX_WAIT_MS = 45_000;
const CALENDAR_TARGET_CHARS = 4500;   // ~3x the day view's target
const CALENDAR_MIN_USEFUL_CHARS = 1200;

// Selectors tried, in order, to advance Outlook Web's calendar to the
// following week. Unverified against a live tenant — same cascade +
// diagnostic-dump-on-failure pattern as `clickTeamsNav` below, chosen
// deliberately because a URL scheme for an arbitrary week has never
// been confirmed to work across tenants, while a "next" control in
// the calendar toolbar is near-certain to exist regardless of build.
const NEXT_WEEK_SELECTORS = [
  'button[aria-label="Next week"]',
  'button[aria-label^="Next"]',
  'button[aria-label*="Next" i]',
  '[data-icon-name="ChevronRight"]',
  'button[title*="Next" i]',
  '[role="button"][aria-label*="Next" i]',
];

const CALENDAR_ALARM_NAME = "calendar-refresh";
const CALENDAR_REFRESH_MINUTES = 30;
// Shorter dedupe window than the four-source capture's 60 minutes —
// this store going stale for a whole hour after one missed run is
// exactly the 2026-08-13 field failure (one capture, ever, then
// nothing). A half-hour cadence with a lighter 20-minute dedupe keeps
// it current without hammering Outlook Web on every alarm tick.
const CALENDAR_DEDUP_WINDOW_MS = 20 * 60 * 1000;

// Diagnose calendar capture (v1.3, options page button): timestamps
// (from tab load) at which the diagnostic probe re-samples the page,
// so a rendering-timing problem ("nothing yet at 2s, everything by
// 10s") is distinguishable from a structural one ("still zero at
// 15s"). Requested explicitly, not derived from CALENDAR_MAX_WAIT_MS —
// this is a one-shot user-triggered diagnostic, not the retry loop
// the real capture uses.
const DIAGNOSTIC_SNAPSHOT_DELAYS_MS = [2000, 5000, 10000, 15000];

// ── Join-link probe config (diagnostic only) ────────────────────────
//
// Both NATIVE calendar backends (`services/_calendar_outlook.py:559`,
// `services/_calendar_eventkit.py:579`) get `join_url` by running
// `_extract_join_url(location, body)` over the invite BODY, which this
// scrape does not have. The extension's route is different: OWA renders
// the event's LOCATION into the aria-label, so when the organiser's
// add-in wrote the join link into Location, the URL is right there in
// the label text (see `extractUrlsFromLabel`). A Teams event renders
// the words "Microsoft Teams Meeting" and no URL, so the label route
// covers some meetings and not others.
//
// The probe below measures which, against a live tenant this
// environment can never sign in to. It looks in BOTH places — anchors
// (`a[href]`) in the same roots the real scan walks, and the aria-label
// TEXT the real parse reads — and reports them separately, because
// v1.4's probe looked only at anchors, found zero, and reported that
// join_url "cannot be filled from this DOM" while a Zoom join URL sat
// in the label text of the very same report. An anchor is reported as
// usable only if it sits inside — or next to — an element carrying a
// meeting-shaped aria-label; anything else could only be matched by
// grid position, which is how a link gets attached to the WRONG
// meeting, so those are counted and never used. A URL found in a
// meeting-shaped label needs no association at all: it is part of that
// meeting's own description.
//
// Passed to `_calendarDiagnosticProbeFunc` as ARGS (regex sources, not
// RegExp objects) for the same reason TIME_RANGE_RE.source already is:
// an injected script cannot close over this file's module scope, and
// duplicating the vocabulary inside the probe would let the two drift.

// The conferencing providers a URL must match to be a JOIN link rather
// than a location. Host AND path both have to match: `zoom.us/j/<id>`
// joins a meeting, `zoom.us/pricing` does not, and a link to some other
// site entirely is a place, not a meeting. Regex SOURCES, not RegExp
// objects, because this list is handed to an injected script as an arg
// (see JOIN_URL_PROBE_CONFIG below) and structured-cloned on the way.
//
// One list, two consumers — `extractUrlsFromLabel` (which fills
// `join_url` from label text) and the diagnostic probe — precisely so
// the field and the diagnostic that measures the field can never
// disagree about what counts as a join link.
const JOIN_PROVIDER_PATTERNS = [
  {
    name: "teams",
    host: "^(?:teams\\.microsoft\\.com|teams\\.live\\.com|teams\\.cloud\\.microsoft)$",
    path: "^/l/meetup-join/|^/l/meeting/|^/meet/",
  },
  { name: "zoom", host: "(?:^|\\.)zoom\\.us$", path: "^/(?:j|w|s|my|wc)/" },
  { name: "webex", host: "(?:^|\\.)webex\\.com$", path: "^/(?:meet|join|wbxmjs)/|/j\\.php|/m\\.php" },
  { name: "meet", host: "^meet\\.google\\.com$", path: "^/[A-Za-z0-9]" },
];

const JOIN_URL_PROBE_CONFIG = {
  providers: JOIN_PROVIDER_PATTERNS,
  // Hosts safe to print verbatim. Anything else keeps only its last two
  // labels ("acme.webex.com" -> "*.webex.com") — a Webex/Zoom site
  // subdomain is usually the CUSTOMER's name, which must not land in a
  // report the user pastes into a chat window.
  safeHosts: [
    "teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft",
    "meet.google.com", "zoom.us", "www.zoom.us", "webex.com", "www.webex.com",
  ],
  // Path segments that are structural, not identifying. Every OTHER
  // segment is elided: a join URL's identifying segments ARE the
  // meeting credential (and "zoom.us/my/<name>" is a person's handle),
  // so an example may only ever show the SHAPE of the path.
  safePathSegments: [
    "l", "meetup-join", "meeting", "meet", "j", "join", "w", "s", "my",
    "wc", "wbxmjs", "j.php", "m.php", "0",
  ],
  maxExamples: 3,
  // How far up from an anchor to look for a meeting-shaped aria-label
  // before calling it unassociated. A link matched to the WRONG meeting
  // is worse than no link, so this stays deliberately short.
  maxAncestorHops: 6,
};

// ──────────────────────────────────────────────────────────────────
// Lifecycle: re-arm alarms whenever the service worker (re)starts.
// ──────────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(async () => {
  await setupAlarms();
});

chrome.runtime.onStartup.addListener(async () => {
  await setupAlarms();
});

// Watch for the user changing schedule settings and re-arm immediately
// rather than waiting for the next browser restart.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.autoCapture || changes.captureTimes) {
    setupAlarms().catch((e) => console.warn("[ext] re-arm failed:", e));
  }
});

async function setupAlarms() {
  try {
    await chrome.alarms.clearAll();
  } catch (_) { /* ignore */ }

  // Calendar refresh is scheduled UNCONDITIONALLY — independent of the
  // `autoCapture` toggle below, which only gates the heavier four-
  // source briefing capture. The Record tab's Upcoming Meetings panel
  // depends on this store staying current even for a user who never
  // turns on that toggle; that's the entire point of
  // `calendar_source: "extension"` mode. Field report 2026-08-13: the
  // store held exactly one capture (a single manual click) and was
  // never refreshed again because nothing else was scheduling it.
  chrome.alarms.create(CALENDAR_ALARM_NAME, {
    delayInMinutes: 1,
    periodInMinutes: CALENDAR_REFRESH_MINUTES,
  });
  console.log(
    `[ext] calendar-refresh alarm scheduled (every ${CALENDAR_REFRESH_MINUTES} min)`);

  const cfg = await chrome.storage.local.get({
    autoCapture: false,
    captureTimes: DEFAULT_CAPTURE_TIMES,
  });
  if (!cfg.autoCapture) {
    console.log("[ext] auto-capture (4-source briefing) off; no briefing alarms scheduled");
    return;
  }

  for (const t of (cfg.captureTimes || [])) {
    const next = nextOccurrence(t);
    if (!next) continue;
    chrome.alarms.create(`capture-${t}`, {
      when: next.getTime(),
      periodInMinutes: 24 * 60,  // daily
    });
    console.log(`[ext] scheduled ${t} (next: ${next.toString()})`);
  }
}

function nextOccurrence(timeStr) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(timeStr || "");
  if (!m) return null;
  const hours = parseInt(m[1], 10);
  const minutes = parseInt(m[2], 10);
  if (hours > 23 || minutes > 59) return null;
  const now = new Date();
  const next = new Date(
    now.getFullYear(), now.getMonth(), now.getDate(),
    hours, minutes, 0, 0,
  );
  if (next.getTime() <= now.getTime() + 30_000) {
    // Already passed today (or within next 30s); schedule for tomorrow.
    next.setDate(next.getDate() + 1);
  }
  return next;
}

// ──────────────────────────────────────────────────────────────────
// Alarm fires → capture (with dedup) and POST.
//
// v1.3.1 — field report 2026-08-14: the calendar-refresh alarm had
// NEVER once produced a POST (backend log showed only 3 manual
// "Capture & Send" imports all day, every one carrying all four
// narrative blobs — a calendar-only alarm POST never carries those).
// Investigated against the actual MV3 contract rather than guessed:
//
//   1. Is setupAlarms() reached reliably? Yes — it runs on BOTH
//      onInstalled and onStartup, and creates CALENDAR_ALARM_NAME
//      unconditionally (not gated behind the autoCapture toggle). A
//      periodic chrome.alarms entry, once created, survives service-
//      worker suspension/restart on its own — that's the whole point
//      of the alarms API vs. setTimeout. Not the defect.
//   2. Does the handler use a stale module-scope backendUrl/token?
//      No — both branches below call `chrome.storage.local.get(...)`
//      FRESH on every single alarm fire, which is durable storage,
//      not an in-memory value that resets on a cold service-worker
//      start. Not the defect (this was the prime suspect and it does
//      not hold up against this file's actual code).
//   3. Does manifest.json declare "alarms"? Yes (see permissions).
//      Not the defect.
//   4. THIS is the defect: two early-return branches below (not-
//      configured, dedupe-skip) returned WITHOUT writing anything to
//      chrome.storage.local. A real alarm fire that hit either one
//      left literally zero trace — indistinguishable, from the
//      options page, the popup, or the backend log, from the alarm
//      never having fired at all. Combined with lastCalendarCaptureAt
//      being shared with the manual-capture dedupe timestamp (a
//      recent manual "Capture & Send" silences the next ~20 minutes
//      of alarm ticks), a user who occasionally uses the manual
//      button could see the alarm silently skip run after run with
//      no way to ever notice. captureCalendarOnly() itself already
//      wrote lastCalendarCaptureAt/lastCalendarResult on every one of
//      ITS return paths (capture failure, zero-events, fetch
//      failure) — only the guard code IN FRONT of that call was
//      silent. Fixed below: every branch of this listener now writes
//      both fields, and the call itself is wrapped so an unexpected
//      thrown error (should never happen — captureCalendarOnly
//      already catches its own body) still leaves a trace rather than
//      an unhandled rejection MV3 quietly drops.
// ──────────────────────────────────────────────────────────────────

// Persist a calendar-alarm attempt that never reached (or threw before)
// captureCalendarOnly's own storage writes, so "the alarm fired but
// was skipped/broken" is always visible next to "the alarm never fired
// at all" — see the v1.3.1 comment above.
async function recordCalendarAlarmOutcome(reason, message) {
  const result = {
    ok: false,
    skipped: reason !== "error",
    reason,
    error: message,
    ts: Date.now(),
  };
  await chrome.storage.local.set({
    lastCalendarCaptureAt: Date.now(),
    lastCalendarResult: result,
  });
  return result;
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm?.name === CALENDAR_ALARM_NAME) {
    const cfg = await chrome.storage.local.get({
      backendUrl: "", token: "", lastCalendarCaptureAt: 0,
    });
    if (!cfg.backendUrl || !cfg.token) {
      console.warn("[ext] calendar-refresh fired but extension not configured; skipping");
      await recordCalendarAlarmOutcome(
        "not-configured", "Backend URL or token not configured.");
      return;
    }
    if (Date.now() - cfg.lastCalendarCaptureAt < CALENDAR_DEDUP_WINDOW_MS) {
      const mins = Math.round((Date.now() - cfg.lastCalendarCaptureAt) / 60_000);
      console.log(`[ext] calendar-refresh dedupe: last capture ${mins} min ago, skipping`);
      await recordCalendarAlarmOutcome(
        "deduped",
        `Skipped — last calendar capture was ${mins} min ago ` +
        `(dedupe window ${CALENDAR_DEDUP_WINDOW_MS / 60_000} min).`);
      return;
    }
    try {
      await captureCalendarOnly(cfg.backendUrl, cfg.token);
    } catch (e) {
      // Defense-in-depth: captureCalendarOnly already wraps its own
      // body and writes storage on every return path, so this should
      // be unreachable — but an uncaught throw here would otherwise
      // be a silent, untraceable MV3 unhandled-rejection, exactly the
      // failure mode this whole fix exists to close.
      console.error("[ext] calendar-refresh alarm: unexpected error", e);
      await recordCalendarAlarmOutcome(
        "error", `Unexpected error: ${e.message || String(e)}`);
    }
    return;
  }

  if (!alarm?.name?.startsWith("capture-")) return;
  console.log(`[ext] alarm fired: ${alarm.name}`);

  const cfg = await chrome.storage.local.get({
    backendUrl: "",
    token: "",
    lastCaptureAt: 0,
  });

  if (!cfg.backendUrl || !cfg.token) {
    console.warn("[ext] alarm fired but extension not configured; skipping");
    return;
  }
  if (Date.now() - cfg.lastCaptureAt < DEDUPE_WINDOW_MS) {
    const mins = Math.round((Date.now() - cfg.lastCaptureAt) / 60_000);
    console.log(`[ext] alarm dedupe: last capture ${mins} min ago, skipping`);
    return;
  }

  await captureAndSend(cfg.backendUrl, cfg.token, { source: "alarm" });
});

// ──────────────────────────────────────────────────────────────────
// Popup-initiated manual capture.
// ──────────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "capture-and-send") {
    captureAndSend(msg.backendUrl, msg.token, { source: "manual" })
      .then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
    return true;
  }
  if (msg?.type === "get-status") {
    chrome.storage.local.get({
      lastCaptureAt: 0,
      lastResult: null,
      // Populated independently by the calendar-refresh alarm (and
      // also updated by a manual Capture & Send, which folds calendar
      // capture into the same click) — see captureCalendarOnly /
      // captureCalendarTab below.
      lastCalendarCaptureAt: 0,
      lastCalendarResult: null,
      autoCapture: false,
      captureTimes: DEFAULT_CAPTURE_TIMES,
    }).then(sendResponse);
    return true;
  }
  if (msg?.type === "diagnose-calendar") {
    diagnoseCalendarCapture()
      .then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
    return true;
  }
  if (msg?.type === "diagnose-calendar-api") {
    diagnoseCalendarApi()
      .then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
    return true;
  }
});

// ──────────────────────────────────────────────────────────────────
// The actual capture.
// ──────────────────────────────────────────────────────────────────

// The extension's own manifest version, sent on EVERY POST to the
// backend (see captureAndSend / captureCalendarOnly below) so the app
// can tell a stale, still-installed extension apart from a current
// one — see services/extension_calendar_service.py's
// record_extension_version. Field report: v2.28.0 shipped a new
// extension version with the app having no way to detect the OLD one
// was still what the user had loaded in Chrome. chrome.runtime is
// always available to a service worker, but wrapped defensively
// anyway — this must never be the reason a capture fails to send.
function currentExtensionVersion() {
  try {
    return chrome.runtime.getManifest().version || null;
  } catch (_) {
    return null;
  }
}

async function captureAndSend(backendUrl, token, opts = {}) {
  if (!backendUrl || !token) {
    return { ok: false, error: "Backend URL or token not configured. Open Settings." };
  }

  console.log(`[ext] starting capture (source=${opts.source || "manual"})`);

  const payload = { extension_version: currentExtensionVersion() };
  const counts = {};
  const errors = [];

  // Run each source sequentially so we don't hammer Microsoft's
  // anti-flooding with 4 simultaneous tab opens. Per-source timeouts
  // (see SOURCES at top) — Teams gets ~40s, OWA/Inbox ~25-30s.
  for (const src of SOURCES) {
    try {
      const result = await captureUrl(src);
      payload[`${src.key}_text`] = result.text;
      counts[src.key] = result.text.length;
      console.log(
        `[ext] ${src.label}: ${result.text.length} chars, ` +
        `exit=${result.exitReason}, elapsed=${result.elapsedMs}ms, ` +
        `landed_url=${result.finalUrl}`);
    } catch (e) {
      console.error(`[ext] ${src.label} failed:`, e);
      errors.push(`${src.label}: ${e.message || String(e)}`);
      payload[`${src.key}_text`] = "";
      counts[src.key] = 0;
    }
  }

  // Calendar (structured, v1.2): a manual "Capture & Send" click is
  // also the fastest way for a user to get a fresh calendar read on
  // demand, so fold it into the same flow the periodic calendar-only
  // alarm uses (captureCalendarTab). Failure here must NOT fail the
  // whole briefing capture — the four sources above are independent
  // and still useful on their own.
  let calendarCapture = null;
  try {
    calendarCapture = await captureCalendarTab();
    if (calendarCapture.events.length > 0) {
      payload.calendar_events = calendarCapture.events;
    } else if (calendarCapture.fallbackText) {
      payload.calendar_text = calendarCapture.fallbackText;
    }
    // The path the user actually presses when investigating. See
    // buildCaptureDiag for why its absence here cost two rounds.
    payload.capture_diag = buildCaptureDiag(calendarCapture);
    counts.calendar = calendarCapture.events.length;
    console.log(
      `[ext] Calendar: ${calendarCapture.events.length} event(s) ` +
      `(layer=${calendarCapture.layer || "text-fallback"}` +
      `${calendarCapture.zeroReason ? `, zeroReason=${calendarCapture.zeroReason}` : ""}, ` +
      `weeks=${calendarCapture.diag.weeksScanned}), ` +
      `elapsed=${calendarCapture.elapsedMs}ms`);
  } catch (e) {
    console.error("[ext] Calendar capture failed:", e);
    errors.push(`Calendar: ${e.message || String(e)}`);
    counts.calendar = 0;
  }

  // If literally every source returned 0 chars AND the calendar
  // capture found nothing at all, treat as a hard failure. Otherwise
  // we still POST whatever we got — even just OWA (or just the
  // calendar) is useful.
  const totalChars = SOURCES.reduce((sum, src) => sum + (counts[src.key] || 0), 0);
  const calendarHasContent = !!(payload.calendar_events || payload.calendar_text);
  if (totalChars === 0 && !calendarHasContent) {
    const result = {
      ok: false,
      error: `Every source returned nothing. ${errors.join(" | ")}`,
    };
    await chrome.storage.local.set({ lastCaptureAt: Date.now(), lastResult: result });
    return result;
  }

  // POST to the recorder backend.
  try {
    const res = await fetch(`${backendUrl}/briefing/extension-import`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      const result = {
        ok: false,
        error: `Backend returned ${res.status}: ${body.slice(0, 200)}`,
      };
      await chrome.storage.local.set({ lastCaptureAt: Date.now(), lastResult: result });
      return result;
    }
    const result = {
      ok: true,
      counts,
      calendarLayer: calendarCapture?.layer || null,
      calendarZeroReason: calendarCapture?.zeroReason || null,
      errors: errors.length ? errors : undefined,
      ts: Date.now(),
    };
    await chrome.storage.local.set({
      lastCaptureAt: Date.now(), lastResult: result,
      // The manual button is also a calendar refresh — keep the
      // dedicated calendar status (used by the popup and by the
      // calendar-refresh alarm's dedupe check) in sync with it.
      lastCalendarCaptureAt: Date.now(),
      lastCalendarResult: {
        ok: true,
        eventCount: counts.calendar || 0,
        layer: calendarCapture?.layer || null,
        zeroReason: calendarCapture?.zeroReason || null,
        stats: calendarCapture?.stats || null,
        ts: Date.now(),
      },
    });
    console.log(`[ext] captured & sent:`, counts);
    return result;
  } catch (e) {
    const result = {
      ok: false,
      error: `Couldn't reach ${backendUrl} — is Meeting Recorder running? (${e.message})`,
    };
    await chrome.storage.local.set({ lastCaptureAt: Date.now(), lastResult: result });
    return result;
  }
}

// Returns { text, exitReason, elapsedMs, finalUrl } so caller can
// log enough diagnostics to tell whether a short capture is because
// the SPA was slow (max-wait) vs. genuinely empty (stable below
// target) vs. bounced to login (finalUrl ≠ source URL).
async function captureUrl(src) {
  const tab = await chrome.tabs.create({ url: src.url, active: false });
  const tabId = tab.id;
  const start = Date.now();

  try {
    await waitForTabComplete(tabId);

    // Source-specific post-load nav. v1.0 just navigated to the URL
    // and polled — fine for OWA and Inbox. Doesn't work for Teams
    // Chat: Microsoft strips the #/chat hash fragment when it
    // redirects teams.microsoft.com → teams.cloud.microsoft, so the
    // tab lands on whatever Teams' default view is (Activity).
    // The fix is to navigate via DOM click on the chat nav button
    // after the page loads. Same approach available for other Teams
    // tabs in the future (Calls, Files) if we want them.
    if (src.key === "chat") {
      await clickTeamsNav(tabId, "chat");
    }

    let lastLen = -1;
    let stableCount = 0;
    let lastText = "";
    let exitReason = "max-wait";

    while (Date.now() - start < src.maxWaitMs) {
      let text = "";
      try {
        text = await readMainText(tabId);
      } catch (e) {
        console.warn(`[ext] ${src.label} read failed:`, e);
        exitReason = "read-error";
        break;
      }
      text = (text || "").trim();
      lastText = text;
      if (text.length >= src.targetChars) {
        exitReason = "target-reached";
        return await finish(tabId, text, exitReason, start);
      }
      if (text.length === lastLen && text.length > 0) {
        stableCount += 1;
        if (stableCount >= STABILITY_POLLS) {
          exitReason = text.length >= src.minUsefulChars
            ? "stable-useful"
            : "stable-below-floor";
          return await finish(tabId, text, exitReason, start);
        }
      } else {
        stableCount = 0;
        lastLen = text.length;
      }
      await sleep(POLL_MS);
    }
    return await finish(tabId, lastText, exitReason, start);
  } finally {
    try { await chrome.tabs.remove(tabId); } catch (_) {}
  }
}

// Reads the tab's final URL (in case it redirected) before closing.
// Useful diagnostic for "did this tab actually land on the right
// page or did it bounce to login.microsoftonline?"
async function finish(tabId, text, exitReason, start) {
  let finalUrl = "(unknown)";
  try {
    const t = await chrome.tabs.get(tabId);
    finalUrl = t.url || "(empty)";
  } catch (_) { /* tab might already be gone */ }
  return {
    text,
    exitReason,
    elapsedMs: Date.now() - start,
    finalUrl,
  };
}

// Navigate the Teams SPA to `kw` (e.g. "chat", "activity"). Microsoft
// strips hash routes on the cloud.microsoft redirect, so #/chat in
// the initial URL doesn't actually land you in chat — you land on
// whatever Teams' default view is.
//
// Strategy (in order):
//   1. Set window.location.hash directly. The router intercepts
//      hashchange and renders the matching view. This is the
//      cheapest and most reliable approach — it doesn't depend on
//      Microsoft's button DOM staying stable.
//   2. Try a broad list of selectors as a fallback for builds where
//      the hash route doesn't trigger a re-render.
//   3. If everything fails, dump a diagnostic of visible data-tid
//      / aria-label values to the worker console so we can SEE what
//      Microsoft is shipping today and update the selectors.
async function clickTeamsNav(tabId, kw) {
  // chat is served by both `#/chat` and `#/conversations` in
  // different Teams builds; try the friendlier one first.
  const hashes = kw === "chat"
    ? ["#/chat", "#/conversations"]
    : [`#/${kw}`];

  for (const h of hashes) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        args: [h],
        func: (h) => {
          try {
            if (window.location.hash !== h) {
              window.location.hash = h;
              // hashchange listeners are sync but the router's
              // re-render is async. Caller sleeps after this.
            }
          } catch (_) { /* ignore */ }
        },
      });
    } catch (e) {
      console.warn(`[ext] Teams hash nav to ${h} failed:`, e);
    }
  }
  await sleep(2000);

  // Did the hash actually stick? If so, the router re-rendered and
  // we're done — no need to fight the DOM for a click.
  try {
    const urlNow = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => window.location.hash || "",
    });
    const hashNow = urlNow?.[0]?.result || "";
    if (hashes.includes(hashNow)) {
      console.log(`[ext] Teams nav '${kw}' via hash ${hashNow}`);
      await sleep(2000);  // let the view paint
      return true;
    }
  } catch (_) { /* ignore — fall through to clicker */ }

  const Kw = kw.charAt(0).toUpperCase() + kw.slice(1);  // "Chat", "Activity"
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt > 0) await sleep(1500);
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId },
        args: [kw, Kw],
        func: (kw, Kw) => {
          // Selectors are tried in order. data-tid is Microsoft's
          // own test-id and tends to be most stable, then aria,
          // then class/href fallbacks.
          const selectors = [
            // data-tid patterns Microsoft has used for the left
            // sidebar in different Teams builds:
            `[data-tid="app-bar-${kw}"]`,
            `[data-tid="left-nav-${kw}"]`,
            `[data-tid="${kw}-tab"]`,
            `[data-tid="app-bar-${kw}-button"]`,
            `[data-tid="appBar-${kw}"]`,
            `[data-tid*="${kw}-bar"]`,
            `[data-tid*="${kw}"]`,
            `[id="app-bar-${kw}"]`,
            // Aria patterns — Microsoft frequently localizes
            // visible text but keeps aria-label English for screen
            // readers. Case-insensitive (`i` flag in attr selector)
            // catches "Chat" / "chat" / "Chats" variants.
            `button[aria-label="${Kw}"]`,
            `button[aria-label^="${Kw}"]`,
            `button[aria-label*="${Kw}" i]`,
            `[role="tab"][aria-label="${Kw}"]`,
            `[role="tab"][aria-label*="${Kw}" i]`,
            `[role="treeitem"][aria-label*="${Kw}" i]`,
            `button[title="${Kw}"]`,
            `button[title*="${Kw}" i]`,
            // Hash-based anchor (some Teams builds render the
            // sidebar as <a href="#/chat">):
            `a[href*="#/${kw}"]`,
            `a[href*="/${kw}"]`,
            `a[href*="conversations"]`,
            `[role="link"][href*="${kw}"]`,
          ];
          for (const sel of selectors) {
            let els;
            try { els = document.querySelectorAll(sel); }
            catch (_) { continue; }
            for (const el of els) {
              // offsetParent === null means the element is hidden
              // (display:none or collapsed sidebar). Skip those.
              if (!el || el.offsetParent === null) continue;
              try {
                el.click();
                return sel;
              } catch (_) { /* keep trying others */ }
            }
          }
          return null;
        },
      });
      const clicked = result?.[0]?.result;
      if (clicked) {
        console.log(`[ext] Teams nav '${kw}' clicked via ${clicked} (attempt ${attempt + 1})`);
        await sleep(3000);
        return true;
      }
    } catch (e) {
      console.warn(`[ext] Teams nav '${kw}' click attempt ${attempt + 1} failed:`, e);
    }
  }

  // Final fallback: dump a list of the visible test-ids and
  // aria-labels we DID see so we can extend the selectors next
  // round. Without this we keep guessing in the dark.
  try {
    const diag = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const out = { tids: [], arias: [], hashNow: window.location.hash || "" };
        for (const el of document.querySelectorAll("[data-tid]")) {
          if (!el.offsetParent) continue;
          const v = el.getAttribute("data-tid");
          if (v && !out.tids.includes(v)) out.tids.push(v);
          if (out.tids.length >= 60) break;
        }
        for (const el of document.querySelectorAll("[aria-label]")) {
          if (!el.offsetParent) continue;
          const v = el.getAttribute("aria-label");
          if (v && !out.arias.includes(v)) out.arias.push(v);
          if (out.arias.length >= 60) break;
        }
        return out;
      },
    });
    const d = diag?.[0]?.result;
    if (d) {
      console.warn(`[ext] Teams nav '${kw}' diag — hash=${d.hashNow}`);
      console.warn(`[ext] visible data-tid (${d.tids.length}):`, d.tids.join(" | "));
      console.warn(`[ext] visible aria-label (${d.arias.length}):`, d.arias.join(" | "));
    }
  } catch (_) { /* diag is best-effort */ }

  console.warn(`[ext] couldn't navigate Teams to '${kw}' — extracting whatever's on screen`);
  return false;
}

function waitForTabComplete(tabId) {
  return new Promise((resolve) => {
    const start = Date.now();
    const t = setInterval(async () => {
      let tab;
      try {
        tab = await chrome.tabs.get(tabId);
      } catch (_) {
        clearInterval(t);
        resolve();
        return;
      }
      if (tab.status === "complete" || Date.now() - start > 15000) {
        clearInterval(t);
        // Give the SPA a beat after onload to start mounting.
        setTimeout(resolve, 500);
      }
    }, 250);
  });
}

async function readMainText(tabId) {
  // outlook.cloud.microsoft and teams.cloud.microsoft don't always
  // wrap real content in [role="main"] — they use a different
  // shell layout where [role="main"] is a sparse wrapper. Try
  // several semantic landmarks in order and pick the LARGEST
  // result so we don't accidentally extract from an outer chrome
  // element that only contains nav.
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      try {
        const selectors = [
          '[role="main"]',
          'main',
          '[role="grid"]',           // calendar grid, inbox list
          '[role="feed"]',           // Teams Activity uses feed role
          '[role="region"]',
          '[data-app-section]',      // OWA / outlook.cloud
          '#mainPaneContainer',      // outlook.cloud.microsoft mail body
          '#app',                    // teams.cloud.microsoft shell
        ];
        let best = "";
        let bestSel = "";
        for (const sel of selectors) {
          const els = document.querySelectorAll(sel);
          for (const el of els) {
            const t = (el?.innerText || "").trim();
            if (t.length > best.length) {
              best = t;
              bestSel = sel;
            }
          }
        }
        // Fallback to body if no semantic landmark beat 100 chars —
        // sometimes the only useful content IS in plain divs.
        if (best.length < 100) {
          const bodyText = (document.body?.innerText || "").trim();
          if (bodyText.length > best.length) {
            best = bodyText;
            bestSel = "body";
          }
        }
        // Stash the winning selector on window for inline debugging
        // (visible via the extension's service-worker console only
        // if we re-execute and read it back; mostly here for future
        // me when this needs tweaking).
        return best;
      } catch (e) {
        return "";
      }
    },
  });
  return (results?.[0]?.result) || "";
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ──────────────────────────────────────────────────────────────────
// Calendar structured extraction (v1.2).
//
// Deliberately split into two halves with a hard boundary between
// them, because only one half can be exercised without a live
// Outlook Web session:
//
//   DOM SCAN (below, inside `chrome.scripting.executeScript` calls) —
//     browser-only. Walks the live page, decides which elements carry
//     a meeting-shaped aria-label, and does its best to attach a date
//     to labels that don't state one. Cannot be unit tested here; not
//     verified against a real Outlook Web tenant in this change.
//
//   PARSE + DEDUPE (parseMeetingLabel / extractEventsFromCandidates,
//     further down) — pure functions, zero DOM/chrome references.
//     They take the DOM scan's plain-data output ({label,
//     columnDateIso, layer} records) and turn it into events. This is
//     the half that carries the correctness burden, and it's the half
//     the test fixtures exercise directly.
//
// WHY aria-label and not a CSS/data-attribute selector: Outlook Web's
// markup (class names, data-automationid, DOM nesting) varies by
// tenant and rollout ring and changes without notice, but the
// calendar MUST expose each event's subject and time through
// accessible text or screen readers break. Microsoft can silently
// rename a CSS class; it cannot silently break that contract. Scanning
// EVERY element carrying an aria-label and keeping the ones whose
// label is meeting-shaped (contains a time range) is therefore the
// most tenant-agnostic signal available — no selector to go stale.

// ── Pure: date/time-atom parsing ────────────────────────────────────

const _MONTH_INDEX = {
  jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
  jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
};
const _WEEKDAY_RE_SRC = "(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)";
const _MONTH_RE_SRC = "(?:January|February|March|April|May|June|July|August|" +
  "September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)";
// One date atom: "August 17", "August 17, 2026", "Monday, August 17".
const _DATE_ATOM_RE_SRC =
  `\\b(?:${_WEEKDAY_RE_SRC},?\\s+)?(${_MONTH_RE_SRC})\\.?\\s+(\\d{1,2})(?:,?\\s*(\\d{4}))?\\b`;
// One clock time: "10 AM", "10:00 AM", "10:00am" (meridiem required),
// or "14:00" (24h, colon required). Deliberately does NOT accept a
// bare number ("3 to 5") — that's the single biggest false-positive
// risk when scanning every aria-label on the page for "looks like a
// time range".
const _CLOCK_12H_RE_SRC = "\\d{1,2}(?::\\d{2})?\\s*[AaPp]\\.?[Mm]\\.?";
const _CLOCK_24H_RE_SRC = "\\d{1,2}:\\d{2}";
const _TIME_ATOM_RE_SRC = `(?:${_CLOCK_12H_RE_SRC}|${_CLOCK_24H_RE_SRC})`;

// Full range: optional date + time ... separator ... optional date +
// time. The optional per-side date handles a multi-day event whose
// label spells out both ends ("August 17, 9:00 AM to August 19,
// 5:00 PM"); the common case (no date in the label at all) relies on
// the caller-supplied columnDateIso instead — see resolveDateParts.
const TIME_RANGE_RE = new RegExp(
  `(${_DATE_ATOM_RE_SRC})?,?\\s*(${_TIME_ATOM_RE_SRC})` +
  `\\s*(?:to|until|-|–|—)\\s*` +
  `(${_DATE_ATOM_RE_SRC})?,?\\s*(${_TIME_ATOM_RE_SRC})`,
  "i"
);
// NOTE: the regex above has MORE capture groups than the 4 logical
// fields (each date atom itself captures month/day/year) — see
// _unpackTimeRangeMatch, which reads them out by fixed offset.

const ALL_DAY_RE = /\ball[\s-]?day\b/i;

function _unpackTimeRangeMatch(m) {
  // Group layout for TIME_RANGE_RE: 1=date1(full), 2=month1, 3=day1,
  // 4=year1, 5=time1, 6=date2(full), 7=month2, 8=day2, 9=year2, 10=time2.
  return {
    date1: m[1] ? { month: m[2], day: m[3], year: m[4] } : null,
    time1: m[5],
    date2: m[6] ? { month: m[7], day: m[8], year: m[9] } : null,
    time2: m[10],
    index: m.index,
  };
}

function parseClockAtom(raw) {
  const s = (raw || "").trim();
  let m = /^(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?$/.exec(s);
  if (m) {
    let hour = parseInt(m[1], 10);
    const minute = m[2] ? parseInt(m[2], 10) : 0;
    if (hour === 12) hour = 0;
    if (m[3].toLowerCase() === "p") hour += 12;
    if (hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59) return { hour, minute };
    return null;
  }
  m = /^(\d{1,2}):(\d{2})$/.exec(s);
  if (m) {
    const hour = parseInt(m[1], 10);
    const minute = parseInt(m[2], 10);
    if (hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59) return { hour, minute };
  }
  return null;
}

function _dateAtomToParts(atom, fallbackYear) {
  if (!atom) return null;
  const monthKey = String(atom.month || "").slice(0, 3).toLowerCase();
  const month = _MONTH_INDEX[monthKey];
  if (month === undefined) return null;
  const day = parseInt(atom.day, 10);
  if (!(day >= 1 && day <= 31)) return null;
  const year = atom.year ? parseInt(atom.year, 10) : fallbackYear;
  return { year, month, day };
}

// A date atom ANYWHERE in the label, not just immediately hugging a
// time. Outlook Web writes the date AFTER the time range:
//
//   "Globex, 8:30 AM to 9:00 AM, Friday, August 14, 2026, ..."
//
// TIME_RANGE_RE only captures a date atom directly preceding each time
// atom, so on a real week view it matched the times and saw no date at
// all — every one of the 28 meetings on the page then fell through to
// columnDateIso, and Outlook Web's week grid exposes no
// `role="columnheader"` elements to resolve one from. Result: 47
// "unresolved date/time" candidates carrying a fully-qualified date in
// their own text. Searching the whole label recovers it.
const DATE_ANYWHERE_RE = new RegExp(_DATE_ATOM_RE_SRC, "i");

// Tries each text in order and returns the first date atom found.
// Callers pass the text AFTER the time range first, because that's
// where Outlook Web puts the real date — searching the whole label
// first would let a month name inside a subject ("August Planning
// Review, 9:00 AM to 10:00 AM, Monday, August 10, 2026") win over the
// event's actual date.
function dateAtomAnywhere(texts) {
  for (const t of texts) {
    if (!t) continue;
    const m = DATE_ANYWHERE_RE.exec(t);
    if (m) return { month: m[1], day: m[2], year: m[3] };
  }
  return null;
}

// Date resolution order (explicit, per spec): (1) an explicit date
// atom captured beside the time, (2) a date atom anywhere else in the
// same label — the label is describing this one event, so its own date
// outranks any ancestor guess, (3) the columnDateIso the DOM layer
// resolved from ancestor/column context, (4) nothing — the caller
// drops the event rather than guess.
function resolveDateParts(dateAtom, columnDateIso, fallbackYear, labelTexts) {
  const fromLabel = _dateAtomToParts(dateAtom, fallbackYear);
  if (fromLabel) return fromLabel;
  const fromAnywhere = _dateAtomToParts(
    dateAtomAnywhere(labelTexts || []), fallbackYear);
  if (fromAnywhere) return fromAnywhere;
  if (columnDateIso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(columnDateIso);
    if (m) return { year: +m[1], month: +m[2] - 1, day: +m[3] };
  }
  return null;
}

function _buildLocalDate(dateParts, timeParts) {
  if (!dateParts || !timeParts) return null;
  const d = new Date(dateParts.year, dateParts.month, dateParts.day,
    timeParts.hour, timeParts.minute, 0, 0);
  return isNaN(d.getTime()) ? null : d;
}

function toNaiveIsoString(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// Parse ONE candidate label into a result object:
//   { kind: "event", subject, startIso, endIso }
//   { kind: "all-day", subject }          — recognized, deliberately excluded
//   { kind: "date-unresolved", subject }  — meeting-shaped but no date anywhere
//   { kind: "not-meeting-shaped" | "empty" }
//
// `columnDateIso` ("YYYY-MM-DD" or null) is whatever the DOM layer
// resolved for this element's calendar column/day — see the header
// comment for the trust order. Pure: no DOM, no I/O, no `Date.now()`
// except through the caller-supplied fallbackYear.
function parseMeetingLabel(label, columnDateIso, fallbackYear) {
  const raw = String(label || "").replace(/\s+/g, " ").trim();
  if (!raw) return { kind: "empty" };

  const m = TIME_RANGE_RE.exec(raw);
  if (!m) {
    if (ALL_DAY_RE.test(raw)) {
      const subject = raw.replace(ALL_DAY_RE, "").replace(/[,;\s]+$/, "").trim();
      return { kind: "all-day", subject: subject || raw };
    }
    return { kind: "not-meeting-shaped" };
  }

  const parts = _unpackTimeRangeMatch(m);
  const subject = raw.slice(0, parts.index).replace(/[,;\s]+$/, "").trim();
  if (!subject) return { kind: "not-meeting-shaped" };

  const year = fallbackYear || new Date().getFullYear();
  // Where to look for a date the time-range match didn't capture: the
  // text after the range first (Outlook Web's own position for it),
  // then the whole label as a last resort.
  const dateTexts = [raw.slice(parts.index + m[0].length), raw];
  const startDateParts = resolveDateParts(parts.date1, columnDateIso, year, dateTexts);
  // An end date only falls back to the *start* date (never straight
  // to columnDateIso) when the label named its own start date — that
  // label is telling us the event isn't just "today", so borrowing
  // columnDateIso for the end would silently truncate a multi-day
  // span back down to one day.
  const endDateParts = parts.date2
    ? _dateAtomToParts(parts.date2, year)
    : (parts.date1 ? startDateParts
      : resolveDateParts(null, columnDateIso, year, dateTexts));

  const startTime = parseClockAtom(parts.time1);
  const endTime = parseClockAtom(parts.time2);
  if (!startDateParts || !startTime || !endDateParts || !endTime) {
    return { kind: "date-unresolved", subject };
  }

  const start = _buildLocalDate(startDateParts, startTime);
  const end = _buildLocalDate(endDateParts, endTime);
  if (!start || !end) return { kind: "date-unresolved", subject };

  return { kind: "event", subject, startIso: toNaiveIsoString(start), endIso: toNaiveIsoString(end) };
}

// ── Pure: organizer out of the same label tail ───────────────────────
//
// Outlook Web writes the organizer into the tail that follows the time
// range — the SAME tail `resolveDateParts`/`dateAtomAnywhere` already
// read the date out of:
//
//   "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026,
//    Microsoft Teams Meeting, By Jane Doe, Busy"
//                              ^^^^^^^^^^^^
//
// Before this, `extractEventsFromCandidates` declared an `organizer`
// field on every captured event and nothing ever assigned it — the
// field shipped in the schema, through the backend's
// `events_from_structured`, into the store, always "". This fills it
// from data already being scanned. Nothing about candidate discovery,
// `parseMeetingLabel`, `TIME_RANGE_RE` or the date resolution changes:
// this reads the same string a second time, and every failure mode
// returns "" so an event is exactly what it was before.
//
// Why the organizer is worth having beyond display: the follow-up
// draft path (`backend/services/follow_up_recipients.py`) resolves an
// address out of a directory far more often from a two-token name than
// from the bare first name an action item usually carries, and the
// organizer is a full name.

// The organizer segment starts a comma-delimited segment of the tail
// (or the tail itself). Anchoring on the segment boundary is what keeps
// a subject like "Design Review by the numbers" from reading as one —
// a subject sits BEFORE the time range and is never in the tail at all,
// but the no-time-range path below falls back to the whole label.
const ORGANIZER_TAIL_RE = /(?:^|,)\s*by\s+/i;
const ORGANIZER_ANYWHERE_RE = /,\s*by\s+/i;

// Whole segments that are show-as / recurrence / cancellation status,
// not part of anybody's name. Matched case-insensitively against a
// WHOLE trimmed segment — never as a substring, so a person whose name
// merely contains one of these words is unaffected.
const ORGANIZER_STATUS_SEGMENTS = new Set([
  "busy", "free", "tentative", "away", "out of office", "oof",
  "working elsewhere", "unknown", "private",
  "recurring event", "exception to recurring event",
  "occurrence of a recurring event", "series",
  "canceled", "cancelled", "canceled event", "cancelled event",
  "declined", "accepted", "no response", "not responded", "organizer",
  "all day", "all-day",
  "microsoft teams meeting", "skype meeting", "teams meeting",
  "zoom meeting", "webex meeting", "google meet",
]);

function _isOrganizerStatusSegment(seg) {
  return ORGANIZER_STATUS_SEGMENTS.has(
    String(seg || "").trim().replace(/[.\s]+$/, "").toLowerCase());
}

// An organiser segment that is an SMTP ADDRESS rather than a display
// name — the shape a tenant writes when it has no resolved display name
// for the organiser (an external organiser, a distribution list, a
// room). Field report 2026-08-19: nine of ten follow-up drafts went out
// with no recipient because the only thing the app had for a person was
// a bare first name, which a corporate directory will not resolve. An
// address needs no resolving at all, so when the label hands us one it
// is kept EXACTLY as written — not title-cased, not split, not
// "cleaned". `services/follow_up_recipients.py` is the consumer, and
// unchanged by this file: it currently treats every organiser label as
// a NAME (`candidate_names` -> `_prettify` title-cases a lowercase
// one), so recognising an address as already-resolved is a change that
// belongs in that module, not here. Passing the address through
// verbatim is the half this side owes it.
//
// Deliberately structural and narrow: one `@`, a dot-bearing domain, no
// whitespace or segment punctuation on either side. It is a
// classification, not a validator — the address is passed through
// whether or not it matches; matching only decides that the surname-
// first join below must NOT fire for it.
const ORGANIZER_EMAIL_RE =
  /^[^\s@,;:<>()[\]]+@[^\s@,;:<>()[\]]+\.[A-Za-z]{2,}$/;

// A bare token that could be the SURNAME half of a "Last, First" split:
// letters (any script), plus the punctuation that lives inside real
// surnames. Deliberately excludes anything with a digit or an `@`, so
// an address (`a.doe@globex.example`) and an id-bearing room or
// distribution-list name are never glued to the segment after them.
const ORGANIZER_SURNAME_TOKEN_RE = /^[\p{L}][\p{L}'’.\-]*$/u;

// The segment that may be joined BACK onto a bare surname as the given
// name: starts with a letter and carries no digit and no `@`. Keeps
// "Pat Jr. [US-US]" (a real given-name segment with a suffix and a
// bracketed region) while rejecting "Umbrella HQ Room 3" (a place that
// merely follows the organiser in the tail).
const ORGANIZER_GIVEN_SEGMENT_RE = /^[\p{L}][^@\d]*$/u;

// Longest an organizer name is allowed to be before we assume the
// segment split went wrong and report nothing rather than a sentence.
const ORGANIZER_MAX_LEN = 80;

// Pull the organizer out of ONE candidate label. Returns "" for every
// shape it doesn't confidently recognize — an absent segment, a
// segment that is only status words, a runaway length, or a throw.
//
// The comma problem: the name itself routinely CONTAINS commas
// ("Roe, Pat Jr. [US-US]", "Noh, Kim"), so the tail cannot simply be
// split on comma and the first piece taken — that truncates real
// surnames, the same failure `owner_service.split_owners` deliberately
// avoids (see `test_owner_service.py::test_comma_is_not_split`). The
// rule instead: take the first segment; join the SECOND one to it only
// when the first is a single bare token (the "Last, First Suffix
// [REGION]" form), and stop at the first status segment either way.
//
// The organiser is not always a person, and not always a name. Every
// shape below turned up in real capture output (v1.5):
//
//   By a.doe@globex.example   an SMTP address where a display name
//                             normally sits — kept verbatim, never
//                             joined to the following segment
//   By Noh, Kim               surname-first, comma inside the name
//   By Jane  Doe              a double space inside the name —
//                             collapsed, like every other run of
//                             whitespace in the label
//   By Zoë Døe                non-ASCII, preserved byte for byte
//   By Northwind Evite        a distribution list, not a person
//   By Umbrella HQ Room 3     a room — a digit in it must not stop it
//                             being read, only stop it being glued
//                             onto a preceding bare token
function extractOrganizerFromLabel(label) {
  try {
    const raw = String(label || "").replace(/\s+/g, " ").trim();
    if (!raw) return "";

    // Prefer the tail after the time range — the position Outlook Web
    // actually uses. A candidate whose time came from a `<time
    // datetime>` pair has no text range in its label at all; for those
    // fall back to the whole label, but require the stricter
    // comma-anchored form so a subject can't be mistaken for a name.
    const m = TIME_RANGE_RE.exec(raw);
    const searchIn = m ? raw.slice(m.index + m[0].length) : raw;
    const marker = (m ? ORGANIZER_TAIL_RE : ORGANIZER_ANYWHERE_RE).exec(searchIn);
    if (!marker) return "";

    const rest = searchIn.slice(marker.index + marker[0].length);
    const segments = rest.split(",").map((s) => s.trim()).filter(Boolean);
    if (!segments.length) return "";
    if (_isOrganizerStatusSegment(segments[0])) return "";

    // An address is complete on its own. The "Last, First" join below
    // must not fire for one: `a.doe@globex.example` is a single bare
    // token, so the pre-v1.5 rule would have glued whatever segment
    // followed onto it ("a.doe@globex.example, Umbrella HQ Room 3")
    // whenever that segment wasn't a known status word. A `mailto:`
    // prefix, if OWA ever writes one, is dropped — the address itself
    // is what a directory and a To: field want.
    let name = segments[0].replace(/^mailto:/i, "").trim();
    const isEmail = ORGANIZER_EMAIL_RE.test(name);

    // "Last, First Suffix [REGION]" — the ONE case where the tail's own
    // comma is part of the name rather than a segment boundary. Both
    // halves have to look the part: a bare surname-shaped token, then a
    // segment that could be a given name. Anything else (an address, a
    // room with a number in it, a distribution list) is taken as-is,
    // because gluing the next segment onto it invents a name nobody has.
    if (!isEmail
        && ORGANIZER_SURNAME_TOKEN_RE.test(name)
        && segments[1]
        && !_isOrganizerStatusSegment(segments[1])
        && ORGANIZER_GIVEN_SEGMENT_RE.test(segments[1])) {
      name = `${name}, ${segments[1]}`;
    }
    name = name.replace(/[,;\s]+$/, "").trim();
    if (!name || name.length > ORGANIZER_MAX_LEN) return "";
    // A clock time surviving into the "name" means the segmentation
    // went somewhere unexpected; report nothing rather than garbage.
    if (/\d{1,2}:\d{2}/.test(name)) return "";
    return name;
  } catch (_) {
    return "";
  }
}

// ── Pure: join URL / location URL out of the same label tail (v1.5) ──
//
// Third read of the same string. Outlook Web renders the event's
// LOCATION field into the label, in the segment between the date and
// the `By <organiser>` segment:
//
//   "Onboarding call, 9:00 AM to 9:30 AM, Friday, August 14, 2026,
//    https://zoom.us/j/0000000000?pwd=…, By Jane Doe, Busy"
//                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//
// so whether a join URL is available comes down to whether the
// ORGANISER'S client put one in Location. Zoom's Outlook add-in does.
// Teams does not — a Teams event's Location reads "Microsoft Teams
// Meeting", the words, and the URL only ever lives in the invite body,
// which this scrape has never had. Both outcomes are normal and neither
// is a failure; `stats.withJoinUrl` is what tells the two apart.
//
// THE LINE BETWEEN A JOIN LINK AND A LOCATION URL — the one decision
// this function exists to make. `join_url` is a promise that following
// the URL puts you IN this meeting. Only a recognised conferencing
// provider (JOIN_PROVIDER_PATTERNS: Teams meetup-join, Zoom, Webex,
// Google Meet — host AND path both matching) can keep that promise. A
// real capture also carried an `https://…/library/…` training-site link
// in the very same Location position: that is where the meeting is
// ABOUT something, or at most where to go — following it does not join
// anything. Treating it as a join link would put a "Join" button in
// front of the user that silently goes somewhere else, which is worse
// than the empty field this replaces. So a non-conferencing URL is
// returned SEPARATELY, as `locationUrl`, and the caller puts it in
// `location` (also empty until now on this path) — never in `join_url`.
// An unrecognised conferencing provider therefore degrades to a
// location, not to a wrong join link.
//
// Never logged. A join URL with its `?pwd=` is a single-use credential;
// it goes into the event record and nowhere else. Diagnostics count and
// classify, never print (see `stats` in extractEventsFromCandidates and
// `redact` in the probe).

// A URL inside a comma-delimited label segment. Stops at whitespace and
// at the `,` / `;` that end the segment, so the trailing ", By <name>"
// can never be swallowed into the URL.
const LABEL_URL_RE_SRC = "https?://[^\\s,;]+";

// Beyond this a "URL" is not a URL, it is a parse that went wrong.
const LABEL_URL_MAX_LEN = 2000;

// Which provider (if any) this URL joins a meeting on. null for a URL
// that is merely a place. Returns the provider NAME only — the URL
// itself never goes anywhere but the event record.
function joinProviderForUrl(rawUrl) {
  try {
    const u = new URL(String(rawUrl || ""));
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    for (const p of JOIN_PROVIDER_PATTERNS) {
      if (new RegExp(p.host, "i").test(u.host)
          && new RegExp(p.path, "i").test(u.pathname || "")) {
        return p.name;
      }
    }
    return null;
  } catch (_) {
    return null;
  }
}

// Pull the FIRST conferencing join URL and the FIRST other URL out of
// one candidate label. Returns { joinUrl, joinProvider, locationUrl },
// each "" when that shape isn't present — so a label with no URL at all
// (the overwhelming majority, and every Teams-only calendar) produces
// exactly the values the field already had.
function extractUrlsFromLabel(label) {
  const empty = { joinUrl: "", joinProvider: "", locationUrl: "" };
  try {
    const raw = String(label || "").replace(/\s+/g, " ").trim();
    // Cheap reject first: this runs on every scanned candidate, and
    // most labels have no URL in them at all.
    if (!raw || !/https?:\/\//i.test(raw)) return empty;

    // Same region as the organiser: the tail after the time range,
    // which is where OWA puts Location. A candidate whose time came
    // from a `<time datetime>` pair has no text range to slice at, so
    // for those the whole label is searched — a URL in a subject is
    // still that meeting's own link, unlike a stray word "by".
    const m = TIME_RANGE_RE.exec(raw);
    const searchIn = m ? raw.slice(m.index + m[0].length) : raw;

    const out = { ...empty };
    const re = new RegExp(LABEL_URL_RE_SRC, "gi");
    let hit;
    while ((hit = re.exec(searchIn)) !== null) {
      // Trailing sentence punctuation belongs to the label, not the URL.
      const url = hit[0].replace(/[.,;:!?)\]}>"']+$/, "");
      if (!url || url.length > LABEL_URL_MAX_LEN) continue;
      const provider = joinProviderForUrl(url);
      if (provider) {
        if (!out.joinUrl) { out.joinUrl = url; out.joinProvider = provider; }
      } else if (!out.locationUrl) {
        out.locationUrl = url;
      }
      if (out.joinUrl && out.locationUrl) break;
    }
    return out;
  } catch (_) {
    return empty;
  }
}

// Given a candidate's structured start/end (raw strings straight off
// a `<time datetime>` or `data-start`/`data-end`-style attribute — see
// `findExternalTime` in the DOM scan below), build real Date objects.
// Pure. Deliberately does NOT accept a bare time-of-day here (e.g.
// "10:00 AM") — `new Date("10:00 AM")` silently resolves against
// TODAY in the runtime's local timezone, which would be quietly wrong
// for the "next week" scan. Only full datetime strings a `<time
// datetime="...">` attribute is expected to carry are accepted; a bare
// time-of-day found via text (sibling/ancestor/descendant) instead
// takes the OTHER path — merged into the label and resolved through
// parseMeetingLabel/columnDateIso, same as any other text time range.
function parseStructuredTimePair(startRaw, endRaw) {
  if (!startRaw || !endRaw) return null;
  const start = new Date(startRaw);
  const end = new Date(endRaw);
  if (isNaN(start.getTime()) || isNaN(end.getTime())) return null;
  return { start, end };
}

// Parse a candidate that arrived with structuredStart/structuredEnd
// (a datetime-pair the DOM layer found on a `<time>` descendant or
// datetime/data-* attribute, rather than as text in the label) into
// the same result shape parseMeetingLabel returns, so callers don't
// need to care which path produced it.
function parseStructuredCandidate(c) {
  const subject = String((c && c.label) || "").replace(/\s+/g, " ").trim();
  if (!subject) return { kind: "empty" };
  const pair = parseStructuredTimePair(c.structuredStart, c.structuredEnd);
  if (!pair) return { kind: "date-unresolved", subject };
  return {
    kind: "event",
    subject,
    startIso: toNaiveIsoString(pair.start),
    endIso: toNaiveIsoString(pair.end),
  };
}

// Turn a list of DOM-scan candidates ({label, columnDateIso, layer,
// structuredStart?, structuredEnd?}) into deduped structured events +
// stats. Pure. Dedup key is (normalized subject, start) — Outlook Web
// routinely renders the same event's aria-label on several nested
// elements (the tile, its title span, its time span, ...), so the
// SAME event commonly yields several candidates that must collapse to
// one.
function extractEventsFromCandidates(candidates, opts = {}) {
  const fallbackYear = opts.fallbackYear || new Date().getFullYear();
  const stats = {
    scanned: candidates.length,
    parsed: 0,
    allDay: 0,
    notMeetingShaped: 0,
    dateUnresolved: 0,
    deduped: 0,
    layerCounts: {},
    // How many of the kept events actually carry each of the two
    // fields that used to ship declared-but-never-assigned. Counted so
    // "populated" and "silently empty" are never the same number in
    // the popup or the logs — see the popup's calendar status line.
    withOrganizer: 0,
    withJoinUrl: 0,
    // v1.5, counts and classifications ONLY — no URL and no organiser
    // string is ever put in these, or anywhere else that gets logged.
    // `joinUrlByProvider` is what makes "this tenant is Teams-only, so
    // no label carries a URL" distinguishable from "extraction broke";
    // `withLocationUrl` counts the non-conferencing Location URLs that
    // were deliberately NOT called join links.
    joinUrlByProvider: {},
    withLocationUrl: 0,
    withOrganizerEmail: 0,
  };
  const seen = new Map();

  for (const c of candidates || []) {
    const r = (c && c.structuredStart)
      ? parseStructuredCandidate(c)
      : parseMeetingLabel(c && c.label, (c && c.columnDateIso) || null, fallbackYear);
    if (r.kind === "all-day") { stats.allDay++; continue; }
    if (r.kind === "not-meeting-shaped" || r.kind === "empty") { stats.notMeetingShaped++; continue; }
    if (r.kind === "date-unresolved") { stats.dateUnresolved++; continue; }

    const key = `${r.subject.toLowerCase()}|${r.startIso}`;
    if (seen.has(key)) { stats.deduped++; continue; }

    const layer = (c && c.layer) || "unknown";
    const organizer = (c && c.organizer) || extractOrganizerFromLabel(c && c.label);
    // Third read of the same label — see extractUrlsFromLabel. A
    // DOM-supplied value, if one is ever added, still wins.
    const urls = extractUrlsFromLabel(c && c.label);
    const joinUrl = (c && c.joinUrl) || urls.joinUrl;
    const location = (c && c.location) || urls.locationUrl;
    if (organizer) stats.withOrganizer++;
    if (organizer && ORGANIZER_EMAIL_RE.test(organizer)) stats.withOrganizerEmail++;
    if (joinUrl) {
      stats.withJoinUrl++;
      const p = (c && c.joinUrl) ? (joinProviderForUrl(joinUrl) || "other") : urls.joinProvider;
      stats.joinUrlByProvider[p] = (stats.joinUrlByProvider[p] || 0) + 1;
    }
    if (location && urls.locationUrl && location === urls.locationUrl) stats.withLocationUrl++;
    seen.set(key, {
      subject: r.subject,
      start: r.startIso,
      end: r.endIso,
      // Was always "" unless the DOM layer supplied one (nothing does
      // today). A NON-conferencing URL in the label's Location position
      // lands here rather than in join_url — it is a place, not a
      // meeting to join. See extractUrlsFromLabel.
      location,
      // Was always "" — nothing ever assigned `c.organizer`. Now
      // recovered from the label's own tail (see
      // extractOrganizerFromLabel); a DOM-supplied value, if one is
      // ever added, still wins. Extraction failure returns "", i.e.
      // exactly the value this field always had.
      organizer,
      // v1.5: filled from the label's own text when the organiser's
      // add-in wrote a RECOGNISED CONFERENCING link into the event's
      // Location (Zoom's does; a Teams event carries only the words
      // "Microsoft Teams Meeting"), and left "" — exactly the value it
      // always had — otherwise. A non-conferencing URL never reaches
      // this field; it is a location and goes in `location`.
      // v1.7: also filled from Outlook's OWN calendar response when
      // one carried a join URL for this meeting (the Teams case, whose
      // URL is in the invite body and never in the label). See
      // mergeDetailIntoEvents.
      join_url: joinUrl,
      // The invite body / agenda. Empty here and filled by
      // mergeDetailIntoEvents when Outlook's response carried it —
      // the grid has never rendered a description, which is why the
      // Record tab said "(No description on this invite.)" on every
      // row. HTML is reduced to text before it gets this far.
      body: "",
    });
    stats.layerCounts[layer] = (stats.layerCounts[layer] || 0) + 1;
    stats.parsed++;
  }

  return { events: Array.from(seen.values()), stats };
}

// Which layer actually produced the surviving events, most-trusted
// first — so a silent regression to the text-scrape fallback is
// visible in the popup/logs rather than indistinguishable from a
// clean structured capture.
function dominantLayer(stats) {
  const counts = (stats && stats.layerCounts) || {};
  if (counts["aria-label"] > 0) return "aria-label";
  if (counts["generic-node"] > 0) return "generic-node";
  return null;
}

// Pure: should the settle-and-collect poll loop stop, given the
// sequence of candidate counts observed so far (oldest first)? Used
// by settleAndCollectCalendar's retry loop (impure — real
// chrome.tabs/chrome.scripting calls) and unit-tested directly here
// with plain arrays of numbers.
//
// Stops once the last `stabilityPolls` counts are all equal — but a
// STABLE ZERO isn't trusted until `minPollsBeforeZeroExit` polls have
// happened, because a freshly-opened SPA tab reads "0 candidates" on
// its first few polls before it's mounted anything; that's a
// still-rendering page, not a structurally empty one. A stable
// NON-ZERO count is trusted immediately — once real candidates show
// up and hold steady, there's no reason to keep waiting out the rest
// of the max-wait budget.
function shouldStopPolling(counts, opts = {}) {
  const stabilityPolls = opts.stabilityPolls || STABILITY_POLLS;
  const minPollsBeforeZeroExit = opts.minPollsBeforeZeroExit || 5;
  if (!counts || counts.length < stabilityPolls) return false;
  const tail = counts.slice(-stabilityPolls);
  if (!tail.every((c) => c === tail[0])) return false;
  if (tail[0] > 0) return true;
  return counts.length >= minPollsBeforeZeroExit;
}

// Pure: explain a ZERO-event result so the popup can say something
// more useful than "Calendar: 0 events" — see the v1.3 header comment
// for why this exists. `stillRendering` comes from the caller: true
// when the candidate-count poll loop (shouldStopPolling above) never
// stabilized before CALENDAR_MAX_WAIT_MS ran out.
//
// v1.3.2: this used to end in a single catch-all — "found N
// candidates, none had a parseable time" — for every scanned>0 case
// that wasn't 100% all-day or 100% date-unresolved. That string is
// exactly what a 2026-08-14 field report showed while the TRUE cause
// was unrelated to parsing at all (the DOM scan's depth cap silently
// truncating before it ever reached the meeting tiles — see
// _calendarDomScanFunc's header comment). A wrong-but-confident
// diagnosis cost real debugging time. The catch-all is now broken
// apart into the three distinct "candidates existed but produced
// nothing" shapes `extractEventsFromCandidates`'s stats already
// distinguish, plus a genuine mixed-cause fallback that shows the
// stats breakdown instead of guessing a single headline cause.
function classifyZeroReason(stats, opts = {}) {
  if (opts.stillRendering) {
    return "page still rendering (candidate count never stabilized before the wait limit)";
  }
  const s = stats || {};
  const scanned = s.scanned || 0;
  if (scanned === 0) {
    return "no candidate elements found";
  }
  const plural = scanned === 1 ? "" : "s";
  const notMeetingShaped = s.notMeetingShaped || 0;
  const allDay = s.allDay || 0;
  const dateUnresolved = s.dateUnresolved || 0;
  const deduped = s.deduped || 0;

  if (allDay > 0 && allDay === scanned) {
    return `found ${scanned} candidate${plural}, all all-day (excluded)`;
  }
  if (notMeetingShaped > 0 && notMeetingShaped === scanned) {
    return `found ${scanned} candidate${plural}, none were meeting-shaped ` +
      `(didn't match "<subject>, <time> to <time>")`;
  }
  if (dateUnresolved > 0 && dateUnresolved === scanned) {
    return `found ${scanned} candidate${plural} with a time but no resolvable date`;
  }
  // Genuinely mixed causes (or a bucket this function doesn't have a
  // name for yet, e.g. every candidate deduped away) — name what's
  // actually known rather than defaulting to one of the specific
  // messages above, which would misattribute the cause the same way
  // the old single catch-all did.
  return `found ${scanned} candidate${plural}, none produced an event ` +
    `(${notMeetingShaped} not meeting-shaped, ${dateUnresolved} unresolved date/time, ` +
    `${allDay} all-day, ${deduped} duplicate)`;
}

// ── DOM scan (runs inside the page; exercised in tests via a
//    simulated fake DOM — see chrome-extension/tests/) ───────────────

// Runs inside the calendar tab via chrome.scripting.executeScript.
// Deliberately fully self-contained (no reference to any function
// above) — injected scripts execute in an isolated page context that
// cannot close over this file's top-level scope, same constraint the
// existing readMainText/clickTeamsNav functions already work under.
// The ONLY external thing it touches is the ambient `document` global
// — real in the browser, a plain object test fixtures set as
// `globalThis.document` before calling this function directly in
// Node (see chrome-extension/tests/).
//
// Scans `document` plus every reachable same-origin iframe
// (`el.contentDocument` — cross-origin access throws or returns null,
// caught and skipped) and open shadow root (`el.shadowRoot` — closed
// roots aren't exposed on the element at all, so those are silently
// and correctly never pierced).
//
// v1.3.2: this used to be a hand-rolled `.children` recursion with a
// hard `depth > 30` cutoff, on the theory that `querySelectorAll`
// couldn't be used because it doesn't exist on the fake-DOM test
// harness. That cutoff was the actual bug: a 2026-08-14 field
// diagnostic against a real Outlook Web week view showed the flat
// probe's `querySelectorAll("[aria-label]")` finding 28 matching
// meeting labels while the real scan found 0 candidates. Instrumenting
// the walk (see the depth-35/50 tests in
// chrome-extension/tests/background.test.js) confirmed it: the walk's
// own diagnostics showed `maxDepthReached: 31, elementsWalked: 31` on
// a synthetic 35-level-deep tree — the recursion hits `depth > 30` on
// its 32nd nesting level and stops, silently, before ever reaching a
// label planted at level 35. Outlook Web's React tree nests calendar
// tiles past that. `querySelectorAll` has no such limit — it's a
// native engine call, not a JS stack recursion — so each discovered
// root (document / iframe document / shadow root) is now searched
// flat with one call, and the fake-DOM test harness grew a
// `querySelectorAll` of its own (chrome-extension/tests/, `el()`/
// `doc()`) rather than keeping the depth-limited recursion just to
// stay testable against a double that couldn't do it.
//
// Only root DISCOVERY (finding iframes and shadow hosts, which aren't
// reachable via a CSS selector) still walks `.children` — via
// `querySelectorAll("*")` per root, which is a single flat call per
// root, not a depth-limited recursive one.
//
// Returns { candidates, diag }. `candidates` is a plain array of
// { label, columnDateIso, layer, structuredStart?, structuredEnd? }
// records — no parsing happens here, only collection; every actual
// interpretation decision lives in the tested pure functions above.
// `diag` is scan-shape info (element/iframe/shadow-root/root counts)
// for logging.
function _calendarDomScanFunc() {
  const out = [];
  const diag = { elementsWalked: 0, rootsScanned: 0, iframesSeen: 0, iframesEntered: 0, shadowRootsSeen: 0 };
  try {
    const TIME_HINT_RE = /\d{1,2}(:\d{2})?\s*[AaPp]?\.?[Mm]?\b|\d{1,2}:\d{2}\b/;
    const ALLDAY_HINT_RE = /\ball[\s-]?day\b/i;
    const MONTH_HINT_RE = /(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}(,?\s*\d{4})?/i;
    const TIME_RANGE_HINT_RE = /(\d{1,2}(:\d{2})?\s*[AaPp]\.?[Mm]\.?|\d{1,2}:\d{2})\s*(?:to|until|-|–|—)\s*(\d{1,2}(:\d{2})?\s*[AaPp]\.?[Mm]\.?|\d{1,2}:\d{2})/i;
    const KNOWN_DATE_ATTRS = ["data-start-date", "data-date", "data-start-day", "data-day", "data-dayid"];
    const ROLE_HINT_VALUES = { button: true, option: true, gridcell: true };

    function getAttr(el, name) {
      try { return el && el.getAttribute ? el.getAttribute(name) : null; } catch (_) { return null; }
    }
    function textOf(el) {
      try { return (el && (el.innerText != null ? el.innerText : el.textContent)) || ""; } catch (_) { return ""; }
    }

    // Breadth-first search for up to `maxResults` descendants matching
    // `predicate`, capped at `maxDepth`. Used to find <time>
    // descendants without depending on querySelectorAll.
    function findDescendants(el, predicate, maxDepth, maxResults) {
      const found = [];
      const queue = [{ node: el, depth: 0 }];
      while (queue.length && found.length < maxResults) {
        const cur = queue.shift();
        if (cur.depth > 0 && predicate(cur.node)) found.push(cur.node);
        if (cur.depth >= maxDepth) continue;
        const kids = cur.node.children || [];
        for (let i = 0; i < kids.length; i++) queue.push({ node: kids[i], depth: cur.depth + 1 });
      }
      return found;
    }

    // A candidate's time doesn't have to live in its own label/text.
    // Look, in order: (1) a <time> descendant — if TWO are found,
    // treat their `datetime` attrs as a structured start/end pair
    // (common pattern: separate start-time and end-time nodes); (2)
    // the element's own datetime/data-start(-time)/data-end(-time)
    // attributes, as a structured pair; (3) TEXT that already spells
    // a two-sided range — a <time> descendant's text, the element's
    // own title/data-time attrs, adjacent siblings' text, or the
    // nearest ancestor gridcell/column/columnheader's text — merged
    // into the label and left to the existing text-range parser
    // downstream (columnDateIso already handles the "no date in the
    // text" case correctly, so a text range is safer than guessing a
    // date on a bare time-of-day here — see parseStructuredTimePair's
    // comment for why bare times aren't treated as structured).
    function findExternalTime(el) {
      const timeEls = findDescendants(el, (n) => (n.tagName || "").toLowerCase() === "time", 6, 2);
      const dtAttrs = timeEls.map((t) => getAttr(t, "datetime")).filter(Boolean);
      if (dtAttrs.length >= 2) return { mode: "datetime-pair", start: dtAttrs[0], end: dtAttrs[1] };

      const startAttr = getAttr(el, "datetime") || getAttr(el, "data-start") || getAttr(el, "data-start-time");
      const endAttr = getAttr(el, "data-end") || getAttr(el, "data-end-time");
      if (startAttr && endAttr) return { mode: "datetime-pair", start: startAttr, end: endAttr };

      const texts = [];
      if (timeEls.length) texts.push(textOf(timeEls[0]).trim());
      for (const attr of ["title", "data-time", "data-start-time"]) {
        const v = getAttr(el, attr);
        if (v) texts.push(v);
      }
      const parent = el.parentElement;
      if (parent && parent.children) {
        for (let i = 0; i < parent.children.length; i++) {
          const kid = parent.children[i];
          if (kid === el) continue;
          texts.push(textOf(kid).trim() || getAttr(kid, "aria-label") || "");
        }
      }
      let node = el.parentElement;
      for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
        const role = getAttr(node, "role");
        if (role === "gridcell" || role === "columnheader" || role === "column") {
          texts.push(getAttr(node, "aria-label") || textOf(node));
        }
      }
      for (const t of texts) if (t && TIME_RANGE_HINT_RE.test(t)) return { mode: "text", text: t };
      for (const t of texts) if (t && TIME_HINT_RE.test(t)) return { mode: "text", text: t };
      return null;
    }

    function nearestDateIso(el, columnHeaders) {
      let node = el;
      for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
        for (const attr of KNOWN_DATE_ATTRS) {
          const v = getAttr(node, attr);
          if (v) {
            const d = new Date(v);
            if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
          }
        }
        const aria = getAttr(node, "aria-label");
        if (aria && MONTH_HINT_RE.test(aria)) {
          const d = new Date(aria.replace(/^[A-Za-z]+,\s*/, ""));
          if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
        }
      }
      // Tier 3: nearest columnheader (collected during the same walk
      // that found `el`, across the whole reachable tree including
      // iframes/shadow roots) by horizontal position.
      try {
        const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
        if (rect) {
          const cx = rect.left + rect.width / 2;
          let best = null, bestDist = Infinity;
          for (const h of columnHeaders) {
            const label = getAttr(h, "aria-label") || textOf(h);
            if (!label || !MONTH_HINT_RE.test(label)) continue;
            const hr = h.getBoundingClientRect ? h.getBoundingClientRect() : null;
            if (!hr) continue;
            const dist = Math.abs((hr.left + hr.width / 2) - cx);
            if (dist < bestDist) { bestDist = dist; best = label; }
          }
          if (best) {
            const d = new Date(best.replace(/^[A-Za-z]+,\s*/, ""));
            if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
          }
        }
      } catch (_) { /* best-effort only */ }
      return null;
    }

    // String-level pre-filter dedup: several nested elements often
    // carry the literal same label text for one event. The pure
    // subject+start dedup downstream is the real safety net; this is
    // just cheap early pruning.
    const seenLabels = new Set();
    const columnHeaders = [];
    // { el, label, layer, structuredStart?, structuredEnd? } — date
    // resolution (needs the FULL columnHeaders list) happens in a
    // second pass after the whole tree has been walked, so an event
    // that appears before its column's header in DOM order still
    // resolves correctly.
    const rawCandidates = [];

    function visit(el) {
      const role = getAttr(el, "role");
      if (role === "columnheader") columnHeaders.push(el);

      const ownLabel = getAttr(el, "aria-label");
      if (ownLabel) {
        if (seenLabels.has(ownLabel)) return;
        let label = ownLabel;
        let structuredStart, structuredEnd;
        if (!TIME_HINT_RE.test(label) && !ALLDAY_HINT_RE.test(label)) {
          const ext = findExternalTime(el);
          if (ext && ext.mode === "text") label = `${label}, ${ext.text}`;
          else if (ext && ext.mode === "datetime-pair") { structuredStart = ext.start; structuredEnd = ext.end; }
        }
        if (!structuredStart && !TIME_HINT_RE.test(label) && !ALLDAY_HINT_RE.test(label)) return;
        seenLabels.add(ownLabel);
        rawCandidates.push({ el, label, layer: "aria-label", structuredStart, structuredEnd });
        return;
      }

      // Layer 2: generic event-shaped node without its own aria-label
      // (some builds render calendar tiles this way). Broader net,
      // only adds candidates layer 1 didn't already cover.
      if (!role || !ROLE_HINT_VALUES[role]) return;
      let text = textOf(el).trim();
      if (!text || seenLabels.has(text)) return;
      let structuredStart, structuredEnd;
      if (!TIME_HINT_RE.test(text) && !ALLDAY_HINT_RE.test(text)) {
        const ext = findExternalTime(el);
        if (ext && ext.mode === "text") text = `${text}, ${ext.text}`;
        else if (ext && ext.mode === "datetime-pair") { structuredStart = ext.start; structuredEnd = ext.end; }
      }
      if (!structuredStart && !TIME_HINT_RE.test(text) && !ALLDAY_HINT_RE.test(text)) return;
      seenLabels.add(text);
      rawCandidates.push({ el, label: text, layer: "generic-node", structuredStart, structuredEnd });
    }

    // Discover every reachable root — the top document, every
    // same-origin iframe document, and every open shadow root — via
    // BFS so a shadow root nested inside an iframe (or vice versa) is
    // still found. Each root is inspected with ONE flat
    // `querySelectorAll("*")` call, not a depth-limited recursion:
    // finding iframe/shadow-host elements has no CSS-selector
    // shortcut (there's no `:has-shadow-root` selector), but a flat
    // scan-and-classify of every element in a root is a single native
    // call regardless of how deep that root's tree goes.
    function collectRoots(root0) {
      const entries = [{ root: root0, kind: "document" }];
      const seen = new Set([root0]);
      let i = 0;
      while (i < entries.length) {
        const root = entries[i++].root;
        diag.rootsScanned++;
        let all;
        try { all = root.querySelectorAll("*"); } catch (_) { all = []; }
        for (const node of all) {
          const tag = (node.tagName || "").toLowerCase();
          if (tag === "iframe") {
            diag.iframesSeen++;
            let innerDoc = null;
            try { innerDoc = node.contentDocument || null; } catch (_) { innerDoc = null; }
            if (innerDoc && !seen.has(innerDoc)) {
              seen.add(innerDoc);
              diag.iframesEntered++;
              entries.push({ root: innerDoc, kind: "iframe" });
            }
          }
          if (node.shadowRoot && !seen.has(node.shadowRoot)) {
            seen.add(node.shadowRoot);
            diag.shadowRootsSeen++;
            entries.push({ root: node.shadowRoot, kind: "shadow" });
          }
        }
      }
      return entries;
    }

    // The candidate-shaped selector: anything with its own aria-label
    // (layer 1), any of the layer-2 generic-event roles (so `visit`
    // can apply the same "no own label, but event-shaped" fallback it
    // always has), or a columnheader (needed for date resolution,
    // see `nearestDateIso`'s tier 3 — previously found for free
    // because the old walk visited literally everything; a flat query
    // has to ask for it explicitly).
    const CANDIDATE_SELECTOR =
      '[aria-label], [role="button"], [role="option"], [role="gridcell"], [role="columnheader"]';

    const root0 = typeof document !== "undefined" ? document : null;
    if (root0) {
      for (const { root } of collectRoots(root0)) {
        let nodes;
        try { nodes = root.querySelectorAll(CANDIDATE_SELECTOR); } catch (_) { nodes = []; }
        for (const node of nodes) {
          diag.elementsWalked++;
          visit(node);
        }
      }
    }

    for (const c of rawCandidates) {
      out.push({
        label: c.label,
        columnDateIso: nearestDateIso(c.el, columnHeaders),
        layer: c.layer,
        structuredStart: c.structuredStart,
        structuredEnd: c.structuredEnd,
      });
    }
  } catch (_) {
    // Best-effort only — an empty return here just means the caller
    // falls back to the text-scrape path.
  }
  return { candidates: out, diag };
}

// Advance Outlook Web's calendar to the following week. Unverified —
// see NEXT_WEEK_SELECTORS above for why a click cascade was chosen
// over a dated URL.
// ──────────────────────────────────────────────────────────────────
// Detail from the SCREEN (v1.9) — the source that cannot be wrong
// about auth, hosts, API versions or workers.
// ──────────────────────────────────────────────────────────────────
//
// FIVE RELEASES OF HISTORY, because it explains the design.
//
//   v1.5  read join links out of the grid label. Worked — and found
//         the only 1 of 25 labels that had one.
//   v1.6  guessed four API endpoints. All four modelled on classic
//         OWA; the tenant runs the new stack. Nothing worked.
//   v1.7  recorded Outlook's own responses instead of calling the API.
//         Right idea, still unproven: if Outlook fetches from a
//         SERVICE WORKER, a main-world patch never sees it.
//   v1.8  made the failure modes distinguishable in diagnostics.
//
// Every one of those depends on something this project cannot observe
// from where it is built: an endpoint, an auth scheme, a JSON shape,
// or which thread issues a fetch.
//
// This does not. When a user clicks an event, Outlook RENDERS the
// attendees, the agenda and the join link into the page — that is what
// the user is looking at. Reading what is on screen cannot be defeated
// by a service worker, a bearer token, a tenant migration or an API
// version, because by the time it is on screen all of that has already
// happened.
//
// It IS slower, and it touches the DOM. Both were why this was passed
// over in favour of the API route four releases ago. That judgement
// was wrong: "cleaner" is worth nothing next to "works".
//
// STRUCTURE-AGNOSTIC ON PURPOSE. This does not target selectors in the
// detail pane — that would be one more guess about markup nobody here
// can see. It snapshots the page's visible text, clicks, waits for the
// text to grow, and reads what is NEW. Then it pulls out:
//
//   * attendees — by EMAIL ADDRESS regex. An address is an address in
//     any markup.
//   * join URL  — via JOIN_PROVIDER_PATTERNS, the same host+path list
//     the label extractor and the diagnostic already share.
//   * agenda    — the remaining new text.
//
// None of that depends on a class name, a role, or a DOM shape.
//
// BOUNDED, because 47 events one at a time is not acceptable: only
// events still MISSING detail, only those inside a near-term window,
// capped in count, and under a wall-clock budget. Whatever it does not
// reach is reported as skipped rather than silently left empty.

const DETAIL_MAX_EVENTS = 25;
const DETAIL_TIME_BUDGET_MS = 90000;
const DETAIL_WINDOW_HOURS = 72;

// Runs IN THE PAGE. `wanted` is [{subject, startIso}]; returns
// [{subject, startIso, attendees, body, joinUrl}] for whatever it
// managed to open inside the budget, plus counters.
async function _readEventDetailsFunc(wanted, joinPatterns, maxEvents, budgetMs) {
  const started = Date.now();
  const out = { details: [], opened: 0, matchedElement: 0, skipped: 0,
                grew: 0, joinFromAnchor: 0,
                // Per-event outcome, so the app can tell the user WHY a
                // specific meeting has no detail instead of a flat "(No
                // description on this invite.)" for every cause.
                statuses: [],
                error: null };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  try {
    const providers = (joinPatterns || []).map((p) => ({
      name: p.name, host: new RegExp(p.host, "i"), path: new RegExp(p.path, "i"),
    }));

    const isJoin = (u) => {
      try {
        const url = new URL(u);
        return providers.some((p) => p.host.test(url.hostname) && p.path.test(url.pathname));
      } catch (_) { return false; }
    };

    // EVERY REACHABLE ROOT, NOT JUST `document`.
    //
    // FIELD SCREENSHOT 2026-08-21, v2.57.0. The captured "agenda" for a
    // real meeting read, in full:
    //
    //     Join / Chat / Fri 8/21/2026 10:00 AM - 11:00 AM /
    //     No location added / GG / <organizer> invited you.
    //
    // That is the detail pane's CHROME — its buttons, its date line,
    // its footer. The invite's actual description was not in it, and
    // neither was the Join URL. Both were present on screen.
    //
    // Outlook renders the invite body in its own same-origin IFRAME
    // (the message-body frame), and renders parts of the card inside
    // SHADOW ROOTS. `document.body.innerText` stops at both
    // boundaries, and so does `document.querySelectorAll("a[href]")`.
    // So the reader saw the frame around the content and never the
    // content: a Teams pane yields no anchor (joinFromAnchor: 0 in
    // every field diagnostic to date) and a Webex invite yields no
    // pasted URL, because the URL is one frame deeper.
    //
    // `_calendarDomScanFunc` has always walked iframes and shadow
    // roots — that is why the GRID scan works. The detail reader was
    // the only DOM consumer here that did not, and it is the one whose
    // whole job is reading the pane.
    //
    // Cross-origin frames throw on access; they are skipped, not
    // fatal. Depth and count are bounded so a pathological page costs
    // a fixed budget rather than the run.
    const MAX_ROOTS = 400;
    const collectRoots = () => {
      const roots = [];
      const seen = new Set();
      const pushDoc = (doc, depth) => {
        if (!doc || depth > 4 || roots.length >= MAX_ROOTS) return;
        if (seen.has(doc)) return;
        seen.add(doc);
        roots.push(doc);
        // Shadow roots hosted anywhere in this document.
        let hosts = [];
        try { hosts = Array.from(doc.querySelectorAll("*")); }
        catch (_) { hosts = []; }
        for (const el of hosts) {
          if (roots.length >= MAX_ROOTS) break;
          let sr = null;
          try { sr = el.shadowRoot; } catch (_) { sr = null; }
          if (sr && !seen.has(sr)) { seen.add(sr); roots.push(sr); }
        }
        // Same-origin frames, recursively. Cross-origin access throws.
        let frames = [];
        try { frames = Array.from(doc.querySelectorAll("iframe,frame")); }
        catch (_) { frames = []; }
        for (const f of frames) {
          if (roots.length >= MAX_ROOTS) break;
          let sub = null;
          try { sub = f.contentDocument; } catch (_) { sub = null; }
          if (sub) pushDoc(sub, depth + 1);
        }
      };
      pushDoc(document, 0);
      return roots;
    };

    const queryAll = (sel) => {
      const out = [];
      for (const root of collectRoots()) {
        try {
          const found = root.querySelectorAll(sel);
          for (const el of found) out.push(el);
        } catch (_) { /* detached root — skip */ }
      }
      return out;
    };

    // Every aria-label-bearing element, across every reachable root —
    // the tile itself can sit inside a shadow root on newer OWA.
    const labelled = () => queryAll("[aria-label]");

    const norm = (t) => String(t || "").replace(/\s+/g, " ").trim().toLowerCase();

    // Text from every reachable root, so the invite body inside the
    // message iframe counts as text this pane showed. Ordered
    // outermost-first, which keeps the main document's line order
    // intact for the diff below and appends frame content after it.
    const visibleText = () => {
      const parts = [];
      for (const root of collectRoots()) {
        try {
          const host = root.body || root;
          const t = host ? (host.innerText != null
                            ? host.innerText : host.textContent) : "";
          if (t) parts.push(t);
        } catch (_) { /* skip */ }
      }
      return parts.join("\n");
    };

    const EMAIL_RE = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;
    const URL_RE = /https?:\/\/[^\s"'<>)\]]+/g;

    // ANCHOR HREFS, NOT JUST TEXT.
    //
    // v1.10 read URLs only out of `innerText`, and the field result was
    // exact: Webex and Zoom meetings got a Join link, Teams meetings got
    // nothing. That is not a Teams-specific parsing problem — it is
    // WHERE the URL lives:
    //
    //   Webex / Zoom  the organiser's add-in pastes the raw URL into the
    //                 invite body, so it IS visible text.
    //   Teams         the pane renders a "Join" BUTTON. The URL is the
    //                 anchor's href and appears nowhere in the text.
    //
    // Reading text and concluding "Teams has no join link" is the same
    // mistake as v1.4's anchor-only probe, exactly inverted: that one
    // looked at elements and missed text, this one looked at text and
    // missed elements. Both places are read now.
    //
    // href attributes only — no other attribute is inspected, and no
    // element structure is depended on.
    const anchorUrls = () => {
      const out = [];
      try {
        for (const a of queryAll("a[href]")) {
          const h = a.getAttribute("href") || "";
          if (h.startsWith("http")) out.push(h);
        }
      } catch (_) { /* ignore */ }
      return out;
    };

    // Attendees render as NAMES in the detail pane; addresses usually do
    // not appear at all. v1.10 matched only addresses, which is why a
    // meeting with a dozen invitees reported "Attendees (1)" — the one
    // that happened to show an address — or (0).
    //
    // Outlook exposes each attendee row to assistive tech, so the
    // accessible name is the person's name without this having to guess
    // at markup. Deliberately conservative about what counts as a
    // person: 2-4 words, letters and the punctuation names actually
    // contain, no digits, no URL/email shapes, and nothing sentence-like.
    // A wrong attendee is worse than a missing one — it propagates into
    // speaker identification and follow-up recipients.
    // THERE IS NO NAME-SHAPE SCANNER ANY MORE, ON PURPOSE.
    //
    // 1.11–1.12 tried to identify attendees by scanning the page for
    // labels that LOOKED like a person's name — 2-to-4 title-case
    // words, minus a vocabulary blacklist. The field result (2026-08-20)
    // was a meeting with "Attendees (24)" of which 22 were Outlook's
    // own controls — "Skip to main content", "App launcher", "Ribbon
    // tabs", "Chat with Copilot" — plus the user's OWN account button
    // rendered as an invitee. A blacklist can never enumerate a
    // product's entire UI vocabulary, in every language Outlook ships
    // in, across every redesign. Shape-of-text is simply not evidence
    // that a string denotes a person.
    //
    // Attendees now come only from sources where they are DATA:
    //   * Outlook's own event-detail responses (mechanisms 1 and 3 —
    //     the captured JSON carries the real invitee list, which field
    //     diagnostics showed filling 19 of 23 meetings), and
    //   * email addresses in the text THIS pane added.
    // A meeting whose sources yield nothing shows no attendees — an
    // honest zero, which the 24-button lie was not.

    for (const want of wanted || []) {
      if (out.details.length >= maxEvents
          || Date.now() - started > budgetMs) {
        out.skipped++;
        out.statuses.push({ subject: want.subject, startIso: want.startIso,
                            status: "budget" });
        continue;
      }

      const target = norm(want.subject);
      if (!target) { out.skipped++; continue; }

      // The event tile whose label carries this subject. Longest label
      // wins: a tile's label is the full "subject, time, ..." string,
      // while a stray container may repeat just the subject.
      let el = null;
      let best = -1;
      for (const cand of labelled()) {
        const lab = norm(cand.getAttribute("aria-label"));
        if (!lab.startsWith(target)) continue;
        if (lab.length > best) { best = lab.length; el = cand; }
      }
      if (!el) {
        // A stuck pane from the previous event hides the grid, and
        // then EVERY later tile "does not exist" — the field run's
        // signature was 11 opens followed by 12 consecutive misses.
        // One Escape + settle + re-scan recovers the cascade case
        // without costing the genuinely-absent-tile case more than
        // half a second.
        try {
          document.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Escape", code: "Escape", keyCode: 27, bubbles: true,
          }));
        } catch (_) { /* ignore */ }
        await sleep(400);
        best = -1;
        for (const cand of labelled()) {
          const lab = norm(cand.getAttribute("aria-label"));
          if (!lab.startsWith(target)) continue;
          if (lab.length > best) { best = lab.length; el = cand; }
        }
      }
      if (!el) {
        out.skipped++;
        out.statuses.push({ subject: want.subject, startIso: want.startIso,
                            status: "no_tile" });
        continue;
      }
      out.matchedElement++;

      const before = visibleText();
      const beforeLen = before.length;
      // Anchors present BEFORE the click, so the join link is taken
      // from what this event added rather than from a Join button
      // belonging to some other meeting already on screen. Attaching a
      // link to the wrong meeting is worse than having none.
      const anchorsBefore = new Set(anchorUrls());
      // Person-shaped labels already present — as a SET, not a count.
      // The set serves two jobs: render detection compares membership
      // (a churning page re-rendering the SAME labels is not a
      // signal), and attendee extraction subtracts it (a label that
      // existed before the click is page chrome, not an invitee of
      // THIS meeting).

      try {
        el.click();
      } catch (_) { out.skipped++; continue; }
      out.opened++;

      // Wait for the pane to render — poll rather than fix a delay, so
      // a fast tenant is not punished and a slow one is not truncated.
      // GROWTH_MIN is deliberately small. An earlier draft required 40
      // new characters, which silently discarded a sparse invite — one
      // attendee, no agenda — as "never rendered". Whether the pane is
      // USEFUL is decided below by what was actually extracted, not by
      // guessing a size for it here.
      // "Did the pane render?" is measured by whether ANY of the three
      // things we came for appeared — not by how much text arrived.
      //
      // Text length alone was wrong twice. First at 40 characters,
      // which discarded a sparse invite. Then at 10, which still
      // discarded a Teams pane: Teams reveals a Join BUTTON and a
      // couple of words, so its text can grow by less than that while
      // carrying exactly the URL this whole mechanism exists to get.
      // Judging arrival by volume is a proxy; judging it by content is
      // the actual question.
      const GROWTH_MIN = 10;
      // FIELD REGRESSION 2026-08-20, extension 1.11. This check
      // decides when the detail pane has arrived, and 1.11 broke it by
      // widening the signals to "ANY new anchor anywhere on the page"
      // and "ANY change in the page-wide count of name-shaped labels".
      // Outlook Web is a live application — its DOM churns constantly,
      // clicked or not — so on a real calendar those fired on the
      // FIRST 150ms poll, extraction ran against a pane that had not
      // loaded, and every meeting came back empty. The Webex links
      // 1.10 had been finding disappeared, and the empty capture then
      // overwrote the store's previously-enriched events (fixed on the
      // backend in the same release: enrichment now ratchets).
      //
      // Two rules restore correctness without giving up the cases the
      // wider signals were added for (a Teams pane that reveals mostly
      // a button; an invite that reveals mostly attendee rows):
      //
      //   1. A signal must be SPECIFIC to what we came for: a new
      //      JOIN-shaped anchor (not any anchor), or a name-shaped
      //      label that was not present before the click (set
      //      membership, not count — a page re-rendering the same
      //      labels moves nothing).
      //   2. A signal starts extraction only after the page SETTLES:
      //      once signalled, keep polling until the text length holds
      //      still for two consecutive polls, so extraction reads the
      //      loaded pane rather than its first painted fragment.
      const paneSignal = () =>
        (visibleText().length > beforeLen + GROWTH_MIN)
        || anchorUrls().some((u) => !anchorsBefore.has(u) && isJoin(u));
      let after = before;
      let signalled = false;
      let lastLen = -1;
      let stable = 0;
      for (let i = 0; i < 24; i++) {
        await sleep(150);
        after = visibleText();
        if (!signalled) {
          if (paneSignal()) signalled = true;
          else continue;
        }
        if (after.length === lastLen) {
          stable += 1;
          if (stable >= 2) break;
        } else {
          stable = 0;
          lastLen = after.length;
        }
      }

      if (signalled) {
        out.grew++;
        // What is on screen now that was not before. Crude and exactly
        // right: the detail pane IS the new text.
        const fresh = after.slice(0, 200000);
        // The text THIS pane added. Two diffs, in order of trust:
        // innerText usually grows by appending, so a prefix match
        // yields the exact addition; the old split heuristic stays as
        // the fallback for a pane that inserted mid-page.
        //
        // What is deliberately GONE is the final fallback to the whole
        // page. It existed so a pane with little text still yielded
        // something, and what it yielded was the entire calendar's
        // visible text stored as the meeting's "agenda" — garbage that
        // then overwrote real fields via the store merge. URL and
        // email scans below still search the whole page (that is what
        // found the pasted Webex links); only the BODY is held to a
        // genuine diff, because the body is the one field where "the
        // whole page" is worse than nothing.
        // ORDER-INDEPENDENT DIFF. The previous version assumed the
        // pane's text lands AFTER the existing page text in document
        // order (a prefix match, with a split heuristic behind it).
        // Nothing guarantees that: where Outlook's event panel sits in
        // the DOM relative to the grid is exactly the kind of fact
        // this project cannot observe and keeps guessing wrong. If the
        // panel renders BEFORE the grid, both heuristics produce an
        // EMPTY diff for every event — no body, no addresses, no
        // pasted Webex URL — while looking identical to "the pane had
        // nothing". An extraction pipeline must not have a failure
        // mode that depends on element order.
        //
        // Lines in `after` minus (counted) lines in `before`: immune
        // to where the pane inserts, and duplicated lines survive in
        // the right quantity. Order within the pane is preserved.
        const beforeCounts = new Map();
        for (const ln of before.split("\n")) {
          beforeCounts.set(ln, (beforeCounts.get(ln) || 0) + 1);
        }
        const newLines = [];
        for (const ln of after.split("\n")) {
          const c = beforeCounts.get(ln) || 0;
          if (c > 0) beforeCounts.set(ln, c - 1);
          else newLines.push(ln);
        }
        const added = newLines.join("\n").trim();
        // NOTHING is scanned page-wide any more. The whole-page email
        // scan attributed another meeting's organiser — whose address
        // is literally rendered in that meeting's grid tile label —
        // as an attendee of whichever meeting happened to be clicked
        // (field screenshot 2026-08-20: a different call's
        // organiser address listed among this meeting's "attendees").
        // The same logic would hand one meeting's pasted join URL to
        // another. Mis-attribution is worse than a miss, so both
        // scans are held to the text THIS pane added; the join link
        // additionally comes from anchors that appeared with the
        // pane, which is already click-scoped.

        const emails = Array.from(new Set(
          (added.match(EMAIL_RE) || []).map((e) => e.trim())));

        // Text URLs first (Webex/Zoom paste theirs into the body), then
        // anchors that appeared with this event (Teams renders a Join
        // BUTTON and puts the URL only in the href). Text is preferred
        // where both exist: a pasted URL is unambiguously part of THIS
        // invite, whereas an anchor is located by having newly appeared.
        const urls = added.match(URL_RE) || [];
        const newAnchors = anchorUrls().filter((u) => !anchorsBefore.has(u));
        const joinUrl = urls.find(isJoin) || newAnchors.find(isJoin) || "";
        if (!urls.find(isJoin) && newAnchors.find(isJoin)) out.joinFromAnchor++;

        // Attendee NAMES from the pane's accessible labels, plus any
        // addresses in the text. Names are what Outlook actually
        // renders; v1.10 read addresses only and reported "Attendees
        // (1)" for a meeting with a dozen invitees.
        // ONLY labels that appeared with this pane. Anything present
        // before the click — buttons, view controls, other tiles — is
        // interface chrome that happens to be name-shaped, and on a
        // real calendar the page-wide scan finds plenty of it
        // ("New event", "Next week"). Subtracting the pre-click set
        // removes all of it in one move, and also stops one meeting's
        // still-open pane from bleeding invitees into the next.
        const attendees = emails;

        // Body: the new text with addresses and links removed, so the
        // agenda does not re-state the attendee list.
        let body = added.replace(EMAIL_RE, " ").replace(URL_RE, " ")
          .replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
        if (body.length > 8000) body = body.slice(0, 8000);

        // Only record something we actually found. A pane that
        // rendered but yielded no address, no link and no text
        // contributes nothing — and an empty attendee list recorded
        // here would be indistinguishable from a meeting that
        // genuinely has none.
        if (attendees.length || joinUrl || body) {
          out.details.push({
            subject: want.subject,
            startIso: want.startIso,
            attendees,
            body,
            joinUrl,
          });
          out.statuses.push({ subject: want.subject, startIso: want.startIso,
                              status: "opened" });
        } else {
          // The pane rendered and genuinely offered none of the three
          // fields — an invite with no description and no visible
          // detail. Distinct from every failure mode.
          out.statuses.push({ subject: want.subject, startIso: want.startIso,
                              status: "opened_empty" });
        }
      }

      // Close the pane so the next click starts from a comparable
      // baseline. Escape is the one gesture every Outlook build honours.
      try {
        document.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Escape", code: "Escape", keyCode: 27, bubbles: true,
        }));
      } catch (_) { /* ignore */ }
      await sleep(200);
    }
  } catch (e) {
    out.error = (e && e.message) ? e.message : String(e);
  }
  return out;
}

async function goToNextCalendarWeek(tabId) {
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt > 0) await sleep(1000);
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId },
        args: [NEXT_WEEK_SELECTORS],
        func: (selectors) => {
          for (const sel of selectors) {
            let els;
            try { els = document.querySelectorAll(sel); } catch (_) { continue; }
            for (const el of els) {
              if (!el || el.offsetParent === null) continue;
              try { el.click(); return sel; } catch (_) { /* try next */ }
            }
          }
          return null;
        },
      });
      const clicked = result && result[0] && result[0].result;
      if (clicked) {
        console.log(`[ext] calendar: advanced to next week via ${clicked}`);
        await sleep(1500);
        return true;
      }
    } catch (e) {
      console.warn(`[ext] calendar: next-week click attempt ${attempt + 1} failed:`, e);
    }
  }
  console.warn(
    "[ext] calendar: couldn't find a 'next week' control — only the " +
    "current week was captured. NEXT_WEEK_SELECTORS may need updating " +
    "for this Outlook Web build.");
  return false;
}

// Wait for the calendar tab's content to settle, then return whatever
// the DOM scan found. v1.3: retries on CANDIDATE COUNT, not a fixed
// text-size wait — the old version ran the DOM scan exactly once,
// AFTER the text-based settle loop finished, so a grid that rendered
// its text shell before its event tiles (or one behind a same-origin
// iframe the old scan couldn't see at all) could get scanned before
// anything was actually there. Now the scan itself runs every poll
// and `shouldStopPolling` decides when the count has stabilized.
// Returns { candidates, text, scanDiag, stabilized } — `text` is the
// raw inner-text fallback used only when structured extraction finds
// nothing at all; `stabilized` is false if the max-wait budget ran out
// without the count ever settling (see classifyZeroReason's
// "page still rendering" case).
// ──────────────────────────────────────────────────────────────────
// Response capture (v1.7) — read Outlook's OWN calls instead of
// making our own.
// ──────────────────────────────────────────────────────────────────
//
// THE HISTORY THAT PRODUCED THIS, because it is the whole argument.
//
// Three fields are empty on every extension-sourced meeting — the
// Teams join URL, the attendee list, and the invite body. All three
// live in the event detail, which the calendar grid does not render.
// Field data settled that: `anchorCount: 0`, `labelJoinCount: 1`
// across a full week, and v1.5's label extraction correctly found the
// one and only link that existed.
//
// v1.6 then tried to fetch the detail from the API directly, and
// failed for a reason worth writing down. It shipped four candidate
// endpoints, all modelled on classic OWA on `outlook.office.com`. The
// field run came back:
//
//     origin: "https://outlook.cloud.microsoft"
//     canaryPresent: false
//     rest-v2-calendarview: 401 auth-rejected
//
// That tenant is the NEW Outlook web stack. It has no X-OWA-CANARY —
// not missing, not applicable — and authenticates with bearer tokens,
// so three candidates were never attempted and the fourth was
// rejected. Every guess was wrong, and each guess cost a full
// release/reinstall/re-run cycle on the user's side.
//
// THE LESSON IS NOT "guess better endpoints." It is that replicating
// somebody else's authenticated request means tracking their auth
// scheme forever, and this project cannot observe that scheme from
// here. Any fix shaped like "call endpoint X with credential Y" is one
// Microsoft change away from another cycle.
//
// So this does not make a request at all.
//
// Outlook is ALREADY fetching every one of these fields — it has to,
// to render the calendar. This installs a passive recorder in the
// page before Outlook's own scripts run, lets Outlook authenticate
// however it likes to whatever endpoint it likes, and reads the
// responses on the way back. No endpoint to guess. No token ever
// handled by this extension. Works on classic OWA and the new stack
// identically, because it does not care which one is running.
//
// WHY MAIN WORLD. A content script's `fetch` is the isolated world's
// own, which is NOT the object page scripts call — patching it there
// records nothing. `world: "MAIN"` puts the patch on the page's real
// globals. And it must be registered at `document_start`: Outlook's
// calendar request is in flight within a second of navigation, and a
// recorder installed after it is a recorder that missed it.
//
// WHAT IT DOES NOT DO. It never modifies a request, never blocks one,
// never reads a credential, and never sends anything anywhere — the
// patch is read-only pass-through and stores parsed bodies on a page
// global that the extension reads back once and then drops. Request
// headers, including Authorization, are never touched.

// Reads back what the recorder collected and CLEARS it, so a second
// week's navigation starts from empty rather than re-harvesting the
// first week's bodies.
function _harvestCalendarResponses() {
  try {
    const s = window.__mrCal;
    if (!s) {
      return { bodies: [], seen: 0, matched: 0, dropped: 0,
               notMeetingShaped: 0, installed: false };
    }
    const out = {
      bodies: s.bodies, seen: s.seen, matched: s.matched,
      dropped: s.dropped, notMeetingShaped: s.notMeetingShaped || 0,
      installed: true,
    };
    s.bodies = [];
    s.bytes = 0;
    return out;
  } catch (e) {
    return { bodies: [], seen: 0, matched: 0, dropped: 0, installed: false,
             error: (e && e.message) || String(e) };
  }
}

// Pull the three detail fields out of whatever shape the tenant's API
// returned, and key them by subject + start so they can be matched to
// the events the DOM scan already produced.
//
// SHAPE-AGNOSTIC ON PURPOSE. Classic OWA (EWS-over-JSON) and the new
// stack (Graph-ish) name these fields differently and nest them
// differently, and v1.6 proved this project cannot know in advance
// which one a tenant runs. So rather than parse a schema, this walks
// the response for any object that looks like a meeting — has a
// subject-ish key AND a start-ish key — and reads the aliases it
// knows. An unrecognised shape yields nothing, which is exactly the
// behaviour the fields had before this existed.
const DETAIL_KEYS = {
  subject: ["subject", "title", "normalizedsubject"],
  start: ["start", "starttime", "startdate", "originalstart", "startdatetime"],
  attendees: ["attendees", "requiredattendees", "optionalattendees", "participants"],
  body: ["body", "bodypreview", "description", "textbody"],
  joinUrl: ["joinurl", "onlinemeetingurl", "skypeteamsmeetingurl", "joinweburl"],
  onlineMeeting: ["onlinemeeting", "onlinemeetinginformation"],
};

function _pick(obj, aliases) {
  for (const k of Object.keys(obj || {})) {
    if (aliases.includes(k.toLowerCase())) return obj[k];
  }
  return undefined;
}

// A datetime out of any of the shapes these APIs use: a bare ISO
// string, {DateTime: "..."}, or {dateTime: "...", timeZone: "..."}.
function _detailDateTime(v) {
  if (!v) return "";
  if (typeof v === "string") return v;
  if (typeof v === "object") {
    const inner = _pick(v, ["datetime", "date"]);
    if (typeof inner === "string") return inner;
  }
  return "";
}

// Attendee display names. Names only — an address is a person's
// contact detail and nothing downstream needs it here; the roster
// wants names. Falls back to the address's local part ONLY when there
// is no name at all, since "no attendees" and "attendees we could not
// name" are different states and the roster can use either.
function _detailAttendees(v) {
  const out = [];
  const push = (entry) => {
    if (!entry) return;
    if (typeof entry === "string") { out.push(entry); return; }
    if (typeof entry !== "object") return;
    const name = _pick(entry, ["name", "displayname"]);
    if (typeof name === "string" && name.trim()) { out.push(name.trim()); return; }
    const email = _pick(entry, ["emailaddress", "address", "mailbox"]);
    if (typeof email === "string" && email.includes("@")) { out.push(email.trim()); return; }
    if (email && typeof email === "object") {
      const n = _pick(email, ["name", "displayname"]);
      const a = _pick(email, ["address", "emailaddress"]);
      if (typeof n === "string" && n.trim()) out.push(n.trim());
      else if (typeof a === "string" && a.trim()) out.push(a.trim());
    }
  };
  if (Array.isArray(v)) v.forEach(push);
  else push(v);
  // De-duped case-insensitively; a meeting can list the same person as
  // both required and optional.
  const seen = new Set();
  return out.filter((n) => {
    const k = n.toLowerCase();
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function _detailBody(v) {
  if (typeof v === "string") return v;
  if (v && typeof v === "object") {
    const c = _pick(v, ["content", "text", "value"]);
    if (typeof c === "string") return c;
  }
  return "";
}

// HTML bodies are the norm. The invite body is shown to the user and
// fed to the briefing, so it wants to be readable text, not markup.
function _stripHtml(html) {
  if (!html || !/<[a-z!/]/i.test(html)) return (html || "").trim();
  return html
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|tr|li|h[1-6])>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// Same key the DOM extraction uses (subject|startIso) so the two sides
// line up. Start is normalised to minute precision: the API returns
// seconds and a timezone, the label parse does not, and an exact
// string compare would match nothing.
function detailKey(subject, startIso) {
  const subj = String(subject || "").trim().toLowerCase();
  const s = String(startIso || "");
  const m = s.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!subj || !m) return "";
  return `${subj}|${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}`;
}

// Walk captured response bodies and return Map(key -> {attendees,
// body, joinUrl}). Node- and depth-capped: these payloads are large
// and an unbounded walk would stall the service worker.
function detailsFromResponses(bodies) {
  const found = new Map();
  let nodes = 0;

  const visit = (v, depth) => {
    if (v === null || typeof v !== "object" || depth > 14 || nodes > 200000) return;
    nodes++;
    if (Array.isArray(v)) {
      for (const x of v) visit(x, depth + 1);
      return;
    }

    const subject = _pick(v, DETAIL_KEYS.subject);
    const startRaw = _pick(v, DETAIL_KEYS.start);
    if (typeof subject === "string" && startRaw !== undefined) {
      const key = detailKey(subject, _detailDateTime(startRaw));
      if (key) {
        const attendees = _detailAttendees(_pick(v, DETAIL_KEYS.attendees));
        const body = _stripHtml(_detailBody(_pick(v, DETAIL_KEYS.body)));
        let joinUrl = _pick(v, DETAIL_KEYS.joinUrl);
        if (typeof joinUrl !== "string") {
          const om = _pick(v, DETAIL_KEYS.onlineMeeting);
          joinUrl = (om && typeof om === "object") ? _pick(om, DETAIL_KEYS.joinUrl) : "";
        }
        if (typeof joinUrl !== "string") joinUrl = "";

        // Merge rather than overwrite: the same meeting can appear in
        // more than one response (a list call and a detail call), and
        // whichever one actually carried a field should win over a
        // later one that did not.
        const prev = found.get(key) || { attendees: [], body: "", joinUrl: "" };
        found.set(key, {
          attendees: attendees.length ? attendees : prev.attendees,
          body: body || prev.body,
          joinUrl: joinUrl || prev.joinUrl,
        });
      }
    }

    for (const k of Object.keys(v)) visit(v[k], depth + 1);
  };

  for (const b of bodies || []) visit(b, 0);
  return found;
}

// Fold captured detail into the events the DOM scan produced.
// ADDITIVE ONLY: a field the DOM already filled is never overwritten,
// and an event with no matching detail is returned byte-identical.
function mergeDetailIntoEvents(events, details) {
  const stats = { matched: 0, gainedAttendees: 0, gainedBody: 0, gainedJoinUrl: 0 };
  if (!details || !details.size) return { events, stats };

  for (const ev of events || []) {
    const d = details.get(detailKey(ev.subject, ev.start));
    if (!d) continue;
    stats.matched++;
    if (d.attendees.length && !(ev.attendees && ev.attendees.length)) {
      ev.attendees = d.attendees;
      stats.gainedAttendees++;
    }
    if (d.body && !ev.body) {
      // Bounded — the backend caps this too, but a runaway invite body
      // should not be carried across the wire to find that out.
      ev.body = d.body.slice(0, 8000);
      stats.gainedBody++;
    }
    if (d.joinUrl && !ev.join_url) {
      ev.join_url = d.joinUrl;
      stats.gainedJoinUrl++;
    }
  }
  return { events, stats };
}

const CAL_RECORDER_SCRIPT_ID = "mr-calendar-response-recorder";

// Registered just before the capture tab opens and removed straight
// after. Registering rather than injecting is what buys
// `document_start` — `executeScript` on an already-created tab cannot
// beat the page's own first request, which is exactly the request
// that carries the calendar.
async function registerCalendarRecorder() {
  try {
    try {
      await chrome.scripting.unregisterContentScripts({ ids: [CAL_RECORDER_SCRIPT_ID] });
    } catch (_) { /* not registered — fine */ }
    await chrome.scripting.registerContentScripts([{
      id: CAL_RECORDER_SCRIPT_ID,
      matches: [
        "https://outlook.office.com/*",
        "https://outlook.cloud.microsoft/*",
        "https://*.office.com/*",
        "https://*.cloud.microsoft/*",
      ],
      js: ["calendar-recorder.js"],
      runAt: "document_start",
      world: "MAIN",
      allFrames: true,
      persistAcrossSessions: false,
    }]);
    return true;
  } catch (e) {
    // Chrome < 111 has no MAIN-world content scripts. Capture still
    // works, just without detail — reported, never silently degraded.
    console.warn("[ext] calendar recorder registration failed:", e);
    return false;
  }
}

async function unregisterCalendarRecorder() {
  try {
    await chrome.scripting.unregisterContentScripts({ ids: [CAL_RECORDER_SCRIPT_ID] });
  } catch (_) { /* ignore */ }
}

async function settleAndCollectCalendar(tabId, label) {
  const start = Date.now();
  let text = "";
  let candidates = [];
  let scanDiag = {};
  const counts = [];
  let stabilized = false;

  while (Date.now() - start < CALENDAR_MAX_WAIT_MS) {
    try {
      text = (await readMainText(tabId)) || "";
    } catch (e) {
      console.warn(`[ext] calendar ${label}: text read failed:`, e);
    }
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId },
        func: _calendarDomScanFunc,
      });
      const scanResult = (result && result[0] && result[0].result) || {};
      candidates = scanResult.candidates || [];
      scanDiag = scanResult.diag || {};
    } catch (e) {
      console.warn(`[ext] calendar ${label}: DOM scan failed:`, e);
    }

    counts.push(candidates.length);

    if (shouldStopPolling(counts)) {
      stabilized = true;
      break;
    }
    // Fast path: plenty of fallback text AND at least one candidate
    // already — no reason to keep polling out the stability window.
    if (text.length >= CALENDAR_TARGET_CHARS && candidates.length > 0) {
      stabilized = true;
      break;
    }
    await sleep(POLL_MS);
  }

  console.log(
    `[ext] calendar ${label}: ${candidates.length} candidate(s) after ` +
    `${counts.length} poll(s) (stabilized=${stabilized}, ` +
    `iframesEntered=${scanDiag.iframesEntered || 0}, ` +
    `shadowRootsSeen=${scanDiag.shadowRootsSeen || 0}), ` +
    `${text.length} chars fallback text (below floor=` +
    `${text.length < CALENDAR_MIN_USEFUL_CHARS})`);
  return { candidates, text, scanDiag, stabilized };
}

// Full calendar capture: this week + next week (see the header
// comment on why one week view alone can miss the tail of the panel's
// 168h window), structured extraction with a text-scrape fallback.
// Returns { events, layer, stats, zeroReason, fallbackText, elapsedMs, diag }.
// Both detail mechanisms, run against the week that is ON SCREEN.
//
// WHY THIS IS PER-WEEK AND NOT ONCE AT THE END.
//
// v1.9 ran a single detail pass after BOTH weeks had been scanned —
// which means after `goToNextCalendarWeek` had already navigated away
// from the current week, with no navigation back. Mechanism 2 works by
// finding an event's tile by its aria-label and clicking it, and it
// only considers events starting inside DETAIL_WINDOW_HOURS (72h) —
// every one of which is a tile in the CURRENT week's view. So it went
// looking for tiles that were no longer rendered, matched nothing,
// counted everything as skipped, and returned in under a second having
// never opened a single event.
//
// Field evidence 2026-08-20: a whole calendar-only capture completed in
// ~21s. A pass that actually opened ~20 events cannot finish in less
// than a minute. The "second mechanism" shipped in v2.45.0 had never
// once clicked anything, and the counters that would have said so were
// being deleted by the store bug fixed alongside this.
//
// So detail is now collected while the relevant week is still
// rendered, and accumulated into a map that the final extraction folds
// in. Both mechanisms run per-week for the same reason: mechanism 1's
// captured bodies are harvested per-week too, and running it first
// means mechanism 2 only pays for what mechanism 1 could not fill.
async function collectWeekDetail(tabId, candidates, capturedBodies, diag, into,
                                 harvest, statusInto) {
  // What this week's tiles parse to. A separate extraction from the
  // final one, on this week's candidates only, so the subjects and
  // start times handed to the clicker correspond to tiles that are
  // actually on screen right now.
  const weekEvents = extractEventsFromCandidates(candidates, {
    fallbackYear: new Date().getFullYear(),
  }).events;
  if (!weekEvents.length) return;

  // ── Mechanism 1: Outlook's own responses ──────────────────────────
  // Cheap, needs no clicking. Applied first so mechanism 2 skips
  // anything already answered.
  const fromResponses = detailsFromResponses(capturedBodies);
  for (const [k, v] of fromResponses) {
    const prev = into.get(k) || { attendees: [], body: "", joinUrl: "" };
    into.set(k, {
      attendees: v.attendees.length ? v.attendees : prev.attendees,
      body: v.body || prev.body,
      joinUrl: v.joinUrl || prev.joinUrl,
    });
  }

  // ── Mechanism 2: the rendered event ───────────────────────────────
  try {
    const cutoff = Date.now() + DETAIL_WINDOW_HOURS * 3600 * 1000;
    // Ended meetings get a short grace (still useful minutes after a
    // call), then stop competing for clicks entirely.
    const floor = Date.now() - 4 * 3600 * 1000;
    const needing = weekEvents.filter((e) => {
      const already = into.get(detailKey(e.subject, e.start));
      if (already && (already.attendees.length || already.body || already.joinUrl)) {
        return false;
      }
      // Label-derived detail (v1.5's Zoom links) also counts as
      // answered — no reason to open an event we can already join.
      if ((e.attendees && e.attendees.length) || e.body || e.join_url) return false;
      const t = Date.parse(e.start);
      const tEnd = Date.parse(e.end);
      // FIELD ARITHMETIC 2026-08-20 22:07: 23 attempted, 11 opened,
      // then 12 consecutive misses — and the user's next-morning
      // meeting was among the missed. The 72h window had an upper
      // bound only, so the whole PAST week inside the current view
      // ("needing" detail nobody would ever read) queued ahead of
      // tomorrow's meetings and burned the opens. Upcoming meetings
      // are the product; finished ones are an archive.
      if (Number.isFinite(tEnd) && tEnd < floor) return false;
      return Number.isFinite(t) && t <= cutoff;
    });
    // Soonest first, for the same reason: whatever budget or breakage
    // cuts the run short must cut it at the meetings that matter
    // least, and the meeting the user will expand next is by
    // definition the soonest one.
    needing.sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
    diag.domDetailAttempted += needing.length;
    if (!needing.length) return;

    const r = await chrome.scripting.executeScript({
      target: { tabId },
      args: [
        needing.map((e) => ({ subject: e.subject, startIso: e.start })),
        JOIN_PROVIDER_PATTERNS,
        DETAIL_MAX_EVENTS,
        DETAIL_TIME_BUDGET_MS,
      ],
      func: _readEventDetailsFunc,
    });
    const got = (r && r[0] && r[0].result) || null;
    if (!got) return;

    diag.domDetailOpened += got.opened || 0;
    diag.domDetailGrew += got.grew || 0;
    diag.joinFromAnchor += got.joinFromAnchor || 0;

    // Record each event's outcome into the SHARED map so the final
    // merge (which runs on the final extraction, not these local
    // weekEvents) can stamp the POSTed events. An earlier draft
    // stamped weekEvents — a throwaway local — and the statuses would
    // have evaporated before the payload was built.
    for (const st of got.statuses || []) {
      const k = detailKey(st.subject, st.startIso);
      if (k && !statusInto.has(k)) statusInto.set(k, st.status);
    }
    diag.domDetailSkipped += got.skipped || 0;
    diag.domDetailNoTile += got.matchedElement != null
      ? Math.max(0, (needing.length) - (got.matchedElement || 0)) : 0;

    for (const d of got.details || []) {
      const k = detailKey(d.subject, d.startIso);
      if (!k) continue;
      const prev = into.get(k) || { attendees: [], body: "", joinUrl: "" };
      into.set(k, {
        attendees: (d.attendees && d.attendees.length) ? d.attendees : prev.attendees,
        body: d.body || prev.body,
        joinUrl: d.joinUrl || prev.joinUrl,
      });
    }

    // ── Mechanism 3: the responses the CLICKS just provoked ──────────
    //
    // The best source in the whole pipeline, and it was being thrown
    // away. Opening an event makes Outlook fetch that event's FULL
    // detail — the complete attendee list with real names, the invite
    // body, and the join URL as data rather than as scraped text.
    // The recorder is installed and captures every one of those
    // responses; nothing ever harvested them, because the only
    // harvests happened before the clicks.
    //
    // This is strictly better than reading the pane: it is the same
    // information Outlook itself renders FROM, so it needs no
    // assumption about markup, and it cannot mistake one meeting's
    // Join button for another's. Scraping stays as the fallback for a
    // tenant whose detail arrives some other way.
    if (typeof harvest === "function" && got.opened) {
      const before = capturedBodies.length;
      await harvest("post-click");
      if (capturedBodies.length > before) {
        const fresh = detailsFromResponses(capturedBodies.slice(before));
        diag.postClickBodies += capturedBodies.length - before;
        for (const [k, v] of fresh) {
          const prev = into.get(k) || { attendees: [], body: "", joinUrl: "" };
          // Richest wins: a full attendee list from the detail
          // response should replace a single scraped name, so this
          // compares LENGTH rather than merely taking the first
          // non-empty answer.
          const better = v.attendees.length > prev.attendees.length;
          if (better) diag.postClickImproved++;
          into.set(k, {
            attendees: better ? v.attendees : prev.attendees,
            body: v.body || prev.body,
            joinUrl: v.joinUrl || prev.joinUrl,
          });
        }
      }
    }
  } catch (e) {
    // Never fatal: the events are already extracted, and a capture
    // with no detail beats no capture at all.
    console.warn("[ext] screen detail pass failed:", e);
    diag.domDetailError = String((e && e.message) || e).slice(0, 200);
  }
}

async function captureCalendarTab() {
  // Registered BEFORE the tab exists. Outlook's calendar request is in
  // flight within a second of navigation, and a recorder installed
  // after that is a recorder that missed the response carrying the
  // attendees. See calendar-recorder.js.
  const recorderOn = await registerCalendarRecorder();
  const tab = await chrome.tabs.create({ url: CALENDAR_WEEK_URL, active: false });
  const tabId = tab.id;
  const start = Date.now();
  const allCandidates = [];
  const capturedBodies = [];
  const diag = {
    weeksScanned: 0, nextWeekNavOk: null, anyWeekUnstable: false,
    // Whether the detail path was even available this run. Reported
    // rather than inferred: "the recorder could not install" and "the
    // recorder ran and found nothing" are different facts, and
    // collapsing them is the exact defect this project keeps hitting.
    recorderRegistered: recorderOn,
    recorderInstalled: false,
    responsesSeen: 0,
    responsesMatched: 0,
    responsesDropped: 0,
    responsesNotMeetingShaped: 0,
    // Whether the recorder had to be injected after the fact because
    // registration had not taken effect for this tab yet. See
    // ensureRecorderInstalled.
    recorderInjectedLate: false,
    recorderReloaded: false,
    // Mechanism 2 counters, accumulated across weeks.
    domDetailAttempted: 0,
    domDetailOpened: 0,
    domDetailGrew: 0,
    domDetailSkipped: 0,
    domDetailNoTile: 0,
    // Mechanism 3 — responses the clicks provoked.
    postClickBodies: 0,
    postClickImproved: 0,
    joinFromAnchor: 0,
  };
  // Detail gathered from BOTH mechanisms, keyed by subject|start, while
  // the relevant week was still rendered. Folded into the final
  // extraction below.
  const detailByKey = new Map();
  // detailKey -> "opened" | "opened_empty" | "no_tile" | "budget".
  // Stamped onto the FINAL events below so the app can tell the user
  // why a given meeting has no detail.
  const detailStatusByKey = new Map();
  let fallbackText = "";
  // Assigned inside the try (the DOM detail pass needs the live tab).
  // Left null if the try threw before extraction, which the guard
  // below turns into an honest empty result rather than a crash.
  let extraction = null;

  // Reads back and clears whatever the page recorded so far.
  const harvest = async (label) => {
    try {
      const r = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: _harvestCalendarResponses,
      });
      const got = (r && r[0] && r[0].result) || null;
      if (!got) return;
      if (got.installed) diag.recorderInstalled = true;
      diag.responsesSeen += got.seen || 0;
      diag.responsesMatched += got.matched || 0;
      diag.responsesDropped += got.dropped || 0;
      diag.responsesNotMeetingShaped += got.notMeetingShaped || 0;
      capturedBodies.push(...(got.bodies || []));
    } catch (e) {
      console.warn(`[ext] calendar ${label}: harvest failed:`, e);
    }
  };

  // registerContentScripts resolves before Chrome has necessarily
  // applied the registration to a tab that is ALREADY navigating —
  // and this tab was created microseconds after the call. If the
  // recorder is not in the page, injecting it now is too late for the
  // requests already made, so the page is reloaded with the recorder
  // guaranteed present.
  //
  // This exists because the alternative is indistinguishable from
  // every other failure: empty fields and no way to tell why.
  const ensureRecorderInstalled = async () => {
    try {
      const probe = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: () => !!window.__mrCalRecorderInstalled,
      });
      if (probe && probe[0] && probe[0].result) return;

      diag.recorderInjectedLate = true;
      await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        files: ["calendar-recorder.js"],
      });
      // Injected AFTER Outlook's first calendar fetch, so that fetch
      // was missed. Reload with the recorder already resident.
      await chrome.tabs.reload(tabId);
      await waitForTabComplete(tabId);
      diag.recorderReloaded = true;
    } catch (e) {
      console.warn("[ext] recorder install check failed:", e);
    }
  };

  try {
    await waitForTabComplete(tabId);
    if (recorderOn) await ensureRecorderInstalled();

    const week1 = await settleAndCollectCalendar(tabId, "week1 (current)");
    allCandidates.push(...week1.candidates);
    fallbackText = week1.text;
    diag.weeksScanned += 1;
    if (!week1.stabilized) diag.anyWeekUnstable = true;
    await harvest("week1");
    // BEFORE navigating away. Mechanism 2 clicks tiles, and week 1's
    // tiles are only on screen now — this is the bug that made the
    // whole screen-reading mechanism a no-op in v2.45.0.
    await collectWeekDetail(tabId, week1.candidates, capturedBodies, diag,
                            detailByKey, harvest, detailStatusByKey);

    const navOk = await goToNextCalendarWeek(tabId);
    diag.nextWeekNavOk = navOk;
    if (navOk) {
      const week2 = await settleAndCollectCalendar(tabId, "week2 (next)");
      allCandidates.push(...week2.candidates);
      if (week2.text) fallbackText += "\n\n" + week2.text;
      diag.weeksScanned += 1;
      if (!week2.stabilized) diag.anyWeekUnstable = true;
      await harvest("week2");
      await collectWeekDetail(tabId, week2.candidates, capturedBodies, diag,
                              detailByKey, harvest, detailStatusByKey);
    }

    // Final extraction over BOTH weeks, then fold in the detail that
    // was gathered while each week was rendered. Inside the try
    // because the finally below closes the tab.
    extraction = extractEventsFromCandidates(allCandidates, {
      fallbackYear: new Date().getFullYear(),
    });
    const merged = mergeDetailIntoEvents(extraction.events, detailByKey);
    diag.detailMatched = merged.stats.matched;
    diag.detailGainedAttendees = merged.stats.gainedAttendees;
    diag.detailGainedBody = merged.stats.gainedBody;
    diag.detailGainedJoinUrl = merged.stats.gainedJoinUrl;
    extraction.stats.withAttendees = merged.stats.gainedAttendees;
    extraction.stats.withBody = merged.stats.gainedBody;
    extraction.stats.withJoinUrl =
      (extraction.stats.withJoinUrl || 0) + merged.stats.gainedJoinUrl;
    // Stamp each POSTed event with its click-pass outcome, so the app
    // can say "the capture could not find this meeting's tile" instead
    // of a cause-blind "(No description on this invite.)".
    for (const ev of extraction.events) {
      const st = detailStatusByKey.get(detailKey(ev.subject, ev.start));
      if (st) ev.detail_status = st;
    }
  } finally {
    try { await chrome.tabs.remove(tabId); } catch (_) { /* ignore */ }
    // Unregistered whatever happened: this must not stay installed on
    // the user's Outlook between captures.
    await unregisterCalendarRecorder();
  }

  if (!extraction) {
    extraction = { events: [], stats: { scanned: allCandidates.length, parsed: 0 } };
  }

  const zeroReason = extraction.events.length === 0
    ? classifyZeroReason(extraction.stats, { stillRendering: diag.anyWeekUnstable })
    : null;

  return {
    events: extraction.events,
    layer: dominantLayer(extraction.stats),
    stats: extraction.stats,
    zeroReason,
    fallbackText: fallbackText.trim(),
    elapsedMs: Date.now() - start,
    diag,
  };
}

function _todayIsoLocal() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// The periodic, calendar-ONLY refresh (chrome.alarms, every
// CALENDAR_REFRESH_MINUTES — see setupAlarms). Deliberately does NOT
// touch the Today-tab briefing pipeline: it posts calendar_events (or
// calendar_text as a last resort) and nothing else, so the backend
// updates only the Record tab's calendar store, never spends an LLM
// call on every tick, and never overwrites today's saved greeting /
// top_priority / needs_response with a partial calendar-only parse.
// Counts and booleans ONLY — no URL, subject, attendee or body text.
// ONE builder for BOTH POST paths.
//
// Field incident 2026-08-20/21, twice over: capture_diag was attached
// only on the calendar-only alarm path, so every capture the user
// triggered THEMSELVES — the popup's Capture & Send, the button they
// press precisely when investigating a problem — reported nothing.
// Three consecutive diagnostic zips carried a stale alarm-path diag
// while the runs under investigation were the invisible manual ones.
// The path a user reaches for when things are broken is the LAST path
// that may go unreported.
function buildCaptureDiag(capture) {
  const d = capture && capture.diag;
  return {
    recorderRegistered: !!d?.recorderRegistered,
    recorderInstalled: !!d?.recorderInstalled,
    recorderInjectedLate: !!d?.recorderInjectedLate,
    recorderReloaded: !!d?.recorderReloaded,
    responsesSeen: d?.responsesSeen || 0,
    responsesMatched: d?.responsesMatched || 0,
    responsesDropped: d?.responsesDropped || 0,
    responsesNotMeetingShaped: d?.responsesNotMeetingShaped || 0,
    detailMatched: d?.detailMatched || 0,
    detailGainedAttendees: d?.detailGainedAttendees || 0,
    detailGainedBody: d?.detailGainedBody || 0,
    detailGainedJoinUrl: d?.detailGainedJoinUrl || 0,
    domDetailAttempted: d?.domDetailAttempted || 0,
    domDetailOpened: d?.domDetailOpened || 0,
    domDetailGrew: d?.domDetailGrew || 0,
    domDetailSkipped: d?.domDetailSkipped || 0,
    domDetailNoTile: d?.domDetailNoTile || 0,
    postClickBodies: d?.postClickBodies || 0,
    postClickImproved: d?.postClickImproved || 0,
    joinFromAnchor: d?.joinFromAnchor || 0,
    eventsExtracted: (capture && capture.events && capture.events.length) || 0,
  };
}

async function captureCalendarOnly(backendUrl, token) {
  if (!backendUrl || !token) {
    // Every OTHER return path in this function already persists
    // lastCalendarCaptureAt/lastCalendarResult (capture failure,
    // zero-events, fetch failure) — this guard is the one exception
    // that didn't (see the v1.3.1 comment on the onAlarm listener).
    // The current sole caller already checks backendUrl/token itself
    // before calling in, so this is unreachable in practice today,
    // but a silent gap here would resurface the exact bug being fixed
    // the moment anything else calls this function directly.
    const result = { ok: false, error: "Backend URL or token not configured." };
    await chrome.storage.local.set({
      lastCalendarCaptureAt: Date.now(), lastCalendarResult: result,
    });
    return result;
  }
  console.log("[ext] starting calendar-only refresh");

  let capture;
  try {
    capture = await captureCalendarTab();
  } catch (e) {
    const result = { ok: false, error: `Calendar capture failed: ${e.message || String(e)}` };
    await chrome.storage.local.set({ lastCalendarCaptureAt: Date.now(), lastCalendarResult: result });
    return result;
  }

  const payload = { date: _todayIsoLocal(), extension_version: currentExtensionVersion() };
  // Counts and booleans ONLY — no URL, subject, attendee or body text.
  //
  // This is sent because v1.7 shipped these counters into the capture
  // result and nowhere else: the app's diagnostics bundle could not
  // see them, so a field failure still looked like every other field
  // failure. Telling the user "send me the diagnostics and it will say
  // which case this is" and then shipping a bundle that could not say
  // is the same defect as the rest of this saga, committed one layer
  // out.
  payload.capture_diag = buildCaptureDiag(capture);
  if (capture.events.length > 0) {
    payload.calendar_events = capture.events;
  } else if (capture.fallbackText) {
    payload.calendar_text = capture.fallbackText;
  } else {
    const result = {
      ok: false,
      error: `Calendar tab returned nothing (0 structured events, 0 fallback ` +
        `text — ${capture.zeroReason || "unknown reason"}). Is Outlook Web ` +
        `signed in? Try Capture & Send from the popup, or Diagnose calendar ` +
        `capture in Settings.`,
      zeroReason: capture.zeroReason || null,
      stats: capture.stats,
    };
    await chrome.storage.local.set({ lastCalendarCaptureAt: Date.now(), lastCalendarResult: result });
    return result;
  }

  try {
    const res = await fetch(`${backendUrl}/briefing/extension-import`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      const result = { ok: false, error: `Backend returned ${res.status}: ${body.slice(0, 200)}` };
      await chrome.storage.local.set({ lastCalendarCaptureAt: Date.now(), lastCalendarResult: result });
      return result;
    }
    const data = await res.json().catch(() => ({}));
    const result = {
      ok: true,
      eventCount: capture.events.length,
      layer: capture.layer,
      stats: capture.stats,
      zeroReason: capture.zeroReason || null,
      weeksScanned: capture.diag.weeksScanned,
      backendKept: typeof data.kept_events === "number" ? data.kept_events : null,
      ts: Date.now(),
    };
    await chrome.storage.local.set({ lastCalendarCaptureAt: Date.now(), lastCalendarResult: result });
    console.log(
      `[ext] calendar-only refresh sent: ${capture.events.length} event(s) ` +
      `(layer=${capture.layer || "text-fallback"}) → kept=${result.backendKept}`);
    return result;
  } catch (e) {
    const result = {
      ok: false,
      error: `Couldn't reach ${backendUrl} — is Meeting Recorder running? (${e.message})`,
    };
    await chrome.storage.local.set({ lastCalendarCaptureAt: Date.now(), lastCalendarResult: result });
    return result;
  }
}

// ──────────────────────────────────────────────────────────────────
// Diagnose calendar capture (v1.3) — one-click, read-only DOM probe.
//
// The user is not asked to open DevTools or paste a console script:
// clicking "Diagnose calendar capture" in Settings opens the same
// calendar tab the real capture uses, runs this probe against it at
// several points in time, and the options page renders the result
// with a Copy button. The probe is read-only — it never clicks
// anything and never navigates.
//
// It stays self-contained (no closures over this file's module scope)
// for the same chrome.scripting.executeScript constraint as
// _calendarDomScanFunc above — which is why the regex sources and the
// join-link vocabulary arrive as ARGS rather than being read from
// module scope directly.
//
// The container/label half of this probe used to be untested because
// the fake DOM in chrome-extension/tests/ had no CSS selector engine.
// That double now supports compound selectors and one descendant
// combinator, so the join-link half added below IS covered there
// (association by containment/adjacency, provider classification, and
// — most importantly — that a redacted example can never carry the
// credential, the query string, or the customer's host).
// ──────────────────────────────────────────────────────────────────

function _calendarDiagnosticProbeFunc(timeRangeSource, timeHintSource, joinConfig) {
  try {
    // Discover every reachable root: the top document, every
    // same-origin iframe document, and every open shadow root —
    // BFS, so nested iframes/shadow roots (an iframe inside a shadow
    // root, or vice versa) are still found.
    function collectRoots() {
      const entries = [{ root: document, kind: "document" }];
      const seen = new Set([document]);
      let shadowRootCount = 0, iframeCount = 0, iframeAccessibleCount = 0;
      let i = 0;
      while (i < entries.length) {
        const root = entries[i++].root;
        let all;
        try { all = root.querySelectorAll("*"); } catch (_) { all = []; }
        for (const el of all) {
          if (el.tagName === "IFRAME") {
            iframeCount++;
            let doc = null;
            try { doc = el.contentDocument; } catch (_) { doc = null; }
            if (doc && !seen.has(doc)) {
              seen.add(doc);
              entries.push({ root: doc, kind: "iframe" });
              iframeAccessibleCount++;
            }
          }
          if (el.shadowRoot && !seen.has(el.shadowRoot)) {
            shadowRootCount++;
            seen.add(el.shadowRoot);
            entries.push({ root: el.shadowRoot, kind: "shadow" });
          }
        }
      }
      return { entries, shadowRootCount, iframeCount, iframeAccessibleCount };
    }

    function countAcross(entries, selector) {
      let n = 0;
      for (const e of entries) {
        try { n += e.root.querySelectorAll(selector).length; } catch (_) { /* ignore */ }
      }
      return n;
    }

    const { entries, shadowRootCount, iframeCount, iframeAccessibleCount } = collectRoots();

    const containerCounts = {
      '[role="grid"]': countAcross(entries, '[role="grid"]'),
      '[role="main"]': countAcross(entries, '[role="main"]'),
      '[role="application"]': countAcross(entries, '[role="application"]'),
      '[data-app-section]': countAcross(entries, '[data-app-section]'),
    };
    const eventNodeCounts = {
      '[role="button"][aria-label]': countAcross(entries, '[role="button"][aria-label]'),
      'div[role="button"]': countAcross(entries, 'div[role="button"]'),
      '[role="gridcell"] [role="button"]': countAcross(entries, '[role="gridcell"] [role="button"]'),
      '[data-automationid]': countAcross(entries, '[data-automationid]'),
      '[role="listitem"]': countAcross(entries, '[role="listitem"]'),
      'div[draggable="true"]': countAcross(entries, 'div[draggable="true"]'),
    };

    const ariaEls = [];
    for (const e of entries) {
      try { ariaEls.push(...e.root.querySelectorAll("[aria-label]")); } catch (_) { /* ignore */ }
    }
    const labels = ariaEls
      .map((el) => el.getAttribute("aria-label") || "")
      .filter(Boolean);
    const longestLabels = [...labels].sort((a, b) => b.length - a.length).slice(0, 25);

    const timeRangeRe = new RegExp(timeRangeSource, "i");
    const timeHintRe = new RegExp(timeHintSource);
    const timeRangeMatchCount = labels.filter((l) => timeRangeRe.test(l)).length;
    const timeHintMatchCount = labels.filter((l) => timeHintRe.test(l)).length;

    // ── Join-link probe (read-only; never emits a full URL) ─────────
    //
    // Answers one question — is a meeting join link reachable from what
    // the real capture already reads? — by looking in the TWO places a
    // URL can be, and reporting them separately:
    //
    //   ANCHORS. How many join-shaped `a[href]` exist at all, how many
    //   sit INSIDE an element whose aria-label is meeting-shaped (safe
    //   to associate by containment), and how many merely sit NEXT TO
    //   one (weaker, but still positional-free). Links that are neither
    //   are counted as unassociated: those could only be matched by
    //   grid position, which is precisely the association a wrong
    //   answer comes from, so they are reported, never used.
    //
    //   LABEL TEXT. How many join-shaped URLs appear as TEXT inside an
    //   aria-label, and of those, how many inside a MEETING-shaped one
    //   (where the URL is part of that meeting's own description and
    //   needs no association at all). v1.4's probe had only the anchor
    //   half and reported its zero as "join_url cannot be filled from
    //   this DOM" — while a Zoom join URL sat in the label text of the
    //   same report. Non-conferencing URLs in labels are counted too,
    //   separately: those are locations, not join links, and conflating
    //   them is what the verdict must never do.
    function joinLinkProbe() {
      const cfg = joinConfig || {};
      const providers = (cfg.providers || []).map((p) => ({
        name: p.name,
        hostRe: new RegExp(p.host, "i"),
        pathRe: new RegExp(p.path, "i"),
      }));
      const safeHosts = new Set(cfg.safeHosts || []);
      const safeSegments = new Set(cfg.safePathSegments || []);
      const maxExamples = cfg.maxExamples || 3;
      const maxUp = cfg.maxAncestorHops || 6;

      const out = {
        anchorCount: 0,
        matchCount: 0,
        byProvider: {},
        insideMeetingLabelledElement: 0,
        adjacentToMeetingLabelledElement: 0,
        unassociated: 0,
        redactedExamples: [],
        // The half v1.4 never asked about — URLs living in aria-label
        // TEXT rather than in an anchor element.
        labelUrlCount: 0,
        labelJoinCount: 0,
        labelJoinInMeetingShapedLabel: 0,
        labelNonJoinUrlCount: 0,
        labelByProvider: {},
        labelRedactedExamples: [],
      };
      for (const p of providers) out.byProvider[p.name] = 0;
      for (const p of providers) out.labelByProvider[p.name] = 0;

      // Host + path SHAPE only. Query strings are dropped whole and
      // every non-structural path segment is elided — a join URL is a
      // single-use meeting credential, and this report gets pasted
      // into a chat window.
      function redact(href) {
        let u;
        try { u = new URL(href, location.href); } catch (_) { return null; }
        const host = u.host.toLowerCase();
        const shownHost = safeHosts.has(host)
          ? host
          : (host.split(".").length > 2
            ? "*." + host.split(".").slice(-2).join(".")
            : host);
        const segs = (u.pathname || "/").split("/").filter(Boolean)
          .map((s) => (safeSegments.has(s.toLowerCase()) ? s : "…"));
        return shownHost + "/" + segs.join("/") + (u.search ? "?…" : "");
      }

      function providerFor(href) {
        let u;
        try { u = new URL(href, location.href); } catch (_) { return null; }
        for (const p of providers) {
          if (p.hostRe.test(u.host) && p.pathRe.test(u.pathname || "")) return p.name;
        }
        return null;
      }

      function labelIsMeetingShaped(el) {
        const a = el && el.getAttribute ? el.getAttribute("aria-label") : null;
        return !!(a && timeRangeRe.test(a));
      }

      // Containment: this anchor is a descendant of (or is) an element
      // whose own aria-label describes a meeting.
      function insideMeetingLabelled(el) {
        let node = el;
        for (let i = 0; node && i <= maxUp; i++, node = node.parentElement) {
          if (labelIsMeetingShaped(node)) return true;
        }
        return false;
      }

      // Adjacency: a sibling (or a sibling's descendant) of this anchor
      // or of one of its near ancestors carries a meeting-shaped label.
      function adjacentToMeetingLabelled(el) {
        let node = el;
        for (let i = 0; node && i <= maxUp; i++, node = node.parentElement) {
          const parent = node.parentElement;
          if (!parent || !parent.children) continue;
          for (const kid of parent.children) {
            if (kid === node) continue;
            if (labelIsMeetingShaped(kid)) return true;
            let inner = [];
            try { inner = kid.querySelectorAll("[aria-label]"); } catch (_) { inner = []; }
            for (const d of inner) if (labelIsMeetingShaped(d)) return true;
          }
        }
        return false;
      }

      const anchors = [];
      for (const e of entries) {
        try {
          anchors.push(...e.root.querySelectorAll('a[href], area[href], [role="link"][href]'));
        } catch (_) { /* ignore */ }
      }
      out.anchorCount = anchors.length;

      const seenShapes = new Set();
      for (const a of anchors) {
        const href = a.getAttribute ? a.getAttribute("href") : null;
        if (!href) continue;
        const provider = providerFor(href);
        if (!provider) continue;
        out.matchCount++;
        out.byProvider[provider] = (out.byProvider[provider] || 0) + 1;
        if (insideMeetingLabelled(a)) out.insideMeetingLabelledElement++;
        else if (adjacentToMeetingLabelled(a)) out.adjacentToMeetingLabelledElement++;
        else out.unassociated++;
        const shape = redact(href);
        if (shape && !seenShapes.has(shape) && out.redactedExamples.length < maxExamples) {
          seenShapes.add(shape);
          out.redactedExamples.push({ provider, shape });
        }
      }

      // Second place: URL-shaped TEXT inside an aria-label. `labels` is
      // every aria-label on the page (collected above); a label that is
      // also meeting-shaped is the strong case, since the URL is then
      // part of that meeting's own description.
      const urlInTextSrc = "https?://[^\\s,;]+";
      for (const l of labels) {
        const re = new RegExp(urlInTextSrc, "gi");
        let hit;
        const labelIsMeeting = timeRangeRe.test(l);
        while ((hit = re.exec(l)) !== null) {
          const url = hit[0].replace(/[.,;:!?)\]}>"']+$/, "");
          if (!url) continue;
          out.labelUrlCount++;
          const provider = providerFor(url);
          if (!provider) { out.labelNonJoinUrlCount++; continue; }
          out.labelJoinCount++;
          out.labelByProvider[provider] = (out.labelByProvider[provider] || 0) + 1;
          if (labelIsMeeting) out.labelJoinInMeetingShapedLabel++;
          const shape = redact(url);
          if (shape && out.labelRedactedExamples.length < maxExamples
              && !out.labelRedactedExamples.some((e) => e.shape === shape)) {
            out.labelRedactedExamples.push({ provider, shape, where: "aria-label text" });
          }
        }
      }

      const associated = out.insideMeetingLabelledElement +
        out.adjacentToMeetingLabelledElement;
      const inLabels = out.labelJoinInMeetingShapedLabel;
      // Four distinct answers, never collapsed: usable from anchors /
      // usable from label text / present but only positionally
      // associable / genuinely absent from BOTH places. The last one is
      // the only negative, and it now names what was checked — v1.4's
      // version said "cannot be filled from this DOM" on the strength
      // of the anchor count alone, which was a confident negative about
      // a question it had not asked.
      const alsoInLabels = inLabels > 0
        ? `, plus ${inLabels} in meeting-shaped aria-label TEXT`
        : "";
      if (out.matchCount > 0 && associated > 0) {
        out.verdict =
          `found ${out.matchCount} join-shaped anchor link(s), ${associated} of them ` +
          `associable by containment/adjacency${alsoInLabels} — join_url IS fillable ` +
          `from the DOM the capture already scans`;
      } else if (inLabels > 0) {
        out.verdict =
          `found ${inLabels} join-shaped URL(s) in the TEXT of a meeting-shaped ` +
          `aria-label (anchors: ${out.matchCount} join-shaped, ${associated} associable) — ` +
          `join_url IS fillable from the label the capture already parses, no anchor ` +
          `needed; this is the path extractUrlsFromLabel takes`;
      } else if (out.matchCount > 0) {
        out.verdict =
          `found ${out.matchCount} join-shaped anchor link(s), but none inside or ` +
          `adjacent to a meeting-shaped aria-label and none in label text — associating ` +
          `them would have to be positional, which is not safe enough to use`;
      } else {
        out.verdict =
          `no join-shaped links anywhere in the scanned roots — BOTH places checked: ` +
          `${out.anchorCount} anchor(s) carried none, and ${out.labelUrlCount} URL(s) in ` +
          `aria-label text were all non-conferencing (${out.labelNonJoinUrlCount} of them, ` +
          `i.e. locations rather than meetings to join). join_url is genuinely absent ` +
          `here — a Teams-only calendar looks exactly like this, since a Teams event's ` +
          `Location is the words "Microsoft Teams Meeting" and never the URL`;
      }
      return out;
    }

    let joinLinks;
    try {
      joinLinks = joinLinkProbe();
    } catch (e) {
      joinLinks = { error: String((e && e.message) || e) };
    }

    // First grid container found (in root-discovery order — document
    // first, then iframes/shadow roots in BFS order), so we know
    // whether the calendar grid itself lives inside an iframe.
    let gridEl = null, gridInsideIframe = false;
    for (const e of entries) {
      let found = null;
      try { found = e.root.querySelector('[role="grid"]'); } catch (_) { /* ignore */ }
      if (found) { gridEl = found; gridInsideIframe = e.kind === "iframe"; break; }
    }
    const gridInnerText = gridEl
      ? ((gridEl.innerText != null ? gridEl.innerText : gridEl.textContent) || "")
      : "";

    return {
      finalUrl: location.href,
      containerCounts,
      eventNodeCounts,
      ariaLabelCount: ariaEls.length,
      longestLabels,
      patternsTried: [
        { name: "full time-range (subject + start + end, what the real parser requires)", source: timeRangeSource, matchCount: timeRangeMatchCount },
        { name: "any clock-time hint (looser — what the DOM scan uses to pick candidates)", source: timeHintSource, matchCount: timeHintMatchCount },
      ],
      iframeCount,
      iframeAccessibleCount,
      shadowRootCount,
      gridInsideIframe,
      gridInnerTextSample: gridInnerText.slice(0, 2000),
      joinLinks,
    };
  } catch (e) {
    return { error: String((e && e.message) || e) };
  }
}

// Open the calendar tab, run the diagnostic probe at
// DIAGNOSTIC_SNAPSHOT_DELAYS_MS after load, and return a report the
// options page renders directly (JSON, via a Copy button — see the
// v1.3 header comment). Read-only: never clicks, never navigates.
// ──────────────────────────────────────────────────────────────────
// Calendar API probe (v1.6) — CAN we read a meeting's detail without
// opening it?
// ──────────────────────────────────────────────────────────────────
//
// WHY THIS IS A PROBE AND NOT AN IMPLEMENTATION.
//
// Three fields are empty on every extension-sourced meeting, and all
// three are empty for ONE reason: they are not in the calendar grid.
//
//   * `join_url` for a Teams meeting. Teams writes the literal words
//     "Microsoft Teams Meeting" into Location; the URL lives in the
//     invite body. (A Zoom/Webex add-in DOES write its URL into
//     Location, which is why v1.5 fills those and only those — field
//     data: 1 of 25 labels carried a URL, and extraction got it.)
//   * `attendees`. The grid label names only the organiser, so the
//     Record tab reads "ATTENDEES (0) / None listed." forever.
//   * the invite body / agenda, which renders as "(No description on
//     this invite.)" on every row.
//
// The DOM has been asked and has answered: `anchorCount: 0`,
// `labelJoinCount: 1` across a whole week. There is nothing further to
// scrape. The remaining routes are (a) open all ~25 events one by one
// — slow, visibly drives the user's calendar, and re-introduces
// exactly the DOM dependency that broke capture for weeks — or (b)
// ask the same API that OWA itself asks, from inside the user's
// already-authenticated session.
//
// (b) is the better architecture and it is what this probe measures.
// It does NOT measure it by reasoning about it. Twice now this project
// has shipped a confident verdict about a page it could not see:
// v1.4's join-link probe searched for anchors, found none, and
// reported that join links "cannot be filled from this DOM" while its
// own output carried one as text. The rule that came out of that:
//
//     A result you could not read must never render as a result that
//     is not there.
//
// Which endpoint answers, whether it needs a canary token, whether the
// response actually carries attendees/body/join URL — those are facts
// about a live authenticated tenant, and nothing in this repository
// can observe them. So this ships as a MEASUREMENT first. It tries
// each candidate, reports exactly what each one did, and the
// implementation is written against whatever the field run says
// rather than against what seemed likely here.
//
// FOUR verdict states, deliberately, because the failure that matters
// is the one that reads like success:
//
//   usable            — answered 2xx AND carries the fields we need
//   answered-thin     — answered 2xx but the payload lacks them
//   auth-rejected     — 401/403: reachable, we are not entitled
//   unreachable       — network error / non-JSON / no response
//
// "answered-thin" exists so an endpoint that returns a tidy 200 of the
// wrong shape can never be recorded as working, and "auth-rejected" so
// a permissions problem is never filed as "the API does not exist".
//
// PRIVACY. This probe reads real calendar data and reports NONE of it.
// Per candidate it emits a status code, a content type, a byte count,
// which FIELD NAMES were present, and how many items came back. No
// subject, no attendee, no body text, no URL, and no token — the
// canary is reported as present/absent, never as a value.

// Candidate requests, most-likely first. Each is a pure description;
// the injected function builds the actual fetch. Kept out here rather
// than inlined so the list is reviewable in one place and so a future
// candidate is a data change, not a code change.
//
// `needsCanary` marks the OWA `service.svc` family: those reject a
// request whose X-OWA-CANARY header does not match the cookie of the
// same name (CSRF defence). The REST/Graph candidates use bearer auth
// instead and are expected to 401 from a cookie-only call — that is a
// RESULT, not a bug, and is why "auth-rejected" is its own state.
const CALENDAR_API_CANDIDATES = [
  {
    name: "owa-service-findItem",
    note: "OWA's own EWS-over-JSON endpoint, the one the calendar grid uses",
    path: "/owa/service.svc?action=FindItem",
    method: "POST",
    needsCanary: true,
    action: "FindItem",
  },
  {
    name: "owa-service-getCalendarView",
    note: "same service, calendar-view shaped request",
    path: "/owa/service.svc?action=GetCalendarView",
    method: "POST",
    needsCanary: true,
    action: "GetCalendarView",
  },
  {
    name: "owa-0-service-findItem",
    note: "newer /owa/0/ path prefix seen on some tenants",
    path: "/owa/0/service.svc?action=FindItem",
    method: "POST",
    needsCanary: true,
    action: "FindItem",
  },
  {
    name: "rest-v2-calendarview",
    note: "Outlook REST v2 on the same origin (may accept session auth)",
    path: "/api/v2.0/me/calendarview",
    method: "GET",
    needsCanary: false,
    action: "",
  },
];

// Field names worth finding in a response. Presence of these is what
// separates "usable" from "answered-thin". Matched case-insensitively
// against the response's KEY NAMES only — never against values, so a
// meeting whose subject happens to contain the word "attendees"
// cannot make a thin endpoint look usable.
const CALENDAR_API_WANTED_KEYS = {
  attendees: ["attendees", "requiredattendees", "optionalattendees"],
  body: ["body", "bodypreview", "description", "textbody"],
  joinUrl: ["joinurl", "onlinemeeting", "onlinemeetingurl", "skypeteamsmeetingurl",
            "onlinemeetinginformation", "location", "locations"],
  timing: ["start", "end", "starttime", "endtime", "originalstart"],
};

// Runs IN THE PAGE (chrome.scripting.executeScript), which is the
// whole point: fetch() from there is same-origin against
// outlook.office.com, so the session cookies, the canary and the
// tenant routing all come along without this extension ever handling
// a credential. A service-worker fetch would be cross-origin and
// would have to be granted and manage auth itself.
//
// Returns data ABOUT the responses, never the responses.
async function _calendarApiProbeFunc(candidates, wantedKeys, windowDays) {
  const out = {
    origin: "",
    canaryPresent: false,
    mailboxHintPresent: false,
    results: [],
    error: null,
  };

  // Deliberately reports presence, never the value: the canary is a
  // CSRF token and a diagnostics bundle is a file users email around.
  const readCookie = (name) => {
    try {
      const hit = (document.cookie || "").split(";")
        .map((c) => c.trim())
        .find((c) => c.toLowerCase().startsWith(name.toLowerCase() + "="));
      return hit ? hit.slice(hit.indexOf("=") + 1) : "";
    } catch (_) { return ""; }
  };

  try {
    out.origin = location.origin;
    const canary = readCookie("X-OWA-CANARY");
    out.canaryPresent = !!canary;
    // Any of these tells us which mailbox to anchor a request to.
    // Presence only — it is an address.
    out.mailboxHintPresent = !!(readCookie("X-AnchorMailbox")
      || readCookie("DefaultAnchorMailbox"));

    const now = new Date();
    const startIso = new Date(now.getTime() - 86400000).toISOString();
    const endIso = new Date(now.getTime() + windowDays * 86400000).toISOString();

    // The largest array of objects in the response — the meeting
    // items, whatever this shape happens to call them (`Items`,
    // `value`, `Events`). Returned rather than just counted, because
    // the field scan below has to run INSIDE it and nowhere else.
    const findItemArray = (value) => {
      let best = [];
      const walk = (v, depth) => {
        if (v === null || typeof v !== "object" || depth > 12) return;
        if (Array.isArray(v)) {
          if (v.length > best.length && v.some((x) => x && typeof x === "object")) best = v;
          for (const x of v.slice(0, 50)) walk(x, depth + 1);
          return;
        }
        for (const k of Object.keys(v)) walk(v[k], depth + 1);
      };
      walk(value, 0);
      return best;
    };

    // Collect KEY NAMES only, and ONLY from within the meeting items.
    //
    // Scanning the whole response instead is a false-positive machine,
    // and the tests caught it doing exactly that: an EWS reply is
    // wrapped in a top-level `Body` envelope (the SOAP body), which
    // collides with a meeting's own `Body` — so EVERY EWS response
    // scored `body: true` and read as "usable" no matter how empty it
    // was. That is the same defect as v1.4's join-link probe in a new
    // costume: a question whose shape guarantees the answer.
    //
    // Values are never read, so no calendar content can reach the
    // report even by accident. Depth- and node-capped: a calendar
    // response is large and an unbounded walk would hang the tab.
    const keyNamesIn = (items) => {
      const found = new Set();
      let nodes = 0;
      const walk = (v, depth) => {
        if (v === null || typeof v !== "object" || depth > 10 || nodes > 20000) return;
        nodes++;
        if (Array.isArray(v)) { for (const x of v.slice(0, 50)) walk(x, depth + 1); return; }
        for (const k of Object.keys(v)) {
          found.add(k.toLowerCase());
          walk(v[k], depth + 1);
        }
      };
      for (const item of (items || []).slice(0, 50)) walk(item, 0);
      return found;
    };

    for (const cand of candidates) {
      const entry = {
        name: cand.name,
        note: cand.note,
        method: cand.method,
        // The PATH only. The origin is reported once above and the
        // path carries no mailbox or meeting identifier.
        path: cand.path,
        status: null,
        ok: false,
        contentType: "",
        bytes: 0,
        json: false,
        itemCount: 0,
        // Which of the fields we actually need showed up, by name.
        fieldsPresent: {},
        skipped: "",
        verdict: "",
        error: null,
      };

      if (cand.needsCanary && !canary) {
        // NOT recorded as "unreachable" — we never asked. Collapsing
        // "did not ask" into "does not work" is the exact defect this
        // whole probe exists because of.
        entry.skipped = "no X-OWA-CANARY cookie in this session";
        entry.verdict = "not-attempted";
        out.results.push(entry);
        continue;
      }

      try {
        const headers = { Accept: "application/json" };
        let body;
        if (cand.method === "POST") {
          headers["Content-Type"] = "application/json; charset=utf-8";
          headers["X-OWA-CANARY"] = canary;
          headers["Action"] = cand.action;
          headers["X-Requested-With"] = "XMLHttpRequest";
          body = JSON.stringify({
            __type: `${cand.action}JsonRequest:#Exchange`,
            Header: {
              __type: "JsonRequestHeaders:#Exchange",
              RequestServerVersion: "Exchange2013",
            },
            Body: {
              __type: `${cand.action}Request:#Exchange`,
              // AllProperties, NOT IdOnly. The probe's whole job is to
              // decide whether attendees/body/join URL come back, and
              // asking for IdOnly guarantees they do not — which this
              // probe would then have to record as "answered-thin".
              // That would be a false negative manufactured by the
              // question, which is precisely the mistake v1.4's
              // anchor-only join-link search made. Ask for everything;
              // let the response be the evidence.
              ItemShape: {
                __type: "ItemResponseShape:#Exchange",
                BaseShape: "AllProperties",
              },
              ParentFolderIds: [{ __type: "DistinguishedFolderId:#Exchange", Id: "calendar" }],
              Traversal: "Shallow",
              CalendarView: {
                __type: "CalendarView:#Exchange",
                StartDate: startIso,
                EndDate: endIso,
                MaxEntriesReturned: 50,
              },
            },
          });
        }

        const url = cand.method === "GET"
          ? `${location.origin}${cand.path}?startDateTime=${encodeURIComponent(startIso)}&endDateTime=${encodeURIComponent(endIso)}&$top=50`
          : `${location.origin}${cand.path}`;

        const res = await fetch(url, {
          method: cand.method,
          credentials: "include",
          headers,
          body,
        });

        entry.status = res.status;
        entry.ok = res.ok;
        entry.contentType = (res.headers.get("content-type") || "").split(";")[0];

        const text = await res.text();
        entry.bytes = text.length;

        let parsed = null;
        try { parsed = JSON.parse(text); entry.json = true; } catch (_) { entry.json = false; }

        if (parsed) {
          const items = findItemArray(parsed);
          const keys = keyNamesIn(items);
          entry.itemCount = items.length;
          for (const [want, aliases] of Object.entries(wantedKeys)) {
            entry.fieldsPresent[want] = aliases.some((a) => keys.has(a));
          }
        }

        // The four states. Order matters: auth is checked before
        // shape, so a 401 is never described as "thin".
        if (res.status === 401 || res.status === 403) {
          entry.verdict = "auth-rejected";
        } else if (!res.ok) {
          entry.verdict = "unreachable";
        } else if (!entry.json) {
          // A 200 of HTML is a sign-in page, not calendar data.
          entry.verdict = "unreachable";
          entry.error = "2xx but the body was not JSON (likely a sign-in redirect)";
        } else if (entry.fieldsPresent.attendees || entry.fieldsPresent.body
                   || entry.fieldsPresent.joinUrl) {
          entry.verdict = "usable";
        } else {
          entry.verdict = "answered-thin";
        }
      } catch (e) {
        entry.verdict = "unreachable";
        entry.error = (e && e.message) ? e.message : String(e);
      }

      out.results.push(entry);
    }
  } catch (e) {
    out.error = (e && e.message) ? e.message : String(e);
  }

  return out;
}

// Wrapper — opens the calendar tab, runs the probe in it, closes it.
// Mirrors diagnoseCalendarCapture's lifecycle exactly, including the
// finally-block tab cleanup, so a thrown probe can never leave a
// stray tab behind in the user's browser.
async function diagnoseCalendarApi() {
  const tab = await chrome.tabs.create({ url: CALENDAR_WEEK_URL, active: false });
  const tabId = tab.id;
  let probe = null;
  try {
    await waitForTabComplete(tabId);
    // The grid's own first data call has to land before the session is
    // reliably warm; the existing capture path waits for the same
    // reason.
    await sleep(3000);
    const result = await chrome.scripting.executeScript({
      target: { tabId },
      args: [CALENDAR_API_CANDIDATES, CALENDAR_API_WANTED_KEYS, 7],
      func: _calendarApiProbeFunc,
    });
    probe = (result && result[0] && result[0].result) || null;
  } catch (e) {
    probe = { error: e.message || String(e), results: [] };
  } finally {
    try { await chrome.tabs.remove(tabId); } catch (_) { /* ignore */ }
  }

  const results = (probe && probe.results) || [];
  const usable = results.filter((r) => r.verdict === "usable");
  const thin = results.filter((r) => r.verdict === "answered-thin");
  const rejected = results.filter((r) => r.verdict === "auth-rejected");
  const notAttempted = results.filter((r) => r.verdict === "not-attempted");

  // Says what was actually established, and names what was NOT tried
  // rather than letting a skipped candidate read as a failed one.
  let verdict;
  if (usable.length) {
    const fields = usable[0].fieldsPresent || {};
    verdict = `${usable.length} endpoint(s) answered with calendar data carrying `
      + Object.entries(fields).filter(([, v]) => v).map(([k]) => k).join(", ")
      + ` — the detail fields ARE reachable from the signed-in session without opening each event;`
      + ` implement against "${usable[0].name}"`;
  } else if (thin.length) {
    verdict = `${thin.length} endpoint(s) answered but carried none of attendees/body/joinUrl`
      + ` — reachable, wrong shape. The request body needs a fuller property set, not a different endpoint.`;
  } else if (rejected.length) {
    verdict = `${rejected.length} endpoint(s) were reachable but rejected this session's credentials`
      + ` (401/403) — an entitlement problem, NOT evidence the API is absent.`;
  } else if (notAttempted.length === results.length && results.length) {
    verdict = `nothing was attempted: every candidate needs the X-OWA-CANARY cookie and none was present.`
      + ` This says nothing about whether the endpoints work — only that this run could not ask.`;
  } else {
    verdict = `no candidate answered. ${results.length} tried;`
      + ` see each result's status and error for which failed how.`;
  }

  return {
    ok: true,
    origin: (probe && probe.origin) || "",
    canaryPresent: !!(probe && probe.canaryPresent),
    mailboxHintPresent: !!(probe && probe.mailboxHintPresent),
    candidatesTried: results.length,
    results,
    verdict,
    error: (probe && probe.error) || null,
  };
}

async function diagnoseCalendarCapture() {
  const tab = await chrome.tabs.create({ url: CALENDAR_WEEK_URL, active: false });
  const tabId = tab.id;
  const start = Date.now();
  const snapshots = [];
  let realScan = null;

  try {
    await waitForTabComplete(tabId);

    for (const delay of DIAGNOSTIC_SNAPSHOT_DELAYS_MS) {
      const waitMore = delay - (Date.now() - start);
      if (waitMore > 0) await sleep(waitMore);

      let snap = null;
      try {
        const result = await chrome.scripting.executeScript({
          target: { tabId },
          args: [
            TIME_RANGE_RE.source,
            "\\d{1,2}(:\\d{2})?\\s*[AaPp]?\\.?[Mm]?\\b|\\d{1,2}:\\d{2}\\b",
            JOIN_URL_PROBE_CONFIG,
          ],
          func: _calendarDiagnosticProbeFunc,
        });
        snap = (result && result[0] && result[0].result) || null;
      } catch (e) {
        snap = { error: e.message || String(e) };
      }
      snapshots.push({ atMs: Date.now() - start, requestedAtMs: delay, ...(snap || {}) });
    }

    // Run the REAL production scan (_calendarDomScanFunc — the exact
    // function settleAndCollectCalendar calls during a live capture)
    // once more, at the end, and report what IT saw next to the
    // probe's own independent flat-query counts above. Before v1.3.2
    // these two could silently diverge (a depth-capped recursive walk
    // finding far fewer candidates than a flat `[aria-label]` query
    // over the same page) with nothing in this report calling that
    // out — which is exactly what let the depth-cap bug masquerade as
    // a parsing problem. Keeping both numbers in the same report
    // means any future regression that makes the real scan see less
    // than the flat probe shows up immediately as a mismatch here,
    // instead of requiring someone to re-derive it from field reports.
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId },
        func: _calendarDomScanFunc,
      });
      const scanResult = (result && result[0] && result[0].result) || {};
      realScan = {
        candidateCount: (scanResult.candidates || []).length,
        diag: scanResult.diag || null,
      };
    } catch (e) {
      realScan = { error: e.message || String(e) };
    }
  } finally {
    try { await chrome.tabs.remove(tabId); } catch (_) { /* ignore */ }
  }

  const final = snapshots[snapshots.length - 1] || {};
  return {
    ok: true,
    url: CALENDAR_WEEK_URL,
    finalUrl: final.finalUrl || null,
    elapsedMs: Date.now() - start,
    // What the real production DOM scan found, for direct comparison
    // against `ariaLabelCount`/`patternsTried` below (the diagnostic
    // probe's OWN independent flat query) — see the comment above.
    realScan,
    // Element counts at each snapshot — lets a reader tell "nothing
    // structural is wrong, it just wasn't done rendering yet" (counts
    // climb over the 4 samples) apart from "structurally zero"
    // (counts flat at 0 the whole time).
    timeline: snapshots.map((s) => ({
      atMs: s.atMs,
      containerCounts: s.containerCounts,
      eventNodeCounts: s.eventNodeCounts,
      ariaLabelCount: s.ariaLabelCount,
      timeRangeMatchCount: s.patternsTried && s.patternsTried[0] && s.patternsTried[0].matchCount,
      joinLinkMatchCount: s.joinLinks && s.joinLinks.matchCount,
      // Both halves in the timeline, so "the anchors were never there
      // but the labels always were" is readable at a glance rather than
      // only from the final snapshot's verdict.
      joinLinkInLabelTextCount: s.joinLinks && s.joinLinks.labelJoinInMeetingShapedLabel,
      error: s.error,
    })),
    // Read-only join-link probe — the answer to "can join_url be filled
    // from what we already read, or would it need a click into every
    // event?" Looks in BOTH places (anchor elements AND aria-label
    // text) and says which one produced the answer; see
    // JOIN_URL_PROBE_CONFIG. Examples are host+path SHAPE only; a full
    // join URL is a single-use credential and never leaves the page.
    joinLinks: final.joinLinks || null,
    ariaLabelCount: final.ariaLabelCount,
    longestLabels: final.longestLabels || [],
    patternsTried: final.patternsTried || [],
    iframeCount: final.iframeCount,
    iframeAccessibleCount: final.iframeAccessibleCount,
    shadowRootCount: final.shadowRootCount,
    gridInsideIframe: final.gridInsideIframe,
    gridInnerTextSample: final.gridInnerTextSample || "",
    containerCounts: final.containerCounts,
    eventNodeCounts: final.eventNodeCounts,
    error: final.error || null,
  };
}
