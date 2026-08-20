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
// `join_url` is declared on every captured event and has never been
// populated on this path: both NATIVE calendar backends
// (`services/_calendar_outlook.py:559`,
// `services/_calendar_eventkit.py:579`) get it by running
// `_extract_join_url(location, body)` over the invite BODY, and the
// extension scrape has no body — Outlook Web's aria-label carries
// "Microsoft Teams Meeting" as a LABEL, never the URL.
//
// Whether a join link is nonetheless reachable from the elements the
// capture already scans is an open question that cannot be answered
// from this environment (no way to sign in to a real tenant). Rather
// than guess, the diagnostic below counts join-shaped anchors in the
// same roots the real scan walks and reports whether each one sits
// inside — or next to — an element carrying a meeting-shaped
// aria-label. That is the whole decision: if those numbers come back
// non-zero, `join_url` can be filled from the DOM we already have; if
// they come back zero, the only remaining route is opening every event
// to scrape its detail pane, which is slow, fragile and exactly the
// DOM dependency that broke calendar capture before — an owner
// decision, not a default.
//
// Passed to `_calendarDiagnosticProbeFunc` as ARGS (regex sources, not
// RegExp objects) for the same reason TIME_RANGE_RE.source already is:
// an injected script cannot close over this file's module scope, and
// duplicating the vocabulary inside the probe would let the two drift.
const JOIN_URL_PROBE_CONFIG = {
  providers: [
    {
      name: "teams",
      host: "^(?:teams\\.microsoft\\.com|teams\\.live\\.com|teams\\.cloud\\.microsoft)$",
      path: "^/l/meetup-join/|^/l/meeting/|^/meet/",
    },
    { name: "zoom", host: "(?:^|\\.)zoom\\.us$", path: "^/(?:j|w|s|my|wc)/" },
    { name: "webex", host: "(?:^|\\.)webex\\.com$", path: "^/(?:meet|join|wbxmjs)/|/j\\.php|/m\\.php" },
    { name: "meet", host: "^meet\\.google\\.com$", path: "^/[A-Za-z0-9]" },
  ],
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

    let name = segments[0];
    const looksLikeBareSurname = !/\s/.test(name);
    if (looksLikeBareSurname && segments[1] && !_isOrganizerStatusSegment(segments[1])) {
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
    const joinUrl = (c && c.joinUrl) || "";
    if (organizer) stats.withOrganizer++;
    if (joinUrl) stats.withJoinUrl++;
    seen.set(key, {
      subject: r.subject,
      start: r.startIso,
      end: r.endIso,
      location: (c && c.location) || "",
      // Was always "" — nothing ever assigned `c.organizer`. Now
      // recovered from the label's own tail (see
      // extractOrganizerFromLabel); a DOM-supplied value, if one is
      // ever added, still wins. Extraction failure returns "", i.e.
      // exactly the value this field always had.
      organizer,
      // Still always "": Outlook Web's calendar grid carries "Microsoft
      // Teams Meeting" as a LABEL, never the join URL, and no join link
      // has been shown to be reachable from the elements this scan
      // already visits. `diagnoseCalendarCapture`'s joinLinks probe is
      // what answers that question against a real tenant — see
      // JOIN_URL_PROBE_CONFIG. Deliberately NOT backfilled by opening
      // each event to scrape its detail pane: that is slow, fragile,
      // and exactly the DOM dependency that broke calendar capture
      // before — an owner decision, not a default.
      join_url: joinUrl,
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
async function captureCalendarTab() {
  const tab = await chrome.tabs.create({ url: CALENDAR_WEEK_URL, active: false });
  const tabId = tab.id;
  const start = Date.now();
  const allCandidates = [];
  const diag = { weeksScanned: 0, nextWeekNavOk: null, anyWeekUnstable: false };
  let fallbackText = "";

  try {
    await waitForTabComplete(tabId);

    const week1 = await settleAndCollectCalendar(tabId, "week1 (current)");
    allCandidates.push(...week1.candidates);
    fallbackText = week1.text;
    diag.weeksScanned += 1;
    if (!week1.stabilized) diag.anyWeekUnstable = true;

    const navOk = await goToNextCalendarWeek(tabId);
    diag.nextWeekNavOk = navOk;
    if (navOk) {
      const week2 = await settleAndCollectCalendar(tabId, "week2 (next)");
      allCandidates.push(...week2.candidates);
      if (week2.text) fallbackText += "\n\n" + week2.text;
      diag.weeksScanned += 1;
      if (!week2.stabilized) diag.anyWeekUnstable = true;
    }
  } finally {
    try { await chrome.tabs.remove(tabId); } catch (_) { /* ignore */ }
  }

  const extraction = extractEventsFromCandidates(allCandidates, {
    fallbackYear: new Date().getFullYear(),
  });
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
    // Answers exactly one question: is a meeting join link reachable
    // from the DOM the real capture already walks? Three numbers say
    // it — how many join-shaped links exist at all, how many sit
    // INSIDE an element whose aria-label is meeting-shaped (safe to
    // associate by containment), and how many merely sit NEXT TO one
    // (weaker, but still positional-free). Links that are neither are
    // counted as unassociated: those could only be matched by grid
    // position, which is precisely the association a wrong answer
    // comes from, so they are reported, never used.
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
      };
      for (const p of providers) out.byProvider[p.name] = 0;

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

      const associated = out.insideMeetingLabelledElement +
        out.adjacentToMeetingLabelledElement;
      out.verdict = out.matchCount === 0
        ? "no join-shaped links anywhere in the scanned roots — the grid " +
          "does not expose them; join_url cannot be filled from this DOM"
        : (associated === 0
          ? `found ${out.matchCount} join-shaped link(s), but none inside or ` +
            `adjacent to a meeting-shaped aria-label — associating them would ` +
            `have to be positional, which is not safe enough to use`
          : `found ${out.matchCount} join-shaped link(s), ${associated} of them ` +
            `associable by containment/adjacency — join_url IS fillable from ` +
            `the DOM the capture already scans`);
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
      error: s.error,
    })),
    // Read-only join-link probe — the answer to "can join_url be filled
    // from the DOM we already scan, or would it need a click into every
    // event?" See JOIN_URL_PROBE_CONFIG. Examples are host+path SHAPE
    // only; a full join URL is a single-use credential and never leaves
    // the page.
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
