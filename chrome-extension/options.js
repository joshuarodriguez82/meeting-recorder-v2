// Meeting Recorder Chrome extension — options page.

const $ = (id) => document.getElementById(id);

const DEFAULT_CAPTURE_TIMES = ["08:00", "12:00", "17:00"];

async function load() {
  const cfg = await chrome.storage.local.get({
    backendUrl: "",
    token: "",
    autoCapture: false,
    captureTimes: DEFAULT_CAPTURE_TIMES,
    lastCaptureAt: 0,
    lastResult: null,
    lastCalendarCaptureAt: 0,
    lastCalendarResult: null,
  });
  $("backendUrl").value = cfg.backendUrl;
  $("token").value = cfg.token;
  $("autoCapture").checked = !!cfg.autoCapture;
  const times = cfg.captureTimes || DEFAULT_CAPTURE_TIMES;
  $("time1").value = times[0] || "08:00";
  $("time2").value = times[1] || "12:00";
  $("time3").value = times[2] || "17:00";
  renderLastCapture(cfg.lastCaptureAt, cfg.lastResult);
  renderCalendarStatus(cfg.lastCalendarCaptureAt, cfg.lastCalendarResult);
}

// Calendar refreshes on its own alarm (every 30 min, independent of
// the autoCapture toggle above) — surfaced separately so staleness or
// a silent fall-back to the lossy text path is visible without
// opening the console. See background.js captureCalendarOnly.
function renderCalendarStatus(ts, result) {
  const el = $("lastCalendar");
  if (!el) return;
  if (!ts) {
    el.textContent = "Calendar: no capture yet. Runs automatically every 30 min, or click Capture now.";
    return;
  }
  const ago = Math.round((Date.now() - ts) / 60_000);
  const agoStr = ago < 60 ? `${ago} min ago` : `${Math.floor(ago / 60)}h ${ago % 60}m ago`;
  if (result?.ok) {
    const n = result.eventCount ?? 0;
    if (n === 0) {
      // See popup.js's renderCalendarStatus for why this matters:
      // "no candidates" / "still rendering" / "candidates but no
      // parseable time" point at different fixes and were previously
      // indistinguishable (field report 2026-08-14).
      el.textContent = `Calendar: ${agoStr} · 0 events (${result.zeroReason || "reason unknown"}) — try Diagnose calendar capture below`;
    } else {
      const layerLabel = result.layer === "aria-label" ? "structured extraction"
        : result.layer === "generic-node" ? "structured extraction (fallback nodes)"
        : "⚠ text fallback — structured extraction found nothing";
      el.textContent = `Calendar: ${agoStr} · ${n} event${n === 1 ? "" : "s"} · ${layerLabel}`;
    }
  } else if (result) {
    el.textContent = `Calendar: ${agoStr} · ✗ ${result.error || "failed"}`;
  } else {
    el.textContent = `Calendar: ${agoStr}`;
  }
}

function renderLastCapture(ts, result) {
  const el = $("lastCapture");
  if (!ts) {
    el.textContent = "Last capture: never.";
    return;
  }
  const ago = Date.now() - ts;
  const mins = Math.round(ago / 60_000);
  const hours = Math.floor(mins / 60);
  const ago_str = hours > 0
    ? `${hours} hour${hours === 1 ? "" : "s"} ${mins % 60} min ago`
    : `${mins} min ago`;
  if (result?.ok) {
    const counts = result.counts || {};
    el.innerHTML = `Last capture: ${ago_str} · ✓ success. <span class="source-counts">` +
      Object.entries(counts)
        .map(([k, v]) => `<span>${k}: ${v}</span>`)
        .join("") +
      `</span>`;
  } else if (result) {
    el.innerHTML = `Last capture: ${ago_str} · ✗ ${result.error || "failed"}`;
  } else {
    el.textContent = `Last capture: ${ago_str}.`;
  }
}

function showStatus(kind, msg) {
  const el = $("status");
  el.className = kind;
  el.textContent = msg;
}

function normalizeUrl(raw) {
  let u = (raw || "").trim();
  if (!u) return "";
  u = u.replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(u)) {
    u = "http://" + u;
  }
  return u;
}

function readTimes() {
  const out = [];
  for (const id of ["time1", "time2", "time3"]) {
    const v = $(id).value;
    if (/^\d{2}:\d{2}$/.test(v)) out.push(v);
  }
  return out;
}

$("saveBtn").addEventListener("click", async () => {
  const backendUrl = normalizeUrl($("backendUrl").value);
  const token = $("token").value.trim();
  const autoCapture = $("autoCapture").checked;
  const captureTimes = readTimes();
  if (!backendUrl) { showStatus("error", "Backend URL is required."); return; }
  if (!token) { showStatus("error", "Auth token is required."); return; }
  if (autoCapture && captureTimes.length === 0) {
    showStatus("error", "Auto-capture is on but no valid times set.");
    return;
  }
  await chrome.storage.local.set({ backendUrl, token, autoCapture, captureTimes });
  $("backendUrl").value = backendUrl;
  showStatus("ok", autoCapture
    ? `Saved. Scheduled at ${captureTimes.join(", ")} daily.`
    : "Saved. Auto-capture off.");
});

$("testBtn").addEventListener("click", async () => {
  const backendUrl = normalizeUrl($("backendUrl").value);
  const token = $("token").value.trim();
  if (!backendUrl || !token) {
    showStatus("error", "Fill in both connection fields before testing.");
    return;
  }
  showStatus("ok", "Testing…");
  try {
    const res = await fetch(`${backendUrl}/health`, {
      method: "GET",
      headers: { "Authorization": `Bearer ${token}` },
    });
    if (res.ok) {
      showStatus("ok", `✓ Reached recorder at ${backendUrl} (${res.status}).`);
    } else if (res.status === 401 || res.status === 403) {
      showStatus("error", `Reached ${backendUrl} but auth was rejected (${res.status}). Re-copy the token from Meeting Recorder Settings.`);
    } else {
      showStatus("error", `${backendUrl} returned ${res.status}. Is the recorder running?`);
    }
  } catch (e) {
    showStatus("error", `Couldn't reach ${backendUrl} — is Meeting Recorder running? (${e.message})`);
  }
});

$("captureNowBtn").addEventListener("click", async () => {
  const backendUrl = normalizeUrl($("backendUrl").value);
  const token = $("token").value.trim();
  if (!backendUrl || !token) {
    showStatus("error", "Fill in connection first.");
    return;
  }
  showStatus("ok", "Capturing… 4 tabs will open in the background and close themselves. ~30–60 seconds.");
  $("captureNowBtn").disabled = true;
  try {
    const result = await chrome.runtime.sendMessage({
      type: "capture-and-send",
      backendUrl, token,
    });
    if (result?.ok) {
      const c = result.counts || {};
      const calN = c.calendar ?? 0;
      const calSuffix = (calN === 0 && result.calendarZeroReason)
        ? ` (${result.calendarZeroReason})` : "";
      showStatus("ok",
        `✓ Sent to Meeting Recorder. ` +
        `OWA: ${c.owa || 0}, Teams: ${c.teams || 0}, ` +
        `Inbox: ${c.inbox || 0}, Chat: ${c.chat || 0}, ` +
        `Calendar: ${calN} event${calN === 1 ? "" : "s"}${calSuffix}.`);
    } else {
      showStatus("error", `✗ ${result?.error || "Unknown error"}`);
    }
    // Refresh the last-capture display.
    const cfg = await chrome.storage.local.get({
      lastCaptureAt: 0, lastResult: null,
      lastCalendarCaptureAt: 0, lastCalendarResult: null,
    });
    renderLastCapture(cfg.lastCaptureAt, cfg.lastResult);
    renderCalendarStatus(cfg.lastCalendarCaptureAt, cfg.lastCalendarResult);
  } catch (e) {
    showStatus("error", `✗ ${e.message || String(e)}`);
  } finally {
    $("captureNowBtn").disabled = false;
  }
});

// ── Diagnose calendar capture (v1.3) ──────────────────────────────
//
// No DevTools, no pasted console script: this opens the calendar tab
// in the background, runs a read-only DOM probe against it, and
// prints exactly what it found. The result is meant to be copied and
// shared to get a correct selector written without ever seeing the
// tenant — see the v1.3 header comment in background.js.

function formatDiagnosticReport(report) {
  return JSON.stringify(report, null, 2);
}

$("diagnoseBtn").addEventListener("click", async () => {
  const out = $("diagnosticOutput");
  const copyBtn = $("copyDiagnosticBtn");
  $("diagnoseBtn").disabled = true;
  out.style.display = "block";
  copyBtn.style.display = "none";
  out.textContent = "Running… opens the calendar page in the background and " +
    "samples it at 2s / 5s / 10s / 15s. Takes about 15-20 seconds.";
  try {
    const report = await chrome.runtime.sendMessage({ type: "diagnose-calendar" });
    if (report?.ok === false && !report.finalUrl) {
      out.textContent = `Diagnostic failed: ${report.error || "unknown error"}`;
      return;
    }
    out.textContent = formatDiagnosticReport(report);
    copyBtn.style.display = "inline-block";
  } catch (e) {
    out.textContent = `Diagnostic failed: ${e.message || String(e)}`;
  } finally {
    $("diagnoseBtn").disabled = false;
  }
});

$("diagnoseApiBtn").addEventListener("click", async () => {
  const out = $("apiDiagnosticOutput");
  const copyBtn = $("copyApiDiagnosticBtn");
  $("diagnoseApiBtn").disabled = true;
  out.style.display = "block";
  copyBtn.style.display = "none";
  out.textContent = "Running… opens the calendar page in the background and asks " +
    "each candidate endpoint once. Takes about 10 seconds.";
  try {
    const report = await chrome.runtime.sendMessage({ type: "diagnose-calendar-api" });
    // `ok === false` with no results is a wrapper failure; a report
    // WITH results is a successful measurement even when every
    // candidate failed — that distinction is the point of the probe,
    // so it must survive into how the result is displayed.
    if (report?.ok === false && !(report.results || []).length) {
      out.textContent = `Diagnostic failed: ${report.error || "unknown error"}`;
      return;
    }
    out.textContent = formatDiagnosticReport(report);
    copyBtn.style.display = "inline-block";
  } catch (e) {
    out.textContent = `Diagnostic failed: ${e.message || String(e)}`;
  } finally {
    $("diagnoseApiBtn").disabled = false;
  }
});

$("copyApiDiagnosticBtn").addEventListener("click", async () => {
  const out = $("apiDiagnosticOutput");
  try {
    await navigator.clipboard.writeText(out.textContent || "");
    $("copyApiDiagnosticBtn").textContent = "Copied";
    setTimeout(() => { $("copyApiDiagnosticBtn").textContent = "Copy"; }, 1500);
  } catch (_) { /* clipboard denied — the text is selectable anyway */ }
});

$("copyDiagnosticBtn").addEventListener("click", async () => {
  const out = $("diagnosticOutput");
  try {
    await navigator.clipboard.writeText(out.textContent || "");
    const btn = $("copyDiagnosticBtn");
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = original; }, 1500);
  } catch (e) {
    showStatus("error", `Couldn't copy: ${e.message || String(e)}`);
  }
});

load();
