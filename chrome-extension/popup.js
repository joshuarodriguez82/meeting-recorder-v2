// Meeting Recorder Chrome extension — popup logic.

const $ = (id) => document.getElementById(id);

async function getConfig() {
  return await chrome.storage.local.get({ backendUrl: "", token: "" });
}

function setStatus(kind, text) {
  const el = $("status");
  el.className = kind;
  el.textContent = text;
}

async function refreshConfigBanner() {
  const cfg = await getConfig();
  const need = !cfg.backendUrl || !cfg.token;
  $("notConfigured").style.display = need ? "block" : "none";
  $("captureBtn").disabled = need;
}

function agoText(ts) {
  const mins = Math.round((Date.now() - ts) / 60_000);
  return mins < 60 ? `${mins} min ago` : `${Math.floor(mins / 60)}h ${mins % 60}m ago`;
}

async function renderLastCapture() {
  const cfg = await chrome.storage.local.get({ lastCaptureAt: 0, lastResult: null });
  const el = $("lastCapture");
  if (!cfg.lastCaptureAt) {
    el.textContent = "";
    return;
  }
  const ago = agoText(cfg.lastCaptureAt);
  if (cfg.lastResult?.ok) {
    const c = cfg.lastResult.counts || {};
    const parts = [];
    if (c.owa) parts.push(`O:${c.owa}`);
    if (c.teams) parts.push(`T:${c.teams}`);
    if (c.inbox) parts.push(`I:${c.inbox}`);
    if (c.chat) parts.push(`C:${c.chat}`);
    el.innerHTML = `Last: ${ago} · ✓ ${parts.join(" ") || "(empty)"}`;
  } else if (cfg.lastResult) {
    el.innerHTML = `Last: ${ago} · ✗ failed`;
  } else {
    el.textContent = `Last: ${ago}`;
  }
}

// Calendar count is shown on its own line, prominently — it's fed by
// a separate, more frequent alarm than the 4-source capture above, so
// it needs its own status. Also shows which extraction layer produced
// the count so a silent regression to the lossy text-scrape fallback
// is visible here instead of invisible (field report 2026-08-13: the
// old text/LLM path silently dropped 4 of 5 real meetings).
async function renderCalendarStatus() {
  const cfg = await chrome.storage.local.get({
    lastCalendarCaptureAt: 0, lastCalendarResult: null,
  });
  const el = $("lastCalendar");
  if (!cfg.lastCalendarCaptureAt) {
    el.textContent = "Calendar: no capture yet.";
    return;
  }
  const ago = agoText(cfg.lastCalendarCaptureAt);
  const r = cfg.lastCalendarResult;
  if (r?.ok) {
    const n = r.eventCount ?? 0;
    if (n === 0) {
      // A zero result on its own is indistinguishable between "no
      // candidate elements found" / "page still rendering" / "found
      // N candidates, none had a parseable time" — each points at a
      // completely different fix, so say which one (field report
      // 2026-08-14). See Settings → Diagnose calendar capture for more.
      el.innerHTML = `Calendar: ${ago} · 0 events (${r.zeroReason || "reason unknown"})`;
    } else {
      const layerLabel = r.layer === "aria-label" ? "structured"
        : r.layer === "generic-node" ? "structured (fallback nodes)"
        : "⚠ text fallback";
      // Per-field coverage, not just a count. `organizer` and
      // `join_url` were both declared on every captured event and
      // never assigned — always "" — which looked identical to "this
      // meeting genuinely has none". Both now come out of the label
      // the capture already parses: the organizer from its "By <name>"
      // tail, the join link from the Location segment when the
      // organiser's add-in wrote one there.
      //
      // A zero here is a real answer, not a broken one, and the wording
      // has to say so without claiming more than it knows: a Teams-only
      // week genuinely has no URL in any label (a Teams event's
      // Location is the words "Microsoft Teams Meeting"), which is not
      // the same as extraction having failed. Settings → Diagnose
      // calendar capture tells the two apart.
      const s = r.stats || {};
      const extras = [];
      if (typeof s.withOrganizer === "number") {
        extras.push(`organizer on ${s.withOrganizer}/${n}`);
      }
      const joins = s.withJoinUrl || 0;
      extras.push(joins > 0
        ? `join link on ${joins}/${n}`
        : "no join link in any label");
      el.innerHTML = `Calendar: ${ago} · ${n} event${n === 1 ? "" : "s"} · ` +
        `${layerLabel} · ${extras.join(" · ")}`;
    }
  } else if (r) {
    el.innerHTML = `Calendar: ${ago} · ✗ ${r.error || "failed"}`;
  } else {
    el.textContent = `Calendar: ${ago}`;
  }
}

function openOptions(e) {
  if (e) e.preventDefault();
  chrome.runtime.openOptionsPage();
}

$("settingsLink").addEventListener("click", openOptions);
$("optionsLink").addEventListener("click", openOptions);

// ── Capture ─────────────────────────────────────────────────────────
//
// The background worker acknowledges the click immediately and reports
// everything after that through chrome.storage.local. This popup used
// to `await chrome.runtime.sendMessage(...)` for the whole capture,
// which produced:
//
//   "A listener indicated an asynchronous response by returning true,
//    but the message channel closed before a response was received"
//
// A capture reads five surfaces and can take three minutes. Neither
// this popup nor an MV3 service worker reliably lives that long, and
// when either goes the channel closes and the reply lands nowhere.
//
// Reading from storage instead means closing the popup no longer costs
// you the capture, and reopening it mid-run shows where the run got to
// rather than starting from nothing.

/** Paint whatever the current run says, whether we started it or not. */
async function renderCaptureRun() {
  let run;
  try {
    run = await chrome.runtime.sendMessage({ type: "get-capture-run" });
  } catch {
    // The worker is asleep and has nothing in flight. Not an error.
    run = null;
  }
  if (!run) return false;

  $("captureBtn").disabled = !!run.running;

  if (run.running) {
    setStatus("busy",
      `${run.stage || "Working…"} — reading OWA, Teams, Inbox, Chat and `
      + `Calendar in background tabs. Up to about three minutes. You can `
      + `close this popup; the capture keeps going.`);
    return true;
  }

  const result = run.result;
  if (!result) return false;

  if (result.ok) {
    const c = result.counts || {};
    const parts = [];
    if (c.owa) parts.push(`OWA: ${c.owa}`);
    if (c.teams) parts.push(`Teams: ${c.teams}`);
    if (c.inbox) parts.push(`Inbox: ${c.inbox}`);
    if (c.chat) parts.push(`Chat: ${c.chat}`);
    const calN = c.calendar ?? 0;
    const calSuffix = (calN === 0 && result.calendarZeroReason)
      ? ` (${result.calendarZeroReason})` : "";
    parts.push(`Calendar: ${calN} event${calN === 1 ? "" : "s"}${calSuffix}`);
    setStatus("ok", `✓ Sent (${parts.join(", ")}). Open the Today tab for the parsed brief.`);
  } else {
    setStatus("error", `✗ ${result.error || "Unknown error"}`);
  }
  return false;
}

/** Poll while a run is in flight. Storage events do not reach a popup
 *  reliably across a worker restart, so this asks rather than waits. */
let capturePollTimer = null;
function watchCaptureRun() {
  if (capturePollTimer) return;
  capturePollTimer = setInterval(async () => {
    const stillRunning = await renderCaptureRun();
    if (!stillRunning) {
      clearInterval(capturePollTimer);
      capturePollTimer = null;
      await renderLastCapture();
      await renderCalendarStatus();
      refreshConfigBanner();
    }
  }, 1000);
}

$("captureBtn").addEventListener("click", async () => {
  $("captureBtn").disabled = true;
  setStatus("busy", "Starting…");
  try {
    const cfg = await getConfig();
    if (!cfg.backendUrl || !cfg.token) {
      throw new Error("Backend URL or token not configured. Open Settings.");
    }
    // Returns as soon as the worker has accepted the job — this is an
    // acknowledgement, not the result.
    await chrome.runtime.sendMessage({
      type: "capture-and-send",
      backendUrl: cfg.backendUrl,
      token: cfg.token,
    });
    await renderCaptureRun();
    watchCaptureRun();
  } catch (e) {
    setStatus("error", `✗ ${e.message || String(e)}`);
    $("captureBtn").disabled = false;
    refreshConfigBanner();
  }
});

// Opening the popup during a run picks it up where it is.
renderCaptureRun().then((running) => { if (running) watchCaptureRun(); });

refreshConfigBanner();
renderLastCapture();
renderCalendarStatus();
