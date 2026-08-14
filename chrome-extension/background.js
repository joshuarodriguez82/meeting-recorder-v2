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
// ──────────────────────────────────────────────────────────────────

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm?.name === CALENDAR_ALARM_NAME) {
    const cfg = await chrome.storage.local.get({
      backendUrl: "", token: "", lastCalendarCaptureAt: 0,
    });
    if (!cfg.backendUrl || !cfg.token) {
      console.warn("[ext] calendar-refresh fired but extension not configured; skipping");
      return;
    }
    if (Date.now() - cfg.lastCalendarCaptureAt < CALENDAR_DEDUP_WINDOW_MS) {
      const mins = Math.round((Date.now() - cfg.lastCalendarCaptureAt) / 60_000);
      console.log(`[ext] calendar-refresh dedupe: last capture ${mins} min ago, skipping`);
      return;
    }
    await captureCalendarOnly(cfg.backendUrl, cfg.token);
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
      `(layer=${calendarCapture.layer || "text-fallback"}, ` +
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

// Date resolution order (explicit, per spec): (1) an explicit date in
// the label itself, (2) the columnDateIso the DOM layer resolved from
// ancestor/column context, (3) nothing — the caller drops the event
// rather than guess.
function resolveDateParts(dateAtom, columnDateIso, fallbackYear) {
  const fromLabel = _dateAtomToParts(dateAtom, fallbackYear);
  if (fromLabel) return fromLabel;
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
  const startDateParts = resolveDateParts(parts.date1, columnDateIso, year);
  // An end date only falls back to the *start* date (never straight
  // to columnDateIso) when the label named its own start date — that
  // label is telling us the event isn't just "today", so borrowing
  // columnDateIso for the end would silently truncate a multi-day
  // span back down to one day.
  const endDateParts = parts.date2
    ? _dateAtomToParts(parts.date2, year)
    : (parts.date1 ? startDateParts : resolveDateParts(null, columnDateIso, year));

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

// Turn a list of DOM-scan candidates ({label, columnDateIso, layer})
// into deduped structured events + stats. Pure. Dedup key is
// (normalized subject, start) — Outlook Web routinely renders the
// same event's aria-label on several nested elements (the tile, its
// title span, its time span, ...), so the SAME event commonly yields
// several candidates that must collapse to one.
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
  };
  const seen = new Map();

  for (const c of candidates || []) {
    const r = parseMeetingLabel(c && c.label, (c && c.columnDateIso) || null, fallbackYear);
    if (r.kind === "all-day") { stats.allDay++; continue; }
    if (r.kind === "not-meeting-shaped" || r.kind === "empty") { stats.notMeetingShaped++; continue; }
    if (r.kind === "date-unresolved") { stats.dateUnresolved++; continue; }

    const key = `${r.subject.toLowerCase()}|${r.startIso}`;
    if (seen.has(key)) { stats.deduped++; continue; }

    const layer = (c && c.layer) || "unknown";
    seen.set(key, {
      subject: r.subject,
      start: r.startIso,
      end: r.endIso,
      location: (c && c.location) || "",
      organizer: (c && c.organizer) || "",
      join_url: (c && c.joinUrl) || "",
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

// ── DOM scan (browser-only, NOT unit-testable here) ─────────────────

// Runs inside the calendar tab via chrome.scripting.executeScript.
// Deliberately fully self-contained (no reference to any function
// above) — injected scripts execute in an isolated page context that
// cannot close over this file's top-level scope, same constraint the
// existing readMainText/clickTeamsNav functions already work under.
//
// Returns a plain array of { label, columnDateIso, layer } records —
// no parsing happens here, only collection, so every actual parsing
// decision lives in the tested pure functions above.
function _calendarDomScanFunc() {
  const out = [];
  try {
    const TIME_HINT_RE = /\d{1,2}(:\d{2})?\s*[AaPp]?\.?[Mm]?\b|\d{1,2}:\d{2}\b/;
    const ALLDAY_HINT_RE = /\ball[\s-]?day\b/i;
    const MONTH_HINT_RE = /(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}(,?\s*\d{4})?/i;
    const KNOWN_DATE_ATTRS = ["data-start-date", "data-date", "data-start-day", "data-day", "data-dayid"];

    function nearestDateIso(el) {
      let node = el;
      for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
        for (const attr of KNOWN_DATE_ATTRS) {
          const v = node.getAttribute && node.getAttribute(attr);
          if (v) {
            const d = new Date(v);
            if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
          }
        }
        const aria = node.getAttribute && node.getAttribute("aria-label");
        if (aria && MONTH_HINT_RE.test(aria)) {
          const d = new Date(aria.replace(/^[A-Za-z]+,\s*/, ""));
          if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
        }
      }
      // Tier 3: nearest columnheader by horizontal position.
      try {
        const headers = document.querySelectorAll('[role="columnheader"]');
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        let best = null, bestDist = Infinity;
        for (const h of headers) {
          const label = h.getAttribute("aria-label") || h.innerText || "";
          if (!MONTH_HINT_RE.test(label)) continue;
          const hr = h.getBoundingClientRect();
          const dist = Math.abs((hr.left + hr.width / 2) - cx);
          if (dist < bestDist) { bestDist = dist; best = label; }
        }
        if (best) {
          const d = new Date(best.replace(/^[A-Za-z]+,\s*/, ""));
          if (!isNaN(d.getTime())) return d.toISOString().slice(0, 10);
        }
      } catch (_) { /* best-effort only */ }
      return null;
    }

    // String-level pre-filter dedup: several nested elements often
    // carry the literal same label text for one event. The pure
    // subject+start dedup downstream is the real safety net; this is
    // just cheap early pruning.
    const seenLabels = new Set();

    for (const el of document.querySelectorAll("[aria-label]")) {
      const label = el.getAttribute("aria-label") || "";
      if (!label || seenLabels.has(label)) continue;
      if (!TIME_HINT_RE.test(label) && !ALLDAY_HINT_RE.test(label)) continue;
      seenLabels.add(label);
      out.push({ label, columnDateIso: nearestDateIso(el), layer: "aria-label" });
    }

    // Layer 2: generic event-shaped nodes without their own aria-label
    // (some builds render calendar tiles this way). Broader net, only
    // adds candidates layer 1 didn't already cover.
    for (const el of document.querySelectorAll('[role="button"], [role="option"], [role="gridcell"]')) {
      if (el.hasAttribute("aria-label")) continue;
      const text = (el.innerText || "").trim();
      if (!text || seenLabels.has(text)) continue;
      if (!TIME_HINT_RE.test(text) && !ALLDAY_HINT_RE.test(text)) continue;
      seenLabels.add(text);
      out.push({ label: text, columnDateIso: nearestDateIso(el), layer: "generic-node" });
    }
  } catch (_) {
    // Best-effort only — an empty return here just means the caller
    // falls back to the text-scrape path.
  }
  return out;
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

// Wait for the calendar tab's content to settle (same text-size
// heuristic as OWA/Teams above, just sized for a week grid), then run
// the DOM scan. Returns { candidates, text } — `text` is the raw
// inner-text fallback used only when structured extraction finds
// nothing at all.
async function settleAndCollectCalendar(tabId, label) {
  let lastLen = -1, stableCount = 0, text = "";
  const start = Date.now();
  while (Date.now() - start < CALENDAR_MAX_WAIT_MS) {
    try {
      text = (await readMainText(tabId)) || "";
    } catch (e) {
      console.warn(`[ext] calendar ${label}: text read failed:`, e);
    }
    if (text.length >= CALENDAR_TARGET_CHARS) break;
    if (text.length === lastLen && text.length > 0) {
      stableCount += 1;
      if (stableCount >= STABILITY_POLLS) break;
    } else {
      stableCount = 0;
      lastLen = text.length;
    }
    await sleep(POLL_MS);
  }

  let candidates = [];
  try {
    const result = await chrome.scripting.executeScript({
      target: { tabId },
      func: _calendarDomScanFunc,
    });
    candidates = (result && result[0] && result[0].result) || [];
  } catch (e) {
    console.warn(`[ext] calendar ${label}: DOM scan failed:`, e);
  }

  console.log(
    `[ext] calendar ${label}: ${candidates.length} candidate label(s), ` +
    `${text.length} chars fallback text (below floor=` +
    `${text.length < CALENDAR_MIN_USEFUL_CHARS})`);
  return { candidates, text };
}

// Full calendar capture: this week + next week (see the header
// comment on why one week view alone can miss the tail of the panel's
// 168h window), structured extraction with a text-scrape fallback.
// Returns { events, layer, stats, fallbackText, elapsedMs, diag }.
async function captureCalendarTab() {
  const tab = await chrome.tabs.create({ url: CALENDAR_WEEK_URL, active: false });
  const tabId = tab.id;
  const start = Date.now();
  const allCandidates = [];
  const diag = { weeksScanned: 0, nextWeekNavOk: null };
  let fallbackText = "";

  try {
    await waitForTabComplete(tabId);

    const week1 = await settleAndCollectCalendar(tabId, "week1 (current)");
    allCandidates.push(...week1.candidates);
    fallbackText = week1.text;
    diag.weeksScanned += 1;

    const navOk = await goToNextCalendarWeek(tabId);
    diag.nextWeekNavOk = navOk;
    if (navOk) {
      const week2 = await settleAndCollectCalendar(tabId, "week2 (next)");
      allCandidates.push(...week2.candidates);
      if (week2.text) fallbackText += "\n\n" + week2.text;
      diag.weeksScanned += 1;
    }
  } finally {
    try { await chrome.tabs.remove(tabId); } catch (_) { /* ignore */ }
  }

  const extraction = extractEventsFromCandidates(allCandidates, {
    fallbackYear: new Date().getFullYear(),
  });

  return {
    events: extraction.events,
    layer: dominantLayer(extraction.stats),
    stats: extraction.stats,
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
    return { ok: false, error: "Backend URL or token not configured." };
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
      error: "Calendar tab returned nothing (0 structured events, 0 fallback " +
        "text). Is Outlook Web signed in? Try Capture & Send from the popup.",
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
