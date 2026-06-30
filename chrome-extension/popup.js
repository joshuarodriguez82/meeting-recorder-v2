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

async function renderLastCapture() {
  const cfg = await chrome.storage.local.get({ lastCaptureAt: 0, lastResult: null });
  const el = $("lastCapture");
  if (!cfg.lastCaptureAt) {
    el.textContent = "";
    return;
  }
  const mins = Math.round((Date.now() - cfg.lastCaptureAt) / 60_000);
  const ago = mins < 60
    ? `${mins} min ago`
    : `${Math.floor(mins / 60)}h ${mins % 60}m ago`;
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

function openOptions(e) {
  if (e) e.preventDefault();
  chrome.runtime.openOptionsPage();
}

$("settingsLink").addEventListener("click", openOptions);
$("optionsLink").addEventListener("click", openOptions);

$("captureBtn").addEventListener("click", async () => {
  $("captureBtn").disabled = true;
  setStatus("busy", "Opening 4 tabs in the background (OWA, Teams, Inbox, Chat), reading content, sending to recorder… ~30–60 sec.");

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
      setStatus("ok", `✓ Sent (${parts.join(", ") || "0 chars"}). Open the Today tab for the parsed brief.`);
    } else {
      setStatus("error", `✗ ${result?.error || "Unknown error"}`);
    }
    await renderLastCapture();
  } catch (e) {
    setStatus("error", `✗ ${e.message || String(e)}`);
  } finally {
    $("captureBtn").disabled = false;
    refreshConfigBanner();
  }
});

refreshConfigBanner();
renderLastCapture();
