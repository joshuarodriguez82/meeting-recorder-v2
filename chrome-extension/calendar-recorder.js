// Passive recorder for Outlook's OWN calendar responses.
//
// Registered by background.js (registerCalendarRecorder) at
// `document_start` in the MAIN world, for the duration of a capture
// only, then unregistered.
//
// WHY THIS EXISTS — the short version:
//
// The Teams join URL, the attendee list and the invite body are not in
// the calendar grid. v1.6 tried to fetch them from the API and shipped
// four candidate endpoints, all modelled on classic OWA. The field run
// said `origin: outlook.cloud.microsoft`, `canaryPresent: false`, 401 —
// the new Outlook stack, no CSRF canary, bearer auth. Every guess was
// wrong, and each one cost a release/reinstall/re-run cycle.
//
// Replicating someone else's authenticated request means tracking their
// auth scheme forever. So this makes no request. Outlook already
// fetches all three fields to render the page; this reads the
// responses on the way back and lets Outlook authenticate however it
// likes, to whatever endpoint it likes.
//
// v1.8: WHAT COUNTS AS A CALENDAR RESPONSE CHANGED.
//
// v1.7 gated recording on a list of URL substrings ("calendarview",
// "/events", …). That list was another guess about a tenant this
// project cannot see — the same mistake as the endpoint list, one
// layer down — and if it guessed wrong the symptom was indistinguishable
// from every other failure: empty fields.
//
// The gate is now the CONTENT, not the URL. Any JSON response whose
// text contains a subject-ish key is parsed and offered to the
// extractor; whether it holds a meeting is decided by looking. A cheap
// string test runs first so this does not JSON.parse every response on
// the page.
//
// That deletes a whole failure mode rather than making it less likely,
// and it means `matched` now answers a question worth asking: not "did
// a URL look right" but "did a response actually contain a meeting".
//
// SAFETY PROPERTIES, all deliberate:
//   * Never modifies, blocks, delays or retries a request.
//   * Never reads a credential. Request headers — Authorization
//     included — are not touched. Response BODIES only.
//   * Never sends anything anywhere. Bodies sit on a page global the
//     extension reads back once and clears.
//   * Bounded in count and bytes, so a tab left open cannot grow it.
//   * Every path wrapped: a throw inside the recorder must never break
//     Outlook's own fetch. A broken calendar is far worse than a
//     missing attendee list.

(function installCalendarResponseRecorder() {
  try {
    if (window.__mrCalRecorderInstalled) return;
    window.__mrCalRecorderInstalled = true;

    const MAX_BODIES = 80;
    const MAX_BYTES = 6 * 1024 * 1024;
    // Above this a single response is not a calendar page worth
    // parsing on the page's own thread — it is a bundle or a blob.
    const MAX_ONE_BODY = 3 * 1024 * 1024;

    const store = {
      bodies: [],
      bytes: 0,
      // Every response that went past the recorder, calendar or not.
      // ZERO here is the single most diagnostic number this file
      // produces: it means the page is not fetching through the main
      // world at all (a service worker or a web worker is), and no
      // amount of tuning what we record would ever have helped.
      seen: 0,
      // Responses that were JSON and contained a subject-ish key.
      matched: 0,
      // Matched but discarded at the caps above.
      dropped: 0,
      // Parsed as JSON but held nothing meeting-shaped. Distinguishes
      // "we saw calendar traffic we could not read" from "we saw no
      // calendar traffic".
      notMeetingShaped: 0,
    };
    window.__mrCal = store;

    // Cheap pre-filter. A response with none of these cannot contain a
    // meeting under any of the vocabularies the extractor knows, so it
    // is rejected without paying for JSON.parse.
    const SUBJECT_HINTS = ['"subject"', '"Subject"', '"normalizedSubject"',
                           '"title"', '"Title"'];

    const looksLikeMeetingJson = (text) => {
      if (!text || text.length > MAX_ONE_BODY) return false;
      const head = text.length > 200000 ? text.slice(0, 200000) : text;
      return SUBJECT_HINTS.some((h) => head.includes(h));
    };

    const record = (text) => {
      if (!text) return;
      if (!looksLikeMeetingJson(text)) return;
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch (_) {
        return;  // not JSON — nothing here to read
      }
      store.matched++;
      if (store.bodies.length >= MAX_BODIES
          || store.bytes + text.length > MAX_BYTES) {
        // Counted, never silently discarded — a truncated harvest that
        // reads as a complete one is the defect this project keeps
        // shipping.
        store.dropped++;
        return;
      }
      store.bodies.push(parsed);
      store.bytes += text.length;
    };

    const origFetch = window.fetch;
    if (typeof origFetch === "function") {
      window.fetch = function (...args) {
        const p = origFetch.apply(this, args);
        try {
          return p.then((res) => {
            try {
              store.seen++;
              // .clone() so the page's own consumer still receives an
              // unread body. Reading the original would break Outlook.
              res.clone().text()
                .then((t) => record(t))
                .catch(() => { /* body already consumed — skip */ });
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
      const origSend = XHR.prototype.send;
      XHR.prototype.send = function (...args) {
        try {
          this.addEventListener("load", () => {
            try {
              store.seen++;
              // responseText throws for arraybuffer/blob types, so it
              // is only read when the type actually permits it.
              const t = (this.responseType === "" || this.responseType === "text")
                ? this.responseText : "";
              record(t);
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
