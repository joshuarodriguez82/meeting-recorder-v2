// Node test suite for the calendar-parsing/DOM-scan logic inside
// chrome-extension/background.js.
//
// chrome-extension/ has no build step and is not an npm package —
// the extension itself ships zero dependencies and loads straight
// into Chrome. This suite doesn't change that: it loads the REAL
// background.js source with Node's `vm` module against a minimal
// `chrome` stub (only enough to satisfy the four top-level
// `chrome.*.addListener(...)` registration calls — none of those
// listeners are ever invoked here) and then calls the exact functions
// the extension calls, exactly the way a prior scratch verification
// harness for this same file did. That harness never landed in the
// repo (it lived only in a session scratchpad); this one does, run
// through node:test instead of hand-rolled pass/fail counting.
//
// Run:  node --test chrome-extension/tests/
//
// Two families of function are exercised:
//
//   PURE (parseMeetingLabel, extractEventsFromCandidates,
//   shouldStopPolling, classifyZeroReason, ...) — plain data in, plain
//   data out. No DOM.
//
//   DOM-WALKING (_calendarDomScanFunc) — touches only `document`,
//   read as an ambient global. In the browser that's the real page;
//   here it's a hand-built fake DOM (see `FakeDom` below) assigned to
//   `sandbox.document` before each call. The walk only ever uses
//   `.children`, `.parentElement`, `.getAttribute`, `.hasAttribute`,
//   `.tagName`, `.innerText`/`.textContent`, `.shadowRoot`,
//   `.contentDocument`, and `.getBoundingClientRect` — a small enough
//   surface that the fake objects below satisfy it without needing a
//   real DOM implementation.
//
// NOT covered here: _calendarDiagnosticProbeFunc and the live
// Outlook Web DOM itself. The probe relies on real querySelectorAll
// (including querySelectorAll inside iframe documents/shadow roots,
// which real browsers support natively) — reimplementing a CSS
// selector engine in the fake DOM below just to test the probe isn't
// worth it for a read-only, user-triggered diagnostic tool. And there
// is no way to sign in to a real Outlook Web tenant from this
// environment — see the v1.3 header comment in background.js.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const BG_PATH = path.join(__dirname, "..", "background.js");
const SRC = fs.readFileSync(BG_PATH, "utf8");

function loadSandbox() {
  const noop = () => {};
  const chromeStub = {
    runtime: {
      onInstalled: { addListener: noop },
      onStartup: { addListener: noop },
      onMessage: { addListener: noop },
    },
    storage: {
      onChanged: { addListener: noop },
      local: { get: async () => ({}), set: async () => {} },
    },
    alarms: { onAlarm: { addListener: noop }, create: noop, clearAll: async () => {} },
    tabs: {},
    scripting: {},
  };
  const sandbox = { chrome: chromeStub, console };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: BG_PATH });
  return sandbox;
}

const sandbox = loadSandbox();

// ── Fake DOM ─────────────────────────────────────────────────────────
//
// Deliberately minimal — see the file header for the exact surface
// _calendarDomScanFunc needs.

function el(tag, attrs = {}, opts = {}) {
  const attrsCopy = { ...attrs };
  const node = {
    tagName: tag.toUpperCase(),
    children: [],
    parentElement: null,
    shadowRoot: opts.shadowRoot || null,
    contentDocument: "contentDocument" in opts ? opts.contentDocument : undefined,
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attrsCopy, name) ? attrsCopy[name] : null;
    },
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attrsCopy, name);
    },
    getBoundingClientRect() {
      return opts.rect || { left: 0, width: 0 };
    },
  };
  Object.defineProperty(node, "innerText", { get: () => opts.text || "" });
  Object.defineProperty(node, "textContent", { get: () => opts.text || "" });
  return node;
}

function append(parent, ...kids) {
  for (const k of kids) {
    k.parentElement = parent;
    parent.children.push(k);
  }
  return parent;
}

function doc(children) {
  return { children };
}

function setDocument(root) {
  sandbox.document = root;
}

// ── _calendarDomScanFunc: sibling/ancestor/descendant time lookup ────

test("candidate with time in an adjacent sibling (not its own label) is captured", () => {
  const subjectEl = el("div", { role: "button", "aria-label": "Team Sync" });
  const timeSibling = el("span", {}, { text: "10:00 AM - 10:30 AM" });
  const wrapper = el("div");
  append(wrapper, subjectEl, timeSibling);
  const dayColumn = el("div", { "data-date": "2026-08-19" });
  append(dayColumn, wrapper);
  setDocument(doc([dayColumn]));

  const { candidates } = sandbox._calendarDomScanFunc();
  const found = candidates.find((c) => c.label.startsWith("Team Sync"));
  assert.ok(found, `expected a Team Sync candidate, got ${JSON.stringify(candidates)}`);
  assert.equal(found.label, "Team Sync, 10:00 AM - 10:30 AM");
  assert.equal(found.columnDateIso, "2026-08-19");

  // And it survives the pure parser end-to-end.
  const extraction = sandbox.extractEventsFromCandidates(candidates, { fallbackYear: 2026 });
  const ev = extraction.events.find((e) => e.subject === "Team Sync");
  assert.ok(ev, `expected a parsed Team Sync event, got ${JSON.stringify(extraction.events)}`);
  assert.equal(ev.start, "2026-08-19T10:00:00");
  assert.equal(ev.end, "2026-08-19T10:30:00");
});

test("candidate with time in an ancestor gridcell (not its own label, no sibling) is captured", () => {
  const subjectEl = el("div", { role: "button", "aria-label": "Budget Sync" });
  const midWrapper = el("div");
  append(midWrapper, subjectEl);
  const gridCell = el("div", { role: "gridcell", "aria-label": "10:00 AM - 10:30 AM" });
  append(gridCell, midWrapper);
  setDocument(doc([gridCell]));

  const { candidates } = sandbox._calendarDomScanFunc();
  const found = candidates.find((c) => c.label.startsWith("Budget Sync"));
  assert.ok(found, `expected a Budget Sync candidate, got ${JSON.stringify(candidates)}`);
  assert.equal(found.label, "Budget Sync, 10:00 AM - 10:30 AM");
});

test("candidate with two <time datetime> descendants resolves as a structured start/end pair", () => {
  const t1 = el("time", { datetime: "2026-08-19T10:00:00" });
  const t2 = el("time", { datetime: "2026-08-19T10:30:00" });
  const subjectEl = el("div", { role: "button", "aria-label": "Budget Review" });
  append(subjectEl, t1, t2);
  setDocument(doc([subjectEl]));

  const { candidates } = sandbox._calendarDomScanFunc();
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].label, "Budget Review");
  assert.equal(candidates[0].structuredStart, "2026-08-19T10:00:00");
  assert.equal(candidates[0].structuredEnd, "2026-08-19T10:30:00");

  const extraction = sandbox.extractEventsFromCandidates(candidates, { fallbackYear: 2026 });
  assert.equal(extraction.events.length, 1);
  assert.equal(extraction.events[0].subject, "Budget Review");
  assert.equal(extraction.events[0].start, "2026-08-19T10:00:00");
  assert.equal(extraction.events[0].end, "2026-08-19T10:30:00");
});

test("a candidate's own datetime/data-* start+end attributes also resolve as a structured pair", () => {
  const subjectEl = el("div", {
    role: "button",
    "aria-label": "Standup",
    "data-start": "2026-08-19T09:00:00",
    "data-end": "2026-08-19T09:15:00",
  });
  setDocument(doc([subjectEl]));

  const { candidates } = sandbox._calendarDomScanFunc();
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].structuredStart, "2026-08-19T09:00:00");
  assert.equal(candidates[0].structuredEnd, "2026-08-19T09:15:00");
});

// ── _calendarDomScanFunc: same-origin iframe / open shadow root ──────

test("a candidate inside a same-origin iframe document is found", () => {
  const innerCandidate = el("div", {
    role: "button",
    "aria-label": "Iframe Meeting, 10:00 AM to 10:30 AM",
  });
  const innerDoc = doc([innerCandidate]);
  const iframeEl = el("iframe", {}, { contentDocument: innerDoc });
  setDocument(doc([iframeEl]));

  const { candidates, diag } = sandbox._calendarDomScanFunc();
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].label, "Iframe Meeting, 10:00 AM to 10:30 AM");
  assert.equal(diag.iframesSeen, 1);
  assert.equal(diag.iframesEntered, 1);
});

test("a cross-origin iframe (contentDocument inaccessible) is skipped, not thrown on", () => {
  const iframeEl = el("iframe", {}, { contentDocument: null });
  setDocument(doc([iframeEl]));

  const { candidates, diag } = sandbox._calendarDomScanFunc();
  assert.equal(candidates.length, 0);
  assert.equal(diag.iframesSeen, 1);
  assert.equal(diag.iframesEntered, 0);
});

test("a candidate inside an open shadow root is found", () => {
  const shadowCandidate = el("div", {
    role: "button",
    "aria-label": "Shadow Meeting, 2:00 PM to 2:30 PM",
  });
  const shadowRoot = doc([shadowCandidate]);
  const hostEl = el("div", {}, { shadowRoot });
  setDocument(doc([hostEl]));

  const { candidates, diag } = sandbox._calendarDomScanFunc();
  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].label, "Shadow Meeting, 2:00 PM to 2:30 PM");
  assert.equal(diag.shadowRootsSeen, 1);
});

test("non-meeting text anywhere in the tree never throws and never yields an event", () => {
  // The DOM scan's own hint regex is deliberately loose (it only
  // decides what's worth handing to the strict pure parser); some
  // noise can still surface as a raw CANDIDATE here (e.g. "3 of 5"
  // contains a bare digit), same as pre-v1.3. What must hold is that
  // it never throws and the strict parser downstream still produces
  // zero events for it — covered by the "zero meeting-shaped labels"
  // extractEventsFromCandidates test below. This test only asserts
  // the DOM-scan half doesn't blow up on label-free noise.
  const noise1 = el("div", { role: "button", "aria-label": "Settings" });
  const noise2 = el("div", { role: "listitem" }, { text: "Inbox" });
  setDocument(doc([noise1, noise2]));

  const { candidates } = sandbox._calendarDomScanFunc();
  assert.equal(candidates.length, 0);
});

// ── shouldStopPolling: retry-until-stable logic (pure) ────────────────

test("shouldStopPolling: too few samples never stops", () => {
  assert.equal(sandbox.shouldStopPolling([0, 0]), false);
  assert.equal(sandbox.shouldStopPolling([5, 5]), false);
});

test("shouldStopPolling: a stable NON-ZERO tail stops immediately", () => {
  assert.equal(sandbox.shouldStopPolling([0, 2, 5, 5, 5]), true);
  // Stops as soon as the tail is long enough — doesn't need the whole
  // history to be uniform, only the last `stabilityPolls`.
  assert.equal(sandbox.shouldStopPolling([9, 9, 3, 3, 3]), true);
});

test("shouldStopPolling: a stable ZERO tail does not stop before the floor", () => {
  assert.equal(sandbox.shouldStopPolling([0, 0, 0]), false);
  assert.equal(sandbox.shouldStopPolling([0, 0, 0, 0]), false);
});

test("shouldStopPolling: a stable ZERO tail stops once the floor is reached", () => {
  assert.equal(sandbox.shouldStopPolling([0, 0, 0, 0, 0]), true);
});

test("shouldStopPolling: fluctuating counts never stop", () => {
  assert.equal(sandbox.shouldStopPolling([1, 2, 3, 4, 5, 6, 7, 8]), false);
});

test("shouldStopPolling: custom stabilityPolls/minPollsBeforeZeroExit are honored", () => {
  assert.equal(sandbox.shouldStopPolling([7, 7], { stabilityPolls: 2 }), true);
  assert.equal(
    sandbox.shouldStopPolling([0, 0], { stabilityPolls: 2, minPollsBeforeZeroExit: 2 }),
    true,
  );
});

// ── classifyZeroReason: distinct zero-reason classifications (pure) ──

test("classifyZeroReason: still-rendering takes priority over stats", () => {
  const stats = { scanned: 5, allDay: 0, dateUnresolved: 0 };
  const reason = sandbox.classifyZeroReason(stats, { stillRendering: true });
  assert.match(reason, /still rendering/);
});

test("classifyZeroReason: no candidates found at all", () => {
  const stats = { scanned: 0, allDay: 0, dateUnresolved: 0 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /no candidate elements found/);
});

test("classifyZeroReason: candidates found but none parseable", () => {
  const stats = { scanned: 14, allDay: 0, dateUnresolved: 0 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /found 14 candidates, none had a parseable time/);
});

test("classifyZeroReason: candidates found but all all-day", () => {
  const stats = { scanned: 3, allDay: 3, dateUnresolved: 0 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /all all-day/);
});

test("classifyZeroReason: candidates found with a time but no resolvable date", () => {
  const stats = { scanned: 2, allDay: 0, dateUnresolved: 2 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /no resolvable date/);
});

test("classifyZeroReason: singular phrasing for exactly one candidate", () => {
  const stats = { scanned: 1, allDay: 0, dateUnresolved: 0 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /found 1 candidate, none had a parseable time/);
});

// ── extractEventsFromCandidates / parseMeetingLabel: regression set ──
// (condensed from the prior scratch harness's fixtures, built from the
// user's own field-reported subjects — pipe, slash, FW: prefix,
// trailing space, all-day, multi-day, nested duplicates.)

const FALLBACK_YEAR = 2026;
const COLUMN_DATE = "2026-08-13";

test("realistic single-day fixture: all 5 real meetings captured (was 1 of 5 pre-v1.2)", () => {
  const dayLabels = [
    "AWS Daily Pulse Call, 10:00 AM to 10:15 AM, Microsoft Teams Meeting, Zoe Doe",
    "PRIORITY: AWS Sales| Active Project Status Reviews and Escalations, 10:00 AM to 10:30 AM, Microsoft Teams Meeting, Will Poe",
    "FW: AWS Connect - Italy / ECC next steps: weekly team connect, 11:30 AM to 12:00 PM",
    "AWS/PGE - IVA PoC Sync-up, 1:00 PM to 1:30 PM, Microsoft Teams Meeting",
    "AI Transformation Stand Up , 7:30 AM to 8:30 AM",
  ];
  const candidates = dayLabels.map((label) => ({ label, columnDateIso: COLUMN_DATE, layer: "aria-label" }));
  const result = sandbox.extractEventsFromCandidates(candidates, { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 5, JSON.stringify(result.stats));

  const subjects = result.events.map((e) => e.subject).sort();
  assert.ok(subjects.includes("PRIORITY: AWS Sales| Active Project Status Reviews and Escalations"));
  assert.ok(subjects.includes("FW: AWS Connect - Italy / ECC next steps: weekly team connect"));
  assert.ok(subjects.includes("AWS/PGE - IVA PoC Sync-up"));
  assert.ok(subjects.includes("AI Transformation Stand Up"));

  const pulseCall = result.events.find((e) => e.subject === "AWS Daily Pulse Call");
  assert.ok(pulseCall);
  assert.equal(pulseCall.start, "2026-08-13T10:00:00");
  assert.equal(pulseCall.end, "2026-08-13T10:15:00");
});

test("all-day event is recognized and excluded, not miscounted as an error", () => {
  const parsed = sandbox.parseMeetingLabel("Company Holiday, All day", COLUMN_DATE, FALLBACK_YEAR);
  assert.equal(parsed.kind, "all-day");

  const result = sandbox.extractEventsFromCandidates(
    [{ label: "Company Holiday, All day", columnDateIso: COLUMN_DATE, layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR },
  );
  assert.equal(result.events.length, 0);
  assert.equal(result.stats.allDay, 1);
  assert.equal(result.stats.dateUnresolved, 0);
});

test("multi-day span parses as a single event with correct start/end days", () => {
  const parsed = sandbox.parseMeetingLabel(
    "Company Offsite, August 17, 9:00 AM to August 19, 5:00 PM", null, FALLBACK_YEAR,
  );
  assert.equal(parsed.kind, "event");
  assert.equal(parsed.startIso, "2026-08-17T09:00:00");
  assert.equal(parsed.endIso, "2026-08-19T17:00:00");
  assert.equal(parsed.subject, "Company Offsite");
});

test("3 nested-duplicate candidates for the same event collapse to exactly 1", () => {
  const dupLabel = "AWS Daily Pulse Call, 10:00 AM to 10:15 AM, Microsoft Teams Meeting";
  const candidates = [
    { label: dupLabel, columnDateIso: COLUMN_DATE, layer: "aria-label" },
    { label: dupLabel, columnDateIso: COLUMN_DATE, layer: "aria-label" },
    { label: dupLabel, columnDateIso: COLUMN_DATE, layer: "generic-node" },
  ];
  const result = sandbox.extractEventsFromCandidates(candidates, { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 1);
  assert.equal(result.stats.deduped, 2);
  assert.equal(sandbox.dominantLayer(result.stats), "aria-label");
});

test("a genuinely unresolvable date is dropped, not guessed, and the drop is counted", () => {
  const parsed = sandbox.parseMeetingLabel("Ad-hoc Sync, 10:00 AM to 10:30 AM", null, FALLBACK_YEAR);
  assert.equal(parsed.kind, "date-unresolved");

  const result = sandbox.extractEventsFromCandidates(
    [{ label: "Ad-hoc Sync, 10:00 AM to 10:30 AM", columnDateIso: null, layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR },
  );
  assert.equal(result.events.length, 0);
  assert.equal(result.stats.dateUnresolved, 1);
});

test("a structured candidate with a missing/invalid end also drops as date-unresolved", () => {
  const result = sandbox.extractEventsFromCandidates(
    [{ label: "Standup", structuredStart: "2026-08-19T09:00:00", structuredEnd: null, layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR },
  );
  assert.equal(result.events.length, 0);
  assert.equal(result.stats.dateUnresolved, 1);
});

test("zero meeting-shaped labels yields zero events, with dominantLayer null", () => {
  const noise = [
    { label: "Settings", columnDateIso: COLUMN_DATE, layer: "aria-label" },
    { label: "3 of 5 unread", columnDateIso: COLUMN_DATE, layer: "aria-label" },
    { label: "Next week", columnDateIso: COLUMN_DATE, layer: "aria-label" },
  ];
  const result = sandbox.extractEventsFromCandidates(noise, { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 0);
  assert.equal(result.stats.notMeetingShaped, 3);
  assert.equal(sandbox.dominantLayer(result.stats), null);
});
