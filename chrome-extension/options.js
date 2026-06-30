// Meeting Recorder Chrome extension — options page.
//
// Stores backendUrl + token in chrome.storage.local (extension-
// scoped, not synced to user's Google account because the token is
// per-machine). Test button hits the recorder's /health endpoint
// with the token to validate both fields in one shot before the
// user closes the page.

const $ = (id) => document.getElementById(id);

async function load() {
  const cfg = await chrome.storage.local.get({ backendUrl: "", token: "" });
  $("backendUrl").value = cfg.backendUrl;
  $("token").value = cfg.token;
}

function showStatus(kind, msg) {
  const el = $("status");
  el.className = kind;
  el.textContent = msg;
}

function normalizeUrl(raw) {
  let u = (raw || "").trim();
  if (!u) return "";
  // Strip trailing slash so we don't end up POSTing to /briefing//import.
  u = u.replace(/\/+$/, "");
  // If user pasted just "127.0.0.1:NNNN", prepend http://.
  if (!/^https?:\/\//i.test(u)) {
    u = "http://" + u;
  }
  return u;
}

$("saveBtn").addEventListener("click", async () => {
  const backendUrl = normalizeUrl($("backendUrl").value);
  const token = $("token").value.trim();
  if (!backendUrl) {
    showStatus("error", "Backend URL is required.");
    return;
  }
  if (!token) {
    showStatus("error", "Auth token is required.");
    return;
  }
  await chrome.storage.local.set({ backendUrl, token });
  $("backendUrl").value = backendUrl;
  showStatus("ok", "Saved.");
});

$("testBtn").addEventListener("click", async () => {
  const backendUrl = normalizeUrl($("backendUrl").value);
  const token = $("token").value.trim();
  if (!backendUrl || !token) {
    showStatus("error", "Fill in both fields before testing.");
    return;
  }
  showStatus("ok", "Testing…");
  try {
    const res = await fetch(`${backendUrl}/health`, {
      method: "GET",
      headers: { "Authorization": `Bearer ${token}` },
    });
    if (res.ok) {
      const body = await res.text();
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

load();
