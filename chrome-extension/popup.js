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
    const layerLabel = r.layer === "aria-label" ? "structured"
      : r.layer === "generic-node" ? "structured (fallback nodes)"
      : "⚠ text fallback";
    el.innerHTML = `Calendar: ${ago} · ${n} event${n === 1 ? "" : "s"} · ${layerLabel}`;
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

$("captureBtn").addEventListener("click", async () => {
  $("captureBtn").disabled = true;
  setStatus("busy", "Opening tabs in the background (OWA, Teams, Inbox, Chat, Calendar), reading content, sending to recorder… ~30–90 sec.");

  try {
    const cfg = await getConfig();
    if (!cfg.backendUrl || !cfg.token) {
      throw new Error("Backend URL or token not configured. Open Settings.");
    }
    const result = await chrome.runtime.sendMessage({
      type: "capture-and-send",
      backendUrl: cfg.backendUrl,
      token: cfg.token,
    });
    if (result?.ok) {
      const c = result.counts || {};
      const parts = [];
      if (c.owa) parts.push(`OWA: ${c.owa}`);
      if (c.teams) parts.push(`Teams: ${c.teams}`);
      if (c.inbox) parts.push(`Inbox: ${c.inbox}`);
      if (c.chat) parts.push(`Chat: ${c.chat}`);
      parts.push(`Calendar: ${c.calendar ?? 0} event${c.calendar === 1 ? "" : "s"}`);
      setStatus("ok", `✓ Sent (${parts.join(", ")}). Open the Today tab for the parsed brief.`);
    } else {
      setStatus("error", `✗ ${result?.error || "Unknown error"}`);
    }
    await renderLastCapture();
    await renderCalendarStatus();
  } catch (e) {
    setStatus("error", `✗ ${e.message || String(e)}`);
  } finally {
    $("captureBtn").disabled = false;
    refreshConfigBanner();
  }
});

refreshConfigBanner();
renderLastCapture();
renderCalendarStatus();
