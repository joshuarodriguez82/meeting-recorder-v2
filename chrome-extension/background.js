// Meeting Recorder Chrome extension — background service worker.
//
// Orchestrates the capture flow:
//   1. Open OWA day view (https://outlook.office.com/calendar/view/day)
//      in a new tab. Wait for the React tree to mount enough content
//      that we have a real calendar grid (uses inner-text size as the
//      "is the SPA done" signal).
//   2. Execute a content script in that tab to extract the inner text
//      of [role="main"]. Close the tab.
//   3. Open Teams Activity (https://teams.microsoft.com/v2/?clientType=desktop#/activity).
//      Same wait + extract.
//   4. POST both texts to the recorder's backend at
//      /briefing/extension-import with the auth token.
//
// Key design choice: this runs in the USER'S real Chrome (not a
// Playwright-controlled instance), so Microsoft's bot detection
// doesn't fire and the user's real session cookies authenticate the
// page loads. That's the whole point of doing this as an extension
// instead of the previous Playwright approach.

const OWA_URL = "https://outlook.office.com/calendar/view/day";
const TEAMS_URL = "https://teams.microsoft.com/v2/?clientType=desktop#/activity";

// Poll timing. Each tab gets up to MAX_WAIT_MS to settle; we extract
// every POLL_MS to see if the inner-text size has reached the
// "looks like real content" threshold or has stopped growing.
const POLL_MS = 1000;
const MAX_WAIT_MS = 25_000;
const TARGET_CHARS = 1500;
const MIN_USEFUL_CHARS = 500;
const STABILITY_POLLS = 3;

// Listen for popup-initiated capture requests. async response is
// returned via the standard return-true sendResponse pattern.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "capture-and-send") {
    captureAndSend(msg.backendUrl, msg.token)
      .then(sendResponse)
      .catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
    return true; // keep the message channel open for the async response
  }
});

async function captureAndSend(backendUrl, token) {
  if (!backendUrl || !token) {
    return { ok: false, error: "Backend URL or token not configured. Open Settings." };
  }

  let owaText = "";
  let teamsText = "";
  let owaErr = "";
  let teamsErr = "";

  // OWA capture. We don't fail the whole flow if Teams flakes, but
  // OWA failing means the brief has no calendar — that's a real
  // failure we surface to the user.
  try {
    owaText = await captureUrl(OWA_URL, "OWA");
  } catch (e) {
    owaErr = e.message || String(e);
    console.error("[Meeting Recorder ext] OWA capture failed:", e);
  }

  // Teams capture. Best-effort; OWA-only brief is still useful.
  try {
    teamsText = await captureUrl(TEAMS_URL, "Teams");
  } catch (e) {
    teamsErr = e.message || String(e);
    console.error("[Meeting Recorder ext] Teams capture failed:", e);
  }

  if (!owaText && !teamsText) {
    return {
      ok: false,
      error: `Nothing captured. OWA: ${owaErr || "empty"} | Teams: ${teamsErr || "empty"}`,
    };
  }

  // POST to the recorder backend. Backend's /briefing/extension-import
  // takes the two text blobs, runs them through the same
  // parse_daily_briefing pipeline the existing Import button uses,
  // and stores via DailyBriefingService.
  try {
    const res = await fetch(`${backendUrl}/briefing/extension-import`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`,
      },
      body: JSON.stringify({
        owa_text: owaText,
        teams_text: teamsText,
      }),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      return {
        ok: false,
        error: `Backend returned ${res.status}: ${body.slice(0, 200)}`,
      };
    }
    return {
      ok: true,
      owa_chars: owaText.length,
      teams_chars: teamsText.length,
    };
  } catch (e) {
    return {
      ok: false,
      error: `Couldn't reach ${backendUrl} — is Meeting Recorder running? (${e.message})`,
    };
  }
}

async function captureUrl(url, label) {
  // Open in a new tab in the current window. The user sees the tab
  // briefly while we wait for content to render; this is intentional
  // — closing it is exactly what makes the capture feel "active"
  // rather than something silent that could leak data.
  const tab = await chrome.tabs.create({ url, active: false });
  const tabId = tab.id;

  try {
    // Wait for the tab to finish initial load.
    await waitForTabComplete(tabId);

    // Poll for content to settle.
    const start = Date.now();
    let lastLen = -1;
    let stableCount = 0;
    let lastText = "";
    while (Date.now() - start < MAX_WAIT_MS) {
      let text = "";
      try {
        text = await readMainText(tabId);
      } catch (e) {
        // Tab might have been closed by something; just stop polling.
        console.warn(`[ext] ${label} read failed:`, e);
        break;
      }
      text = (text || "").trim();
      lastText = text;
      if (text.length >= TARGET_CHARS) {
        console.log(`[ext] ${label}: target reached ${text.length} chars in ${Math.round((Date.now()-start)/1000)}s`);
        return text;
      }
      if (text.length === lastLen && text.length > 0) {
        stableCount += 1;
        if (stableCount >= STABILITY_POLLS) {
          console.log(`[ext] ${label}: stable at ${text.length} chars after ${Math.round((Date.now()-start)/1000)}s`);
          return text;
        }
      } else {
        stableCount = 0;
        lastLen = text.length;
      }
      await sleep(POLL_MS);
    }
    console.warn(`[ext] ${label}: hit max-wait at ${lastText.length} chars`);
    return lastText;
  } finally {
    // Always close the tab so we don't leave a sea of OWA tabs.
    try { await chrome.tabs.remove(tabId); } catch (_) {}
  }
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
    // Use a self-contained function — no closures, no imports.
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
