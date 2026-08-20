// Passive recorder for Outlook's OWN calendar responses.
//
// Registered by background.js (registerCalendarRecorder) at
// `document_start` in the MAIN world, for the duration of a capture
// only, then unregistered.
//
// WHY THIS FILE EXISTS AT ALL — the short version of the argument in
// background.js:
//
// The Teams join URL, the attendee list and the invite body are not in
// the calendar grid. v1.6 tried to fetch them from the API and shipped
// four candidate endpoints, all modelled on classic OWA. The field run
// said `origin: outlook.cloud.microsoft`, `canaryPresent: false`, and
// 401 on the one candidate it could attempt: that tenant is the new
// Outlook stack, which has no CSRF canary and uses bearer tokens.
// Every guess was wrong, and each one cost a full
// release/reinstall/re-run cycle.
//
// Replicating someone else's authenticated request means tracking
// their auth scheme forever. So this does not make a request. Outlook
// already fetches all three fields to render the page; this records
// the responses on the way back and lets Outlook authenticate however
// it likes, to whatever endpoint it likes. Classic OWA and the new
// stack are handled identically because it never has to tell them
// apart.
//
// SAFETY PROPERTIES, all deliberate:
//   * Never modifies, blocks, delays or retries a request.
//   * Never reads or stores a credential. Request headers — including
//     Authorization — are not touched. Only response BODIES are read.
//   * Never sends anything anywhere. Bodies sit on a page global that
//     background.js reads back once and clears.
//   * Bounded in count and bytes, so a calendar tab left open cannot
//     grow it without limit.
//   * Every path is wrapped: a throw inside the recorder must never
//     break Outlook's own fetch. A broken calendar is a far worse
//     outcome than a missing attendee list.

(function installCalendarResponseRecorder() {
  // URL shapes worth recording. Broad on host — not knowing which host
  // or API version the tenant uses is the entire point — and narrow on
  // path, so this records calendar traffic rather than every telemetry
  // beacon on the page.
  //
  // This list lives HERE and nowhere else. An earlier draft kept a
  // copy in background.js too, which is the same drift trap that
  // JOIN_PROVIDER_PATTERNS exists as one list to avoid.
  const URL_HINTS = [
    "calendarview", "calendarevents", "/events", "finditem",
    "getcalendarevent", "calendar/view", "/me/calendars", "getitem",
    "instances",
  ];

  try {
    if (window.__mrCalRecorderInstalled) return;
    window.__mrCalRecorderInstalled = true;

    const MAX_BODIES = 60;
    const MAX_BYTES = 4 * 1024 * 1024;
    const store = { bodies: [], bytes: 0, seen: 0, matched: 0, dropped: 0 };
    window.__mrCal = store;

    const interesting = (url) => {
      const u = String(url || "").toLowerCase();
      return URL_HINTS.some((h) => u.includes(h));
    };

    const record = (url, text) => {
      if (!text) return;
      store.matched++;
      if (store.bodies.length >= MAX_BODIES
          || store.bytes + text.length > MAX_BYTES) {
        // Counted, never silently discarded — a truncated harvest that
        // reads as a complete one is the defect this project keeps
        // hitting.
        store.dropped++;
        return;
      }
      try {
        store.bodies.push(JSON.parse(text));
        store.bytes += text.length;
      } catch (_) { /* not JSON — nothing here to read */ }
    };

    const origFetch = window.fetch;
    if (typeof origFetch === "function") {
      window.fetch = function (...args) {
        const p = origFetch.apply(this, args);
        try {
          return p.then((res) => {
            try {
              store.seen++;
              const url = (res && res.url)
                || (args[0] && args[0].url)
                || args[0];
              if (interesting(url)) {
                // .clone() so the page's own consumer still receives an
                // unread body. Reading the original would break Outlook.
                res.clone().text()
                  .then((t) => record(url, t))
                  .catch(() => { /* body already gone — skip */ });
              }
            } catch (_) { /* never break a page fetch */ }
            return res;
          });
        } catch (_) {
          return p;
        }
      };
    }

    const XHR = window.XMLHttpRequest;
    if (XHR && XHR.prototype) {
      const origOpen = XHR.prototype.open;
      const origSend = XHR.prototype.send;
      XHR.prototype.open = function (method, url, ...rest) {
        try { this.__mrUrl = url; } catch (_) { /* frozen — skip */ }
        return origOpen.call(this, method, url, ...rest);
      };
      XHR.prototype.send = function (...args) {
        try {
          this.addEventListener("load", () => {
            try {
              store.seen++;
              if (!interesting(this.__mrUrl)) return;
              // responseText throws for arraybuffer/blob types, so it is
              // only read when the type actually permits it.
              const t = (this.responseType === "" || this.responseType === "text")
                ? this.responseText : "";
              record(this.__mrUrl, t);
            } catch (_) { /* ignore */ }
          });
        } catch (_) { /* ignore */ }
        return origSend.apply(this, args);
      };
    }
  } catch (_) {
    // Deliberately silent and total: if the recorder cannot install,
    // capture proceeds exactly as it did before it existed.
  }
})();
