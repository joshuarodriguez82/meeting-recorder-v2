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

  const cfg = await chrome.storage.local.get({
    autoCapture: false,
    captureTimes: DEFAULT_CAPTURE_TIMES,
  });
  if (!cfg.autoCapture) {
    console.log("[ext] auto-capture off; no alarms scheduled");
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
      autoCapture: false,
      captureTimes: DEFAULT_CAPTURE_TIMES,
    }).then(sendResponse);
    return true;
  }
});

// ──────────────────────────────────────────────────────────────────
// The actual capture.
// ──────────────────────────────────────────────────────────────────

async function captureAndSend(backendUrl, token, opts = {}) {
  if (!backendUrl || !token) {
    return { ok: false, error: "Backend URL or token not configured. Open Settings." };
  }

  console.log(`[ext] starting capture (source=${opts.source || "manual"})`);

  const payload = {};
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

  // If literally every source returned 0 chars, treat as a hard
  // failure. Otherwise we still POST whatever we got — even just
  // OWA is a useful brief.
  const totalChars = Object.values(counts).reduce((a, b) => a + b, 0);
  if (totalChars === 0) {
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
      errors: errors.length ? errors : undefined,
      ts: Date.now(),
    };
    await chrome.storage.local.set({ lastCaptureAt: Date.now(), lastResult: result });
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
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      try {
        const main = document.querySelector('[role="main"]');
        if (main && (main.innerText || "").trim().length > 0) {
          return main.innerText;
        }
        return document.body ? document.body.innerText : "";
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
