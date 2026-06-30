// Meeting Recorder Chrome extension — popup logic.
//
// Click handler kicks off the background service worker's capture
// flow. UI surfaces busy / ok / error states clearly so the user
// knows whether to retry, fix config, or open Meeting Recorder.

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

function openOptions(e) {
  if (e) e.preventDefault();
  chrome.runtime.openOptionsPage();
}

$("settingsLink").addEventListener("click", openOptions);
$("optionsLink").addEventListener("click", openOptions);

$("captureBtn").addEventListener("click", async () => {
  $("captureBtn").disabled = true;
  setStatus("busy", "Opening Outlook + Teams tabs, reading content, sending to recorder…");

  try {
    const cfg = await getConfig();
    if (!cfg.backendUrl || !cfg.token) {
      throw new Error("Backend URL or token not configured. Open Settings.");
    }

    // Ask the background service worker to run the capture. It
    // handles the multi-tab orchestration so the popup doesn't have
    // to stay open (popups close when they lose focus).
    const result = await chrome.runtime.sendMessage({
      type: "capture-and-send",
      backendUrl: cfg.backendUrl,
      token: cfg.token,
    });

    if (result?.ok) {
      const parts = [];
      if (result.owa_chars) parts.push(`OWA: ${result.owa_chars} chars`);
      if (result.teams_chars) parts.push(`Teams: ${result.teams_chars} chars`);
      const summary = parts.length ? ` (${parts.join(", ")})` : "";
      setStatus("ok", `✓ Sent to Meeting Recorder${summary}. Open the Today tab to see the parsed brief.`);
    } else {
      setStatus("error", `✗ ${result?.error || "Unknown error"}`);
    }
  } catch (e) {
    setStatus("error", `✗ ${e.message || String(e)}`);
  } finally {
    $("captureBtn").disabled = false;
    refreshConfigBanner();
  }
});

refreshConfigBanner();
