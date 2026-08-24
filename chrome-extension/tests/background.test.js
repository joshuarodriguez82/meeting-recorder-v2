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
// _calendarDiagnosticProbeFunc IS covered now (it wasn't before): the
// fake DOM's `querySelectorAll` grew compound-selector and
// descendant-combinator support, which was the only thing standing in
// the way. What it needs beyond `document` — `location.href` and `URL`,
// for href redaction — is handed to the vm context in `loadSandbox`.
//
// NOT covered here: the live Outlook Web DOM itself. There is no way to
// sign in to a real tenant from this environment — see the v1.3 header
// comment in background.js. That is exactly why the probe exists.

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
  // `URL` and `location` are page globals the diagnostic probe uses
  // (href redaction). They're browser/Node globals, not part of a bare
  // vm context's intrinsics, so they're handed in explicitly.
  const sandbox = { chrome: chromeStub, console, URL, location: { href: PAGE_HREF } };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: BG_PATH });
  return sandbox;
}

const PAGE_HREF = "https://outlook.office.com/calendar/view/week";

const sandbox = loadSandbox();

// ── Fake DOM ─────────────────────────────────────────────────────────
//
// Deliberately minimal — see the file header for the exact surface
// _calendarDomScanFunc needs.

// Minimal CSS-selector matcher: enough for the selectors
// _calendarDomScanFunc and _calendarDiagnosticProbeFunc actually issue
// — `*`, bare tag names (`iframe`), attribute selectors (`[attr]`,
// `[attr="value"]`), COMPOUND selectors that stack those on one
// element (`a[href]`, `[role="button"][aria-label]`), a single
// DESCENDANT combinator (`[role="gridcell"] [role="button"]`), and
// comma-separated lists of any of those — not a general selector
// engine. Added so the fake DOM can grow a real `querySelectorAll`,
// per the v1.3.2 depth-cap fix: the production scan uses
// `querySelectorAll` instead of a depth-limited `.children` recursion,
// so the test double has to support it rather than the production code
// staying depth-limited just to remain testable against a double that
// couldn't do it. The compound/descendant support is what lets the
// diagnostic probe be tested here too (it was previously untestable
// for exactly this reason — see the file header).
function matchesCompound(node, compound) {
  if (compound === "*") return true;
  let rest = compound;
  const tagMatch = /^[A-Za-z][A-Za-z0-9]*/.exec(compound);
  if (tagMatch) {
    if ((node.tagName || "").toLowerCase() !== tagMatch[0].toLowerCase()) return false;
    rest = compound.slice(tagMatch[0].length);
  }
  const attrRe = /\[([a-zA-Z0-9_-]+)(?:=("|')(.*?)\2)?\]/g;
  let consumed = 0;
  let m;
  while ((m = attrRe.exec(rest)) !== null) {
    consumed = attrRe.lastIndex;
    const name = m[1];
    const value = m[3];
    if (!node.hasAttribute || !node.hasAttribute(name)) return false;
    if (value !== undefined && node.getAttribute(name) !== value) return false;
  }
  // Anything the loop didn't consume is syntax this double doesn't
  // implement — fail loudly-ish (no match) rather than silently
  // matching everything.
  if (consumed !== rest.length) return false;
  return !!tagMatch || consumed > 0;
}

function matchesSimpleSelector(node, rawSel) {
  const parts = rawSel.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return false;
  if (!matchesCompound(node, parts[parts.length - 1])) return false;
  let ancestor = node.parentElement;
  for (let i = parts.length - 2; i >= 0; i--) {
    let matched = false;
    while (ancestor) {
      const cur = ancestor;
      ancestor = ancestor.parentElement;
      if (matchesCompound(cur, parts[i])) { matched = true; break; }
    }
    if (!matched) return false;
  }
  return true;
}

function matchesSelector(node, selector) {
  return selector.split(",").some((s) => matchesSimpleSelector(node, s));
}

function collectMatches(root, selector, out) {
  const kids = root.children || [];
  for (const kid of kids) {
    if (matchesSelector(kid, selector)) out.push(kid);
    collectMatches(kid, selector, out);
  }
}

function attachQuerySelectorAll(node) {
  node.querySelectorAll = (selector) => {
    const out = [];
    collectMatches(node, selector, out);
    return out;
  };
  return node;
}

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
  return attachQuerySelectorAll(node);
}

function append(parent, ...kids) {
  for (const k of kids) {
    k.parentElement = parent;
    parent.children.push(k);
  }
  return parent;
}

function doc(children) {
  return attachQuerySelectorAll({ children });
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

// ── _calendarDomScanFunc: deep-nesting field regression (v1.3.2) ─────
//
// Field evidence (2026-08-14, Outlook Web week view): the diagnostic
// probe's FLAT `querySelectorAll("[aria-label]")` found 28 meeting
// labels that matched the parser's own TIME_RANGE_RE, but the real
// capture — which used this hand-rolled recursive `walk()` — reported
// 0 events from 255 candidates. `walk()` hard-stops at `depth > 30`.
// Outlook Web's React tree nests calendar tiles deeper than that.
// These tests build a synthetic DOM chain deep enough to reproduce
// the truncation and assert a valid, otherwise-perfectly-parseable
// meeting label at that depth is (pre-fix) missed entirely.

// Wraps `leafEl` in `depth - 1` intermediate <div> layers so it sits
// at generation `depth` below the document (a direct child of
// `document` is generation 1).
function buildChain(depth, leafEl) {
  let node = leafEl;
  for (let i = 0; i < depth - 1; i++) {
    const wrapper = el("div");
    append(wrapper, node);
    node = wrapper;
  }
  return node;
}

for (const depth of [35, 50]) {
  test(`a meeting label at nesting depth ${depth} is reachable by the DOM scan`, () => {
    const leaf = el("div", {
      role: "button",
      "aria-label": "Globex, 8:30 AM to 9:00 AM, Friday, August 14, 2026, Microsoft Teams Meeting, By Riley Poe, Busy",
    });
    const chain = buildChain(depth, leaf);
    setDocument(doc([chain]));

    const { candidates, diag } = sandbox._calendarDomScanFunc();
    const found = candidates.find((c) => c.label.startsWith("Globex"));
    assert.ok(
      found,
      `expected the depth-${depth} "Globex" candidate to be found, got ` +
      `${candidates.length} candidate(s); diag=${JSON.stringify(diag)}`,
    );
  });
}

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

test("classifyZeroReason: all candidates not meeting-shaped names that cause specifically (not the old generic catch-all)", () => {
  const stats = { scanned: 14, notMeetingShaped: 14, allDay: 0, dateUnresolved: 0, deduped: 0 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /found 14 candidates, none were meeting-shaped/);
});

test("classifyZeroReason: mixed causes (no single bucket is 100%) reports the stats breakdown, not a single guessed cause", () => {
  const stats = { scanned: 10, notMeetingShaped: 4, dateUnresolved: 3, allDay: 2, deduped: 1 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /found 10 candidates, none produced an event/);
  assert.match(reason, /4 not meeting-shaped/);
  assert.match(reason, /3 unresolved date\/time/);
  assert.match(reason, /2 all-day/);
  assert.match(reason, /1 duplicate/);
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
  const stats = { scanned: 1, notMeetingShaped: 1, allDay: 0, dateUnresolved: 0 };
  const reason = sandbox.classifyZeroReason(stats, {});
  assert.match(reason, /found 1 candidate, none were meeting-shaped/);
});

// ── extractEventsFromCandidates / parseMeetingLabel: regression set ──
// (condensed from the prior scratch harness's fixtures; the shapes come
// from field-reported subjects — pipe, slash, FW: prefix, trailing
// space, all-day, multi-day, nested duplicates — with fictional names.)

const FALLBACK_YEAR = 2026;
const COLUMN_DATE = "2026-08-13";

test("realistic single-day fixture: all 5 real meetings captured (was 1 of 5 pre-v1.2)", () => {
  const dayLabels = [
    "Acme Daily Pulse Call, 10:00 AM to 10:15 AM, Microsoft Teams Meeting, Zoe Doe",
    "PRIORITY: Acme Sales| Active Project Status Reviews and Escalations, 10:00 AM to 10:30 AM, Microsoft Teams Meeting, Casey Roe",
    "FW: Acme Connect - Italy / ACR next steps: weekly team connect, 11:30 AM to 12:00 PM",
    "Acme/CYB - IVA PoC Sync-up, 1:00 PM to 1:30 PM, Microsoft Teams Meeting",
    "AI Transformation Stand Up , 7:30 AM to 8:30 AM",
  ];
  const candidates = dayLabels.map((label) => ({ label, columnDateIso: COLUMN_DATE, layer: "aria-label" }));
  const result = sandbox.extractEventsFromCandidates(candidates, { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 5, JSON.stringify(result.stats));

  const subjects = result.events.map((e) => e.subject).sort();
  assert.ok(subjects.includes("PRIORITY: Acme Sales| Active Project Status Reviews and Escalations"));
  assert.ok(subjects.includes("FW: Acme Connect - Italy / ACR next steps: weekly team connect"));
  assert.ok(subjects.includes("Acme/CYB - IVA PoC Sync-up"));
  assert.ok(subjects.includes("AI Transformation Stand Up"));

  const pulseCall = result.events.find((e) => e.subject === "Acme Daily Pulse Call");
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
  const dupLabel = "Acme Daily Pulse Call, 10:00 AM to 10:15 AM, Microsoft Teams Meeting";
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

// ── calendar-refresh alarm listener (v1.3.1 field report 2026-08-14) ─
//
// Field evidence: the calendar-refresh alarm had never once produced a
// POST. These tests exercise the REAL chrome.alarms.onAlarm listener
// (not a mock of it) against a fresh vm context per test — a fresh
// context stands in for an MV3 service-worker cold start, since it
// carries no in-memory state across "restarts" the way a stale
// module-scope variable would. `chrome.storage.local` is backed by a
// plain object that persists ACROSS that reset, exactly like the real
// durable chrome.storage.local does — this is what proves credentials
// are read fresh from storage rather than from anything that would
// reset on a real cold start.

const CALENDAR_ALARM_NAME = "calendar-refresh"; // must match background.js
const CALENDAR_DEDUP_WINDOW_MS = 20 * 60 * 1000; // must match background.js

function makeStorageLocalStub(initial) {
  const store = { ...(initial || {}) };
  return {
    _store: store,
    get(defaults) {
      const out = { ...(defaults || {}) };
      for (const k of Object.keys(defaults || {})) {
        if (Object.prototype.hasOwnProperty.call(store, k)) out[k] = store[k];
      }
      return Promise.resolve(out);
    },
    set(obj) {
      Object.assign(store, obj);
      return Promise.resolve();
    },
  };
}

// Loads a brand-new evaluation of background.js — a fresh vm context,
// standing in for a cold-started service worker — wired to a storage
// stub seeded with `initialStorage`. Returns the storage stub (so
// tests can inspect what got written) and the captured onAlarm
// listener (so tests can fire it directly, the same way chrome.alarms
// would).
function loadAlarmSandbox(initialStorage) {
  const storage = makeStorageLocalStub(initialStorage);
  let alarmListener = null;
  const chromeStub = {
    runtime: {
      onInstalled: { addListener: () => {} },
      onStartup: { addListener: () => {} },
      onMessage: { addListener: () => {} },
      getManifest: () => ({ version: "1.3.1" }),
    },
    storage: {
      onChanged: { addListener: () => {} },
      local: storage,
    },
    alarms: {
      onAlarm: { addListener: (fn) => { alarmListener = fn; } },
      create: () => {},
      clearAll: async () => {},
    },
    // Deliberately no tabs/scripting methods — none of the tests below
    // need a working capture; the "cold start" test asserts only that
    // the credentials gate is passed, not that a full capture succeeds
    // against a real Outlook Web tab (out of scope here; see the DOM
    // scan tests above for that half).
    tabs: {},
    scripting: {},
  };
  const alarmVmSandbox = { chrome: chromeStub, console };
  vm.createContext(alarmVmSandbox);
  vm.runInContext(SRC, alarmVmSandbox, { filename: BG_PATH });
  if (!alarmListener) {
    throw new Error("chrome.alarms.onAlarm.addListener was never called by background.js");
  }
  return { sandbox: alarmVmSandbox, storage, alarmListener };
}

test("calendar alarm: missing credentials is traced, not silent", async () => {
  const { storage, alarmListener } = loadAlarmSandbox({});
  await alarmListener({ name: CALENDAR_ALARM_NAME });
  assert.ok(storage._store.lastCalendarCaptureAt > 0,
    "a skipped alarm attempt must still stamp lastCalendarCaptureAt");
  assert.equal(storage._store.lastCalendarResult.ok, false);
  assert.equal(storage._store.lastCalendarResult.reason, "not-configured");
});

test("calendar alarm: dedupe skip is traced, not silent", async () => {
  const now = Date.now();
  const { storage, alarmListener } = loadAlarmSandbox({
    backendUrl: "http://127.0.0.1:17645",
    token: "tok",
    lastCalendarCaptureAt: now - 5 * 60 * 1000, // 5 min ago, inside the 20-min window
  });
  await alarmListener({ name: CALENDAR_ALARM_NAME });
  assert.equal(storage._store.lastCalendarResult.ok, false);
  assert.equal(storage._store.lastCalendarResult.reason, "deduped");
  assert.ok(storage._store.lastCalendarCaptureAt >= now,
    "the skip itself should still be timestamped as an attempt");
});

test("calendar alarm: an alarm fired long enough after the last capture is NOT deduped", async () => {
  const now = Date.now();
  const { storage, alarmListener } = loadAlarmSandbox({
    backendUrl: "http://127.0.0.1:17645",
    token: "tok",
    lastCalendarCaptureAt: now - (CALENDAR_DEDUP_WINDOW_MS + 60_000), // just past the window
  });
  await alarmListener({ name: CALENDAR_ALARM_NAME });
  assert.notEqual(storage._store.lastCalendarResult.reason, "deduped");
  assert.notEqual(storage._store.lastCalendarResult.reason, "not-configured");
});

test("calendar alarm: cold start reads backendUrl/token fresh from chrome.storage.local, not a stale module var", async () => {
  // The "prime suspect" from the field-report investigation: a module-
  // scope variable populated once at worker startup would read
  // undefined here, since this vm context has never run any startup
  // code that could have populated one. background.js instead reads
  // chrome.storage.local directly inside the listener, which is
  // exactly what's seeded below — simulating durable config surviving
  // a cold start. If background.js regresses to a module-scope cache,
  // this test starts failing with reason "not-configured".
  const { storage, alarmListener } = loadAlarmSandbox({
    backendUrl: "http://127.0.0.1:17645",
    token: "tok",
    lastCalendarCaptureAt: 0, // never captured before -> must not dedupe-skip either
  });
  await alarmListener({ name: CALENDAR_ALARM_NAME });
  assert.notEqual(storage._store.lastCalendarResult.reason, "not-configured");
  assert.notEqual(storage._store.lastCalendarResult.reason, "deduped");
  // The fake chrome has no working tabs/scripting, so the capture
  // itself fails further in (proof it got PAST the credentials gate) —
  // and that failure is still fully traced, never silent.
  assert.equal(storage._store.lastCalendarResult.ok, false);
  assert.ok(storage._store.lastCalendarCaptureAt > 0);
});

test("captureCalendarOnly persists a traceable result even when called directly without credentials", async () => {
  const { sandbox: sb, storage } = loadAlarmSandbox({});
  const result = await sb.captureCalendarOnly("", "");
  assert.equal(result.ok, false);
  assert.ok(storage._store.lastCalendarCaptureAt > 0);
  assert.equal(storage._store.lastCalendarResult.ok, false);
});

// ── FIELD REGRESSION: date AFTER the time range, no column date ──────
// Every fixture above supplies a columnDateIso, which is what let the
// suite stay green while real capture returned zero. On a live Outlook
// Web week view there are no role="columnheader" elements to resolve a
// column date from, and the date sits AFTER the time range in the
// label — a position TIME_RANGE_RE never looks at. The field
// diagnostic reported "47 unresolved date/time" against labels that
// each carried a fully-qualified date in their own text.
// These labels reproduce that report's exact shapes (date after the
// time range, comma-and-bracket organiser names, pipes and slashes in
// subjects) with the names and clients replaced by fictional ones.
const FIELD_LABELS = [
  "Globex, 8:30 AM to 9:00 AM, Friday, August 14, 2026, Microsoft Teams Meeting, By Riley Poe, Busy",
  "PRIORITY: Acme Sales| Active Project Status Reviews and Escalations, 10:00 AM to 10:30 AM, Friday, August 14, 2026, Microsoft Teams Meeting, By Casey Roe, Busy, Exception to recurring event",
  "Discuss the Northwind collection script concerns and develop a path forward, 10:30 AM to 11:00 AM, Friday, August 14, 2026, Microsoft Teams Meeting, By Roe, Pat Jr. [US-US], Busy",
  "Hooli/Acme <> Northwind | workforce management and quality management, 1:30 PM to 2:25 PM, Wednesday, August 12, 2026, By Devon Poe, Busy",
  "Acme Daily Pulse Call , 9:30 AM to 9:45 AM, Friday, August 14, 2026, Microsoft Teams Meeting, By Zoë Døe, Busy, Recurring event",
  "Q3 Quarterly Management Meeting, 10:00 AM to 11:30 AM, Wednesday, August 12, 2026, By Northwind Evite, Tentative",
  "Umbrella/Acme/Northwind Sync, 9:30 AM to 10:30 AM, Thursday, August 13, 2026, By Noh, Kim, Busy",
];

test("FIELD: date after the time range resolves with NO column date", () => {
  const candidates = FIELD_LABELS.map((label) => ({
    label, columnDateIso: null, layer: "aria-label",
  }));
  const result = sandbox.extractEventsFromCandidates(
    candidates, { fallbackYear: FALLBACK_YEAR });

  assert.equal(result.events.length, FIELD_LABELS.length,
    `expected every field label to produce an event; stats=${JSON.stringify(result.stats)}`);
  assert.equal(result.stats.dateUnresolved, 0);

  const home = result.events.find((e) => e.subject === "Globex");
  assert.ok(home, "Globex missing");
  assert.equal(home.start, "2026-08-14T08:30:00");
  assert.equal(home.end, "2026-08-14T09:00:00");

  // Each event must land on ITS OWN date, not all collapsed onto one.
  const acme = result.events.find((e) => e.subject.startsWith("Hooli/Acme"));
  assert.equal(acme.start, "2026-08-12T13:30:00");
  const umbrella = result.events.find((e) => e.subject === "Umbrella/Acme/Northwind Sync");
  assert.equal(umbrella.start, "2026-08-13T09:30:00");
});

test("FIELD: a month name in the SUBJECT loses to the real date after the time", () => {
  const parsed = sandbox.parseMeetingLabel(
    "August Planning Review, 9:00 AM to 10:00 AM, Monday, August 10, 2026, Busy",
    null, FALLBACK_YEAR);
  assert.equal(parsed.kind, "event");
  assert.equal(parsed.startIso, "2026-08-10T09:00:00");
});

test("FIELD: an explicit inline date still outranks the trailing one", () => {
  const parsed = sandbox.parseMeetingLabel(
    "Offsite, August 17, 9:00 AM to August 19, 5:00 PM, Monday, August 10, 2026",
    null, FALLBACK_YEAR);
  assert.equal(parsed.kind, "event");
  assert.equal(parsed.startIso, "2026-08-17T09:00:00");
  assert.equal(parsed.endIso, "2026-08-19T17:00:00");
});

test("FIELD: no date anywhere and no column date is still unresolved", () => {
  const parsed = sandbox.parseMeetingLabel(
    "Mystery Meeting, 9:00 AM to 10:00 AM", null, FALLBACK_YEAR);
  assert.equal(parsed.kind, "date-unresolved");
});

// ── extractOrganizerFromLabel: the "By <name>" tail segment ──────────
//
// `organizer` was declared on every captured event and never assigned —
// always "". It is, however, already in the string being parsed: Outlook
// Web writes "By <name>" into the tail after the time range, the same
// tail the date resolver reads. These fixtures cover the variations the
// field labels above actually exhibit (names containing commas, a
// bracketed region suffix, diacritics, several different trailing
// status words) plus the shapes that must yield NOTHING rather than a
// wrong name.

test("organizer: the plain 'By <First Last>' form", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, " +
      "Microsoft Teams Meeting, By Jane Doe, Busy"),
    "Jane Doe");
});

test("organizer: no 'By' segment at all yields empty, not a guess", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Acme/CYB - IVA PoC Sync-up, 1:00 PM to 1:30 PM, Friday, August 14, 2026, " +
      "Microsoft Teams Meeting"),
    "");
});

test("organizer: 'Last, First Suffix [REGION]' keeps its comma, suffix and bracket", () => {
  // The comma inside the name is the whole trap — splitting the tail on
  // comma and taking the first piece would report "Roe". Same reason
  // owner_service.split_owners refuses to split on comma (see
  // test_owner_service.py::test_comma_is_not_split).
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Discuss the Northwind collection script, 10:30 AM to 11:00 AM, " +
      "Friday, August 14, 2026, Microsoft Teams Meeting, By Roe, Pat Jr. [US-US], Busy"),
    "Roe, Pat Jr. [US-US]");
});

test("organizer: bare 'Last, First' keeps both halves", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Umbrella/Acme/Northwind Sync, 9:30 AM to 10:30 AM, " +
      "Thursday, August 13, 2026, By Noh, Kim, Busy"),
    "Noh, Kim");
});

test("organizer: non-ASCII diacritics survive intact", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Acme Daily Pulse Call , 9:30 AM to 9:45 AM, Friday, August 14, 2026, " +
      "Microsoft Teams Meeting, By Zoë Døe, Busy, Recurring event"),
    "Zoë Døe");
});

test("organizer: every trailing status word is stripped, never absorbed into the name", () => {
  const cases = [
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Casey Roe, Busy", "Casey Roe"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Casey Roe, Free", "Casey Roe"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Casey Roe, Tentative", "Casey Roe"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Casey Roe, Out of office", "Casey Roe"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Casey Roe, Busy, Recurring event", "Casey Roe"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Casey Roe, Busy, Exception to recurring event", "Casey Roe"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Casey Roe, Free, Canceled", "Casey Roe"],
    // …and with a comma-carrying name in front of the same statuses.
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Roe, Pat Jr. [US-US], Busy, Recurring event", "Roe, Pat Jr. [US-US]"],
  ];
  for (const [label, expected] of cases) {
    assert.equal(sandbox.extractOrganizerFromLabel(label), expected, label);
  }
});

test("organizer: a 'Canceled:' SUBJECT prefix never reaches the name", () => {
  // The prefix sits before the time range, i.e. in the subject, which
  // this function never looks at.
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Canceled: Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, " +
      "By Jane Doe, Free, Canceled"),
    "Jane Doe");
});

test("organizer: a status word immediately after 'By' yields empty, not a status as a name", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Blocked, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By, Busy"),
    "");
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Blocked, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Busy"),
    "");
});

test("organizer: the word 'by' inside a SUBJECT is not read as an organizer", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Design Review by the numbers, 9:00 AM to 10:00 AM, Monday, August 10, 2026, Busy"),
    "");
});

test("organizer: junk input is empty, never a throw", () => {
  assert.equal(sandbox.extractOrganizerFromLabel(null), "");
  assert.equal(sandbox.extractOrganizerFromLabel(""), "");
  assert.equal(sandbox.extractOrganizerFromLabel(12345), "");
  assert.equal(sandbox.extractOrganizerFromLabel({}), "");
  // A runaway "name" (no recognizable status terminator, absurd length)
  // reports nothing rather than a sentence.
  const runaway = "X, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By " + "z".repeat(200);
  assert.equal(sandbox.extractOrganizerFromLabel(runaway), "");
});

test("organizer: a structured-time candidate (no text range in the label) still resolves", () => {
  // parseStructuredCandidate's labels carry no time range at all, so the
  // tail lookup falls back to the whole label — with the stricter
  // comma-anchored marker, which is why the subject-only case above
  // still yields nothing.
  assert.equal(
    sandbox.extractOrganizerFromLabel("Budget Review, By Devon Poe, Busy"),
    "Devon Poe");
});

test("organizer: flows end-to-end through extractEventsFromCandidates for every field label", () => {
  const candidates = FIELD_LABELS.map((label) => ({
    label, columnDateIso: null, layer: "aria-label",
  }));
  const result = sandbox.extractEventsFromCandidates(
    candidates, { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, FIELD_LABELS.length);

  const bySubject = Object.fromEntries(result.events.map((e) => [e.subject, e]));
  assert.equal(bySubject["Globex"].organizer, "Riley Poe");
  assert.equal(
    bySubject["Discuss the Northwind collection script concerns and develop a path forward"].organizer,
    "Roe, Pat Jr. [US-US]");
  assert.equal(bySubject["Acme Daily Pulse Call"].organizer, "Zoë Døe");
  assert.equal(bySubject["Umbrella/Acme/Northwind Sync"].organizer, "Noh, Kim");
  assert.equal(bySubject["Q3 Quarterly Management Meeting"].organizer, "Northwind Evite");

  // And the fields that were already correct are untouched.
  assert.equal(bySubject["Globex"].start, "2026-08-14T08:30:00");
  assert.equal(bySubject["Globex"].join_url, "");
});

test("organizer: a label with no 'By' segment leaves the event exactly as it was (empty organizer)", () => {
  const result = sandbox.extractEventsFromCandidates(
    [{ label: "Acme/CYB - IVA PoC Sync-up, 1:00 PM to 1:30 PM, Friday, August 14, 2026",
       columnDateIso: null, layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].organizer, "");
  assert.equal(result.events[0].start, "2026-08-14T13:00:00");
});

test("organizer: stats count how many kept events actually carry each field", () => {
  const result = sandbox.extractEventsFromCandidates([
    { label: "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By Jane Doe, Busy",
      columnDateIso: null, layer: "aria-label" },
    { label: "Ad-hoc sync, 1:00 PM to 1:30 PM, Friday, August 14, 2026",
      columnDateIso: null, layer: "aria-label" },
  ], { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.stats.parsed, 2);
  assert.equal(result.stats.withOrganizer, 1);
  // Zero, and REPORTED as zero — the popup says "no join links (not
  // exposed)" off this number rather than leaving an empty field
  // indistinguishable from a meeting that has no link.
  assert.equal(result.stats.withJoinUrl, 0);
});

test("organizer: a DOM-supplied organizer still wins over the label-derived one", () => {
  const result = sandbox.extractEventsFromCandidates(
    [{ label: "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By Jane Doe, Busy",
       organizer: "Preset Person", columnDateIso: null, layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events[0].organizer, "Preset Person");
});

test("organizer: survives the whole DOM-scan -> extract path", () => {
  const tile = el("div", {
    role: "button",
    "aria-label": "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, " +
      "Microsoft Teams Meeting, By Jane Doe, Busy",
  });
  setDocument(doc([tile]));
  const { candidates } = sandbox._calendarDomScanFunc();
  const result = sandbox.extractEventsFromCandidates(candidates, { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].organizer, "Jane Doe");
});

// ── Join-link probe (_calendarDiagnosticProbeFunc) ───────────────────
//
// Read-only, diagnostic only. It exists to ANSWER the question "is a
// join link reachable from the DOM the capture already walks?" rather
// than to assume either answer — see JOIN_URL_PROBE_CONFIG in
// background.js. Nothing here populates `join_url`; these tests pin
// the two things that make the answer trustworthy: association is by
// DOM containment/adjacency (never position), and an example may only
// ever show a redacted host+path SHAPE, because a join URL is a
// single-use meeting credential and the report gets pasted into chat.

const TIME_HINT_SOURCE = "\\d{1,2}(:\\d{2})?\\s*[AaPp]?\\.?[Mm]?\\b|\\d{1,2}:\\d{2}\\b";
const MEETING_LABEL =
  "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By Jane Doe, Busy";
// Shaped like a real Teams invite link; the opaque segment stands in
// for the credential that must never be echoed back.
const TEAMS_HREF =
  "https://teams.microsoft.com/l/meetup-join/19%3ameeting_SECRETTOKEN123%40thread.v2/0" +
  "?context=%7b%22Tid%22%3a%22tenant%22%7d";

// background.js's top-level `const`s live in the context's global
// LEXICAL scope, not on its global object, so they aren't reachable as
// `sandbox.X` the way its `function` declarations are — read them by
// evaluating an expression inside the same context instead. The real
// extension passes these exact two values as executeScript args.
const PROBE_ARGS = vm.runInContext(
  "({ timeRangeSource: TIME_RANGE_RE.source, joinConfig: JOIN_URL_PROBE_CONFIG })",
  sandbox);

function runProbe() {
  return sandbox._calendarDiagnosticProbeFunc(
    PROBE_ARGS.timeRangeSource, TIME_HINT_SOURCE, PROBE_ARGS.joinConfig);
}

test("join probe: a join link INSIDE a meeting-labelled element is associated by containment", () => {
  const link = el("a", { href: TEAMS_HREF });
  const tile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  append(tile, link);
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.matchCount, 1);
  assert.equal(joinLinks.byProvider.teams, 1);
  assert.equal(joinLinks.insideMeetingLabelledElement, 1);
  assert.equal(joinLinks.adjacentToMeetingLabelledElement, 0);
  assert.equal(joinLinks.unassociated, 0);
  assert.match(joinLinks.verdict, /join_url IS fillable/);
});

test("join probe: a join link NEXT TO a meeting-labelled element is associated by adjacency, not containment", () => {
  const link = el("a", { href: TEAMS_HREF });
  const tile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  const wrapper = el("div");
  append(wrapper, tile, link);
  setDocument(doc([wrapper]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.matchCount, 1);
  assert.equal(joinLinks.insideMeetingLabelledElement, 0);
  assert.equal(joinLinks.adjacentToMeetingLabelledElement, 1);
  assert.equal(joinLinks.unassociated, 0);
});

test("join probe: a join link with no meeting-shaped label anywhere near it is UNASSOCIATED, never matched by position", () => {
  const link = el("a", { href: TEAMS_HREF });
  const footer = el("div");
  append(footer, link);
  // A real meeting tile exists, but nowhere near the link in the tree.
  const farTile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  const farBranch = el("div");
  append(farBranch, farTile);
  const page = el("div");
  append(page, farBranch, el("div"), el("div"));
  setDocument(doc([page, footer]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.matchCount, 1);
  assert.equal(joinLinks.insideMeetingLabelledElement, 0);
  assert.equal(joinLinks.adjacentToMeetingLabelledElement, 0);
  assert.equal(joinLinks.unassociated, 1);
  assert.match(joinLinks.verdict, /would have to be positional/);
});

test("join probe: ordinary calendar chrome links are not counted as join links", () => {
  const nav = el("a", { href: "https://outlook.office.com/calendar/view/day" });
  const help = el("a", { href: "https://support.microsoft.com/outlook" });
  const tile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  append(tile, nav, help);
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.anchorCount, 2);
  assert.equal(joinLinks.matchCount, 0);
  assert.equal(joinLinks.redactedExamples.length, 0);
  assert.match(joinLinks.verdict, /no join-shaped links anywhere/);
});

test("join probe: Zoom / Webex / Google Meet links are recognized alongside Teams", () => {
  const tile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  append(tile,
    el("a", { href: "https://zoom.us/j/9876543210?pwd=SECRET" }),
    el("a", { href: "https://acme.webex.com/meet/j.doe" }),
    el("a", { href: "https://meet.google.com/abc-defg-hij" }));
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.matchCount, 3);
  assert.equal(joinLinks.byProvider.zoom, 1);
  assert.equal(joinLinks.byProvider.webex, 1);
  assert.equal(joinLinks.byProvider.meet, 1);
  assert.equal(joinLinks.insideMeetingLabelledElement, 3);
});

test("join probe: examples show the URL SHAPE only — never the credential, the query string, or the customer's host", () => {
  const tile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  append(tile,
    el("a", { href: TEAMS_HREF }),
    el("a", { href: "https://acme.webex.com/meet/j.doe" }),
    el("a", { href: "https://zoom.us/my/janedoe" }));
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  // Array.from: the probe's return value is built inside the vm
  // context, so its arrays carry that realm's Array prototype and
  // deepStrictEqual would fail on the prototype alone.
  const shapes = Array.from(joinLinks.redactedExamples, (e) => e.shape);
  assert.deepEqual(shapes, [
    "teams.microsoft.com/l/meetup-join/…/0?…",
    // The Webex SITE subdomain is usually the customer's name — elided.
    "*.webex.com/meet/…",
    "zoom.us/my/…",
  ]);
  const blob = JSON.stringify(joinLinks);
  for (const secret of ["SECRETTOKEN123", "context=", "janedoe", "j.doe", "acme.webex.com"]) {
    assert.ok(!blob.includes(secret), `redacted output leaked ${secret}: ${blob}`);
  }
});

test("join probe: join links inside iframes and shadow roots are counted too", () => {
  const innerLink = el("a", { href: TEAMS_HREF });
  const innerTile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  append(innerTile, innerLink);
  const iframeEl = el("iframe", {}, { contentDocument: doc([innerTile]) });

  const shadowLink = el("a", { href: "https://zoom.us/j/1234567890" });
  const shadowTile = el("div", { role: "button", "aria-label": MEETING_LABEL });
  append(shadowTile, shadowLink);
  const hostEl = el("div", {}, { shadowRoot: doc([shadowTile]) });

  setDocument(doc([iframeEl, hostEl]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.matchCount, 2);
  assert.equal(joinLinks.insideMeetingLabelledElement, 2);
});

test("join probe: a page with no anchors at all reports zero, not an error", () => {
  setDocument(doc([el("div", { role: "button", "aria-label": MEETING_LABEL })]));
  const { joinLinks } = runProbe();
  assert.equal(joinLinks.anchorCount, 0);
  assert.equal(joinLinks.matchCount, 0);
  assert.ok(!joinLinks.error);
});

// ── extractUrlsFromLabel: join_url out of the same label tail (v1.5) ──
//
// The v1.4 probe searched for join-shaped ANCHOR elements, found none,
// and reported that join_url "cannot be filled from this DOM". Real
// capture output disproved it in the same report: the URL was sitting
// in the aria-label TEXT, in the Location position between the date and
// the "By <organiser>" segment. These fixtures pin both halves of the
// fix — that a recognised conferencing URL there fills `join_url`, and
// that any OTHER URL there is a LOCATION and never becomes one.
//
// Every URL below is synthetic. A join URL is a single-use meeting
// credential; no real one goes in this repo, and none is ever logged.

const ZOOM_LABEL_URL = "https://zoom.us/j/0000000000?pwd=EXAMPLEPWDVALUE&from=addon";
const TEAMS_LABEL_URL =
  "https://teams.microsoft.com/l/meetup-join/19%3ameeting_EXAMPLEID%40thread.v2/0";
const WEBEX_LABEL_URL = "https://globex.webex.com/meet/pat.roe";
const MEET_LABEL_URL = "https://meet.google.com/aaa-bbbb-ccc";
// A link to a training library — the shape that turned up in the same
// Location position as the Zoom link. It is where the meeting is about
// something, not a meeting to join.
const INCIDENTAL_LABEL_URL = "https://learning.example.com/library/course-42";

function labelWithUrl(url, subject = "Onboarding call") {
  return `${subject}, 9:00 AM to 9:30 AM, Friday, August 14, 2026, ` +
    `${url}, By Jane Doe, Busy`;
}

test("join_url: a Zoom link in the Location position (query string and all) is extracted", () => {
  const got = sandbox.extractUrlsFromLabel(labelWithUrl(ZOOM_LABEL_URL));
  assert.equal(got.joinUrl, ZOOM_LABEL_URL);
  assert.equal(got.joinProvider, "zoom");
  assert.equal(got.locationUrl, "");
});

test("join_url: Teams meetup-join, Webex and Google Meet links are recognized too", () => {
  const cases = [
    [TEAMS_LABEL_URL, "teams"],
    [WEBEX_LABEL_URL, "webex"],
    [MEET_LABEL_URL, "meet"],
  ];
  for (const [url, provider] of cases) {
    const got = sandbox.extractUrlsFromLabel(labelWithUrl(url));
    assert.equal(got.joinUrl, url, url);
    assert.equal(got.joinProvider, provider, url);
  }
});

test("join_url: an INCIDENTAL Location URL is a location, never a join link", () => {
  const got = sandbox.extractUrlsFromLabel(labelWithUrl(INCIDENTAL_LABEL_URL));
  assert.equal(got.joinUrl, "");
  assert.equal(got.joinProvider, "");
  assert.equal(got.locationUrl, INCIDENTAL_LABEL_URL);
});

test("join_url: a conferencing host on a NON-join path is not a join link either", () => {
  // Host alone is not enough — https://zoom.us/pricing joins nothing.
  const got = sandbox.extractUrlsFromLabel(labelWithUrl("https://zoom.us/pricing"));
  assert.equal(got.joinUrl, "");
  assert.equal(got.locationUrl, "https://zoom.us/pricing");
});

test("join_url: a label carrying BOTH keeps them in separate fields", () => {
  const label =
    "Workshop, 9:00 AM to 10:00 AM, Friday, August 14, 2026, " +
    `${INCIDENTAL_LABEL_URL}, ${ZOOM_LABEL_URL}, By Jane Doe, Busy`;
  const got = sandbox.extractUrlsFromLabel(label);
  assert.equal(got.joinUrl, ZOOM_LABEL_URL);
  assert.equal(got.locationUrl, INCIDENTAL_LABEL_URL);
});

test("join_url: the trailing ', By <organiser>' is never swallowed into the URL", () => {
  const label = labelWithUrl(ZOOM_LABEL_URL);
  const got = sandbox.extractUrlsFromLabel(label);
  assert.ok(!got.joinUrl.includes("By"), got.joinUrl);
  assert.ok(!got.joinUrl.includes(","), got.joinUrl);
  // ...and the organiser is still read out of the same tail.
  assert.equal(sandbox.extractOrganizerFromLabel(label), "Jane Doe");
});

test("join_url: junk input yields empty fields, never a throw", () => {
  for (const bad of [null, "", 12345, {}, "Standup, 9:00 AM to 9:30 AM"]) {
    const got = sandbox.extractUrlsFromLabel(bad);
    assert.equal(got.joinUrl, "");
    assert.equal(got.locationUrl, "");
  }
});

test("join_url: a label with NO url produces a byte-identical event", () => {
  const label =
    "Acme/CYB - IVA PoC Sync-up, 1:00 PM to 1:30 PM, Friday, August 14, 2026, " +
    "Microsoft Teams Meeting, By Jane Doe, Busy";
  const result = sandbox.extractEventsFromCandidates(
    [{ label, columnDateIso: null, layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 1);
  // Every field, spelled out — this is the "a failed extraction leaves
  // the event exactly as it was" guarantee, not a spot check.
  assert.deepEqual({ ...result.events[0] }, {
    subject: "Acme/CYB - IVA PoC Sync-up",
    start: "2026-08-14T13:00:00",
    end: "2026-08-14T13:30:00",
    location: "",
    organizer: "Jane Doe",
    join_url: "",
    // Always present from v1.7, always "" out of the LABEL — the grid
    // has never carried a description. It is filled, when it can be,
    // by mergeDetailIntoEvents from Outlook's own responses, which is
    // a separate step this assertion deliberately does not involve.
    body: "",
  });
  assert.equal(result.stats.withJoinUrl, 0);
  assert.equal(result.stats.withLocationUrl, 0);
});

test("join_url: flows end-to-end through extractEventsFromCandidates, per provider", () => {
  const result = sandbox.extractEventsFromCandidates([
    { label: labelWithUrl(ZOOM_LABEL_URL, "Zoom hosted review"), layer: "aria-label" },
    { label: labelWithUrl(TEAMS_LABEL_URL, "Teams hosted review"), layer: "aria-label" },
    { label: labelWithUrl(INCIDENTAL_LABEL_URL, "Training block"), layer: "aria-label" },
    { label: labelWithUrl("", "Plain meeting").replace(", ,", ","), layer: "aria-label" },
  ], { fallbackYear: FALLBACK_YEAR });

  const bySubject = Object.fromEntries(result.events.map((e) => [e.subject, e]));
  assert.equal(bySubject["Zoom hosted review"].join_url, ZOOM_LABEL_URL);
  assert.equal(bySubject["Zoom hosted review"].location, "");
  assert.equal(bySubject["Teams hosted review"].join_url, TEAMS_LABEL_URL);
  // The training link is a LOCATION. Nothing offers it as a way to join.
  assert.equal(bySubject["Training block"].join_url, "");
  assert.equal(bySubject["Training block"].location, INCIDENTAL_LABEL_URL);
  assert.equal(bySubject["Plain meeting"].join_url, "");
  assert.equal(bySubject["Plain meeting"].location, "");

  assert.equal(result.stats.withJoinUrl, 2);
  assert.equal(result.stats.withLocationUrl, 1);
  assert.equal(result.stats.joinUrlByProvider.zoom, 1);
  assert.equal(result.stats.joinUrlByProvider.teams, 1);
});

test("join_url: the stats counters classify and count only — no URL is ever in them", () => {
  const result = sandbox.extractEventsFromCandidates([
    { label: labelWithUrl(ZOOM_LABEL_URL, "Zoom hosted review"), layer: "aria-label" },
    { label: labelWithUrl(INCIDENTAL_LABEL_URL, "Training block"), layer: "aria-label" },
  ], { fallbackYear: FALLBACK_YEAR });
  const blob = JSON.stringify(result.stats);
  for (const fragment of ["http", "zoom.us", "EXAMPLEPWDVALUE", "pwd=", "learning.example.com"]) {
    assert.ok(!blob.includes(fragment), `stats leaked ${fragment}: ${blob}`);
  }
});

test("join_url: a DOM-supplied joinUrl still wins over the label-derived one", () => {
  const result = sandbox.extractEventsFromCandidates(
    [{ label: labelWithUrl(ZOOM_LABEL_URL), joinUrl: MEET_LABEL_URL, layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events[0].join_url, MEET_LABEL_URL);
  assert.equal(result.stats.joinUrlByProvider.meet, 1);
});

test("join_url: survives the whole DOM-scan -> extract path", () => {
  const tile = el("div", { role: "button", "aria-label": labelWithUrl(ZOOM_LABEL_URL) });
  setDocument(doc([tile]));
  const { candidates } = sandbox._calendarDomScanFunc();
  const result = sandbox.extractEventsFromCandidates(candidates, { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events.length, 1);
  assert.equal(result.events[0].join_url, ZOOM_LABEL_URL);
  assert.equal(result.events[0].organizer, "Jane Doe");
  assert.equal(result.events[0].start, "2026-08-14T09:00:00");
});

test("join_url: extracting a URL disturbs neither the date resolution nor the subject", () => {
  // The URL sits between the date segment and the organiser — the same
  // tail both of those parse. Every one of the field labels still lands
  // on its own date with its own subject once a URL is inserted there.
  const withUrls = FIELD_LABELS.map((label) =>
    label.replace(", By ", `, ${ZOOM_LABEL_URL}, By `));
  const plain = sandbox.extractEventsFromCandidates(
    FIELD_LABELS.map((label) => ({ label, columnDateIso: null, layer: "aria-label" })),
    { fallbackYear: FALLBACK_YEAR });
  const urled = sandbox.extractEventsFromCandidates(
    withUrls.map((label) => ({ label, columnDateIso: null, layer: "aria-label" })),
    { fallbackYear: FALLBACK_YEAR });

  assert.equal(urled.events.length, plain.events.length);
  for (let i = 0; i < plain.events.length; i++) {
    assert.equal(urled.events[i].subject, plain.events[i].subject);
    assert.equal(urled.events[i].start, plain.events[i].start);
    assert.equal(urled.events[i].end, plain.events[i].end);
    assert.equal(urled.events[i].organizer, plain.events[i].organizer);
    assert.equal(urled.events[i].join_url, ZOOM_LABEL_URL);
  }
});

// ── organizer: the shapes real capture output actually contains ──────

test("organizer: an SMTP address where a display name should be is kept as an address", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, " +
      "By a.doe@globex.example, Busy"),
    "a.doe@globex.example");
});

test("organizer: an address is never glued to the segment after it", () => {
  // The pre-v1.5 rule joined ANY bare single token to the next
  // non-status segment (the "Last, First" form). An address is a bare
  // single token, so it would have come out as
  // "a.doe@globex.example, Umbrella HQ Room 3" — an address that is no
  // longer an address, which resolves to nobody.
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, " +
      "By a.doe@globex.example, Umbrella HQ Room 3, Busy"),
    "a.doe@globex.example");
});

test("organizer: an address is passed through with its own casing, unaltered", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By A.Doe@globex.example, Busy"),
    "A.Doe@globex.example");
  // A mailto: prefix, if the tenant ever writes one, leaves the address.
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By mailto:a.doe@globex.example, Busy"),
    "a.doe@globex.example");
});

test("organizer: an email organiser flows through to the event and is counted as one", () => {
  const result = sandbox.extractEventsFromCandidates(
    [{ label: "Globex sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, " +
        "By a.doe@globex.example, Busy", layer: "aria-label" }],
    { fallbackYear: FALLBACK_YEAR });
  assert.equal(result.events[0].organizer, "a.doe@globex.example");
  assert.equal(result.stats.withOrganizer, 1);
  assert.equal(result.stats.withOrganizerEmail, 1);
});

test("organizer: a double space inside a name is collapsed, not preserved", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By Jane  Doe, Busy"),
    "Jane Doe");
  // ...including in the surname-first form, on either side of the comma.
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By Noh,  Kim  Lee, Busy"),
    "Noh, Kim Lee");
});

test("organizer: a distribution list or room name is read as written", () => {
  const cases = [
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Northwind Evite, Tentative",
      "Northwind Evite"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Umbrella HQ Room 3, Busy",
      "Umbrella HQ Room 3"],
    ["…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Initech All Hands DL, Busy",
      "Initech All Hands DL"],
  ];
  for (const [label, expected] of cases) {
    assert.equal(sandbox.extractOrganizerFromLabel(label), expected, label);
  }
});

test("organizer: a single-token list name is not glued to a room that follows it", () => {
  // "all-hands-dl" is a bare token like a surname, but "Umbrella HQ
  // Room 3" is a place, not a given name — the digit in it is the tell.
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By all-hands-dl, Umbrella HQ Room 3, Busy"),
    "all-hands-dl");
  // The genuine "Last, First" case is unaffected — that IS a given name.
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "…, 9:00 AM to 9:30 AM, Monday, August 10, 2026, By Noh, Kim, Busy"),
    "Noh, Kim");
});

test("organizer: non-ASCII survives the surname-first form too", () => {
  assert.equal(
    sandbox.extractOrganizerFromLabel(
      "Sync, 8:30 AM to 9:00 AM, Friday, August 14, 2026, By Døe, Zoë, Busy"),
    "Døe, Zoë");
});

// ── Join-link probe: label TEXT, and a verdict that says where it looked

test("join probe: a join URL in aria-label TEXT is found even with zero anchors", () => {
  const tile = el("div", {
    role: "button",
    "aria-label": labelWithUrl(ZOOM_LABEL_URL),
  });
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.anchorCount, 0);
  assert.equal(joinLinks.matchCount, 0);
  assert.equal(joinLinks.labelJoinCount, 1);
  assert.equal(joinLinks.labelJoinInMeetingShapedLabel, 1);
  assert.equal(joinLinks.labelByProvider.zoom, 1);
  // The v1.4 verdict for exactly this DOM was "no join-shaped links
  // anywhere ... join_url cannot be filled from this DOM".
  assert.match(joinLinks.verdict, /join_url IS fillable from the label/);
  assert.ok(!/cannot be filled/.test(joinLinks.verdict), joinLinks.verdict);
});

test("join probe: a label-text example is redacted to a SHAPE, like an anchor one", () => {
  const tile = el("div", { role: "button", "aria-label": labelWithUrl(ZOOM_LABEL_URL) });
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  const shapes = Array.from(joinLinks.labelRedactedExamples, (e) => e.shape);
  assert.deepEqual(shapes, ["zoom.us/j/…?…"]);
  const blob = JSON.stringify(joinLinks);
  for (const secret of ["EXAMPLEPWDVALUE", "pwd=", "0000000000", "from=addon"]) {
    assert.ok(!blob.includes(secret), `probe leaked ${secret}: ${blob}`);
  }
});

test("join probe: a non-conferencing URL in a label is counted as a location, not a join link", () => {
  const tile = el("div", {
    role: "button",
    "aria-label": labelWithUrl(INCIDENTAL_LABEL_URL),
  });
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.labelUrlCount, 1);
  assert.equal(joinLinks.labelJoinCount, 0);
  assert.equal(joinLinks.labelNonJoinUrlCount, 1);
  // Genuinely absent — and the verdict says BOTH places were checked
  // and reports the non-conferencing URL it did see, rather than
  // implying the page had nothing in it at all.
  assert.match(joinLinks.verdict, /BOTH places checked/);
  assert.match(joinLinks.verdict, /1 URL\(s\) in aria-label text were all non-conferencing/);
});

test("join probe: genuinely absent says so, and names what it looked at", () => {
  setDocument(doc([el("div", { role: "button", "aria-label": MEETING_LABEL })]));
  const { joinLinks } = runProbe();
  assert.equal(joinLinks.labelUrlCount, 0);
  assert.match(joinLinks.verdict, /no join-shaped links anywhere/);
  assert.match(joinLinks.verdict, /BOTH places checked/);
  assert.match(joinLinks.verdict, /genuinely absent/);
});

test("join probe: anchors and label text are reported separately when both are present", () => {
  const tile = el("div", { role: "button", "aria-label": labelWithUrl(ZOOM_LABEL_URL) });
  append(tile, el("a", { href: TEAMS_HREF }));
  setDocument(doc([tile]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.matchCount, 1);
  assert.equal(joinLinks.insideMeetingLabelledElement, 1);
  assert.equal(joinLinks.labelJoinInMeetingShapedLabel, 1);
  assert.match(joinLinks.verdict, /1 join-shaped anchor link\(s\)/);
  assert.match(joinLinks.verdict, /plus 1 in meeting-shaped aria-label TEXT/);
});

test("join probe: a join URL in a NON-meeting-shaped label is counted but not credited", () => {
  // A URL in some unrelated widget's label is not this meeting's link —
  // it is counted so the report is complete, but it must not be what
  // makes the verdict say join_url is fillable.
  const widget = el("div", { "aria-label": `Recent link ${ZOOM_LABEL_URL}` });
  setDocument(doc([widget]));

  const { joinLinks } = runProbe();
  assert.equal(joinLinks.labelJoinCount, 1);
  assert.equal(joinLinks.labelJoinInMeetingShapedLabel, 0);
  assert.match(joinLinks.verdict, /no join-shaped links anywhere/);
});

// ── Calendar API probe (v1.6) ────────────────────────────────────────
//
// The probe answers "can we read a meeting's detail without opening
// it?" and its ONLY job is to answer honestly. Every test here is
// about a wrong answer being worse than no answer:
//
//   * a candidate we never asked must not read as one that failed
//   * a 401 must not read as "the API isn't there"
//   * a 200 of the wrong shape must not read as success
//   * a 200 of HTML (a sign-in page) must not read as JSON data
//   * no calendar CONTENT may reach the report, ever
//
// v1.4 shipped a verdict that said "cannot be filled from this DOM"
// when it meant "I looked in one place", and the field data disproved
// it. These pin the replacement.

const API_WANTED = {
  attendees: ["attendees", "requiredattendees"],
  body: ["body", "bodypreview"],
  joinUrl: ["joinurl", "onlinemeeting", "location"],
  timing: ["start", "end"],
};

// Drives _calendarApiProbeFunc with a scripted fetch. The function is
// written to run in the page, so everything it touches — fetch,
// document.cookie, location — is injected here.
async function runApiProbe({ candidates, cookie = "", responses = {} }) {
  const prevFetch = sandbox.fetch;
  const prevDoc = sandbox.document;
  const prevLoc = sandbox.location;

  sandbox.document = { cookie };
  sandbox.location = { origin: "https://outlook.office.com" };
  sandbox.fetch = async (url) => {
    const hit = Object.keys(responses).find((k) => url.includes(k));
    if (!hit) throw new Error("network error");
    const r = responses[hit];
    if (r.throws) throw new Error(r.throws);
    return {
      status: r.status,
      ok: r.status >= 200 && r.status < 300,
      headers: { get: () => r.contentType || "application/json" },
      text: async () => r.text,
    };
  };
  try {
    return await sandbox._calendarApiProbeFunc(candidates, API_WANTED, 7);
  } finally {
    sandbox.fetch = prevFetch;
    sandbox.document = prevDoc;
    sandbox.location = prevLoc;
  }
}

const CANARY_COOKIE = "X-OWA-CANARY=EXAMPLECANARYVALUE; other=1";
const ONE_POST = [{
  name: "owa-service-findItem", note: "n", path: "/owa/service.svc?action=FindItem",
  method: "POST", needsCanary: true, action: "FindItem",
}];
const ONE_GET = [{
  name: "rest-v2-calendarview", note: "n", path: "/api/v2.0/me/calendarview",
  method: "GET", needsCanary: false, action: "",
}];

test("a candidate that was never attempted is not recorded as a failure", async () => {
  // No canary cookie → the request is never sent. Reporting that as
  // "unreachable" would be the exact defect this probe exists over:
  // never having asked rendered as having asked and got nothing.
  const out = await runApiProbe({ candidates: ONE_POST, cookie: "" });
  assert.equal(out.results[0].verdict, "not-attempted");
  assert.match(out.results[0].skipped, /canary/i);
  assert.equal(out.results[0].status, null);
  assert.equal(out.canaryPresent, false);
});

test("a 401 is auth-rejected, never unreachable", async () => {
  const out = await runApiProbe({
    candidates: ONE_POST, cookie: CANARY_COOKIE,
    responses: { "service.svc": { status: 401, text: "{}" } },
  });
  assert.equal(out.results[0].verdict, "auth-rejected");
});

test("a 200 carrying the wanted fields is usable, and says which", async () => {
  const out = await runApiProbe({
    candidates: ONE_POST, cookie: CANARY_COOKIE,
    responses: {
      "service.svc": {
        status: 200,
        text: JSON.stringify({
          Body: { Items: [
            { Subject: "s", Attendees: [{ Name: "n" }], Body: { Text: "t" },
              OnlineMeeting: { JoinUrl: "u" }, Start: "2026-08-20T12:30:00Z" },
            { Subject: "s2", Attendees: [], Body: { Text: "" },
              OnlineMeeting: {}, Start: "2026-08-20T13:30:00Z" },
          ] },
        }),
      },
    },
  });
  const r = out.results[0];
  assert.equal(r.verdict, "usable");
  assert.equal(r.fieldsPresent.attendees, true);
  assert.equal(r.fieldsPresent.body, true);
  assert.equal(r.fieldsPresent.joinUrl, true);
  assert.equal(r.itemCount, 2);
});

test("a 200 with none of the wanted fields is answered-thin, not usable", async () => {
  // The failure that matters most: a tidy 200 that carries nothing we
  // need. Recording it as success would send the implementation at an
  // endpoint that can't do the job.
  const out = await runApiProbe({
    candidates: ONE_POST, cookie: CANARY_COOKIE,
    responses: {
      "service.svc": {
        status: 200,
        text: JSON.stringify({ Body: { Items: [{ ItemId: { Id: "AAA" } }] } }),
      },
    },
  });
  assert.equal(out.results[0].verdict, "answered-thin");
  assert.equal(out.results[0].fieldsPresent.attendees, false);
});

test("a 200 of HTML is unreachable, not a successful JSON answer", async () => {
  // A sign-in redirect returns 200 text/html. Parsing that as success
  // is how "it works" gets reported for a session that isn't signed in.
  const out = await runApiProbe({
    candidates: ONE_GET,
    responses: {
      "calendarview": { status: 200, contentType: "text/html",
                        text: "<!doctype html><html>sign in</html>" },
    },
  });
  assert.equal(out.results[0].verdict, "unreachable");
  assert.equal(out.results[0].json, false);
  assert.match(out.results[0].error, /not JSON/i);
});

test("a thrown fetch is unreachable and carries the reason", async () => {
  const out = await runApiProbe({
    candidates: ONE_GET,
    responses: { "calendarview": { status: 0, throws: "Failed to fetch" } },
  });
  assert.equal(out.results[0].verdict, "unreachable");
  assert.match(out.results[0].error, /Failed to fetch/);
});

test("no calendar content and no token reaches the report", async () => {
  // The report is a file users paste into chat. It must carry field
  // NAMES and counts and nothing else — not a subject, not an
  // attendee, not a join URL, and never the CSRF token.
  const SECRETS = ["Acme Bank Partner Introduction", "a.doe@globex.example",
                   "https://globex.zoom.us/j/00000000000", "EXAMPLECANARYVALUE"];
  const out = await runApiProbe({
    candidates: ONE_POST, cookie: CANARY_COOKIE,
    responses: {
      "service.svc": {
        status: 200,
        text: JSON.stringify({ Body: { Items: [{
          Subject: SECRETS[0],
          Attendees: [{ EmailAddress: SECRETS[1] }],
          Body: { Text: `Join here ${SECRETS[2]}` },
          OnlineMeeting: { JoinUrl: SECRETS[2] },
          Start: "2026-08-20T12:30:00Z",
        }] } }),
      },
    },
  });
  const blob = JSON.stringify(out);
  for (const s of SECRETS) assert.ok(!blob.includes(s), `leaked: ${s}`);
  // ...while still having actually measured something.
  assert.equal(out.results[0].verdict, "usable");
  assert.equal(out.canaryPresent, true);   // presence, never the value
});

test("field detection matches key NAMES only, never values", async () => {
  // A meeting whose subject contains the word "attendees" must not
  // make a thin endpoint look usable.
  const out = await runApiProbe({
    candidates: ONE_POST, cookie: CANARY_COOKIE,
    responses: {
      "service.svc": {
        status: 200,
        text: JSON.stringify({ Body: { Items: [
          { ItemId: { Id: "A" }, Subject: "Confirm attendees and body for the call" },
        ] } }),
      },
    },
  });
  assert.equal(out.results[0].verdict, "answered-thin");
  assert.equal(out.results[0].fieldsPresent.attendees, false);
  assert.equal(out.results[0].fieldsPresent.body, false);
});

// ── Detail from Outlook's own responses (v1.7) ───────────────────────
//
// v1.6 tried to CALL the calendar API and shipped four candidate
// endpoints, all modelled on classic OWA. The field run said
// `outlook.cloud.microsoft`, `canaryPresent: false`, 401 — the new
// Outlook stack, no CSRF canary, bearer auth. Every guess was wrong.
//
// So v1.7 stops guessing: it records the responses Outlook itself
// receives. These tests pin the property that makes that work — the
// parser must not know or care which API shape produced the payload,
// because not knowing is the entire point.

const { detailsFromResponses, mergeDetailIntoEvents, detailKey } = sandbox;

// Graph-ish, as the NEW Outlook stack returns.
const GRAPH_BODY = {
  value: [{
    subject: "Quarterly review",
    start: { dateTime: "2026-08-20T12:30:00.0000000", timeZone: "UTC" },
    attendees: [
      { emailAddress: { name: "Ana Doe", address: "a.doe@globex.example" }, type: "required" },
      { emailAddress: { name: "Pat Roe", address: "p.roe@globex.example" }, type: "optional" },
    ],
    body: { contentType: "html", content: "<p>Agenda:</p><ul><li>Numbers</li></ul>" },
    onlineMeeting: { joinUrl: "https://teams.microsoft.com/l/meetup-join/EXAMPLE" },
  }],
};

// EWS-over-JSON, as CLASSIC OWA returns. Same three fields, different
// names, different nesting, different datetime shape.
const EWS_BODY = {
  Body: { ResponseMessages: { Items: [{ RootFolder: { Items: [{
    Subject: "Quarterly review",
    Start: "2026-08-20T12:30:00Z",
    RequiredAttendees: [{ Mailbox: { Name: "Ana Doe" } }],
    Body: { BodyType: "Text", Value: "Agenda: numbers" },
    JoinUrl: "https://globex.zoom.us/j/00000000000",
  }] } }] } },
};

test("detail is read from the NEW Outlook stack's shape", () => {
  const d = detailsFromResponses([GRAPH_BODY]);
  const hit = d.get(detailKey("Quarterly review", "2026-08-20T12:30"));
  assert.ok(hit, "no detail extracted");
  assert.deepEqual([...hit.attendees], ["Ana Doe", "Pat Roe"]);
  assert.match(hit.body, /Agenda/);
  assert.match(hit.joinUrl, /meetup-join/);
});

test("detail is read from classic OWA's shape, with no code that knows which", () => {
  // The SAME parser, no branch on tenant, API version or host. v1.6
  // failed precisely because it had to know; this must not.
  const d = detailsFromResponses([EWS_BODY]);
  const hit = d.get(detailKey("Quarterly review", "2026-08-20T12:30"));
  assert.ok(hit, "no detail extracted");
  assert.deepEqual([...hit.attendees], ["Ana Doe"]);
  assert.equal(hit.body, "Agenda: numbers");
  assert.match(hit.joinUrl, /zoom\.us/);
});

test("HTML invite bodies are reduced to readable text", () => {
  const d = detailsFromResponses([GRAPH_BODY]);
  const hit = d.get(detailKey("Quarterly review", "2026-08-20T12:30"));
  assert.ok(!hit.body.includes("<"), `markup survived: ${hit.body}`);
  assert.match(hit.body, /Numbers/);
});

test("a script tag in an invite body is dropped, not flattened into text", () => {
  const d = detailsFromResponses([{ value: [{
    subject: "S", start: "2026-08-20T09:00:00",
    body: { content: "<p>Hi</p><script>alert(1)</script>" },
  }] }]);
  const hit = d.get(detailKey("S", "2026-08-20T09:00"));
  assert.ok(!hit.body.includes("alert"), `script body survived: ${hit.body}`);
  assert.match(hit.body, /Hi/);
});

test("seconds and timezone suffixes still match the label-parsed start", () => {
  // The API returns seconds and a zone; parseMeetingLabel produces
  // neither. An exact string compare would match nothing at all — and
  // would look exactly like "the API carried no detail".
  assert.equal(
    detailKey("A", "2026-08-20T12:30:00.0000000"),
    detailKey("A", "2026-08-20T12:30"),
  );
  assert.equal(detailKey("A", "2026-08-20T12:30:59Z"), detailKey("A", "2026-08-20T12:30"));
  // ...but a different minute is a different meeting.
  assert.notEqual(detailKey("A", "2026-08-20T12:31"), detailKey("A", "2026-08-20T12:30"));
});

test("merging is additive: a field the label already filled is never overwritten", () => {
  // v1.5's label extraction is the more specific signal for the Zoom
  // case. A later generic response must not clobber it.
  const events = [{
    subject: "Quarterly review", start: "2026-08-20T12:30:00",
    attendees: ["Already Known"], body: "", join_url: "https://kept.example/j/1",
  }];
  const { events: out, stats } = mergeDetailIntoEvents(
    events, detailsFromResponses([GRAPH_BODY]));
  assert.equal(stats.matched, 1);
  assert.deepEqual([...out[0].attendees], ["Already Known"]);
  assert.equal(out[0].join_url, "https://kept.example/j/1");
  // ...but the field that WAS empty gets filled.
  assert.match(out[0].body, /Agenda/);
  assert.equal(stats.gainedBody, 1);
  assert.equal(stats.gainedAttendees, 0);
  assert.equal(stats.gainedJoinUrl, 0);
});

test("an event with no matching response comes through byte-identical", () => {
  const before = {
    subject: "Unrelated", start: "2026-08-21T09:00:00",
    attendees: [], body: "", join_url: "",
  };
  const snapshot = JSON.stringify(before);
  const { events, stats } = mergeDetailIntoEvents(
    [before], detailsFromResponses([GRAPH_BODY]));
  assert.equal(stats.matched, 0);
  assert.equal(JSON.stringify(events[0]), snapshot);
});

test("no captured responses changes nothing at all", () => {
  // The state when the recorder could not install (Chrome < 111) or
  // Outlook served the grid from cache. Must degrade to exactly
  // pre-v1.7 behaviour, never to an error or a wiped field.
  const before = [{ subject: "A", start: "2026-08-20T09:00:00",
                    attendees: ["X"], body: "keep", join_url: "u" }];
  const snapshot = JSON.stringify(before);
  const { events, stats } = mergeDetailIntoEvents(before, detailsFromResponses([]));
  assert.equal(stats.matched, 0);
  assert.equal(JSON.stringify(events), snapshot);
});

test("an unrecognised payload yields nothing rather than garbage", () => {
  // A telemetry beacon that happened to match a URL hint. Inventing a
  // meeting out of it would be worse than missing one.
  const d = detailsFromResponses([
    { telemetry: { events: [{ name: "click", ts: 12345 }] } },
    "not an object",
    null,
  ]);
  assert.equal(d.size, 0);
});

test("the same meeting across two responses merges rather than overwrites", () => {
  // A list call carries attendees; a detail call carries the body.
  // Whichever response HAD a field must win over a later one that
  // didn't — otherwise arrival order silently decides what survives.
  const list = { value: [{ subject: "M", start: "2026-08-20T09:00:00",
                           attendees: [{ emailAddress: { name: "Ana Doe" } }] }] };
  const detail = { value: [{ subject: "M", start: "2026-08-20T09:00:00",
                             body: { content: "The agenda" } }] };
  const d = detailsFromResponses([list, detail]);
  const hit = d.get(detailKey("M", "2026-08-20T09:00"));
  assert.deepEqual([...hit.attendees], ["Ana Doe"]);
  assert.equal(hit.body, "The agenda");
});

test("attendees fall back to an address only when there is no name", () => {
  const d = detailsFromResponses([{ value: [{
    subject: "M", start: "2026-08-20T09:00:00",
    attendees: [
      { emailAddress: { name: "Ana Doe", address: "a.doe@globex.example" } },
      { emailAddress: { address: "unnamed@globex.example" } },
    ],
  }] }]);
  const hit = d.get(detailKey("M", "2026-08-20T09:00"));
  // "no attendees" and "attendees we could not name" are different
  // states; the second must not silently become the first.
  assert.deepEqual([...hit.attendees], ["Ana Doe", "unnamed@globex.example"]);
});

test("a person listed as both required and optional appears once", () => {
  const d = detailsFromResponses([{ value: [{
    subject: "M", start: "2026-08-20T09:00:00",
    requiredAttendees: [{ name: "Ana Doe" }],
    optionalAttendees: [{ name: "ana doe" }],
  }] }]);
  const hit = d.get(detailKey("M", "2026-08-20T09:00"));
  assert.deepEqual([...hit.attendees], ["Ana Doe"]);
});

test("the recorder file registerCalendarRecorder names actually exists", () => {
  // registerContentScripts takes a FILENAME, not a function, so a
  // rename or a missing file fails at runtime inside a background
  // service worker — where nobody sees it, and the only symptom is
  // attendees quietly staying empty. That is the silent-degradation
  // shape this project keeps shipping, so it gets a build-time check.
  const src = fs.readFileSync(BG_PATH, "utf8");
  const m = src.match(/js:\s*\[\s*"([^"]+)"\s*\]/);
  assert.ok(m, "registerCalendarRecorder no longer declares a js file");
  assert.ok(
    fs.existsSync(path.join(path.dirname(BG_PATH), m[1])),
    `background.js registers "${m[1]}" but that file does not exist`,
  );
});

test("the recorder registers for the NEW Outlook host, not just classic OWA", () => {
  // The whole reason v1.6 failed: the field tenant is
  // outlook.cloud.microsoft. A recorder that only matches
  // outlook.office.com would never install there and would look
  // exactly like "the response carried nothing".
  const src = fs.readFileSync(BG_PATH, "utf8");
  const block = src.slice(src.indexOf("async function registerCalendarRecorder"));
  assert.match(block, /outlook\.cloud\.microsoft/);
  assert.match(block, /outlook\.office\.com/);
  assert.match(block, /world:\s*"MAIN"/);
  assert.match(block, /runAt:\s*"document_start"/);
});

// ── Mechanism 2: detail read off the screen (v1.9) ───────────────────
//
// Five releases bet on one mechanism at a time — an endpoint, an auth
// scheme, a JSON shape, a fetch thread — each unobservable from here.
// This one reads what Outlook has already RENDERED, which cannot be
// defeated by any of those. These tests pin that it is structure-
// agnostic (no selector in the detail pane) and that it never makes
// things worse than leaving the fields empty.

// JOIN_PROVIDER_PATTERNS is a top-level `const`, and a `const` in a vm
// script does NOT become a property of the context object the way a
// function declaration does — `sandbox.JOIN_PROVIDER_PATTERNS` is
// undefined. Read it by evaluating the identifier inside the context
// instead, so these tests exercise the REAL provider list rather than
// a local copy that could drift from it.
const JOIN_PATTERNS_FROM_SOURCE =
  vm.runInContext("JOIN_PROVIDER_PATTERNS", sandbox);

// Like fakeDetailPage but the click also reveals ANCHORS and
// attendee-name elements — the two things a Teams pane has and a
// text-only reader cannot see.
function fakeDetailPageRich({ tiles, reveal }) {
  let extra = "";
  let anchors = [];
  let people = [];
  const base = "Calendar grid baseline text that is already on screen.";
  const mkAttr = (attrs) => ({
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
  });
  const els = tiles.map((t) => ({
    getAttribute: (n) => (n === "aria-label" ? t.label : null),
    click() {
      const r = reveal[t.subject] || {};
      extra = "\n" + (r.text || "");
      anchors = (r.hrefs || []).map((h) => mkAttr({ href: h }));
      people = (r.names || []).map((n) => mkAttr({ "aria-label": n }));
    },
  }));
  const doc = {
    body: { get innerText() { return base + extra; } },
    querySelectorAll: (sel) => {
      if (String(sel).includes("a[href]")) return anchors;
      if (String(sel).includes("[title]")) return [...people, ...els];
      return els;
    },
    dispatchEvent: () => { extra = ""; anchors = []; people = []; return true; },
  };
  return doc;
}

async function runRichDetailReader({ tiles, reveal, wanted }) {
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = fakeDetailPageRich({ tiles, reveal });
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    return await sandbox._readEventDetailsFunc(
      wanted, JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
}

function fakeDetailPage({ tiles, revealText }) {
  // Minimal page: aria-labelled tiles that, when clicked, grow
  // body.innerText by revealText[subject]. No classes, no roles — if
  // the reader needed those, it would fail here.
  let extra = "";
  const base = "Calendar grid baseline text that is already on screen.";
  const els = tiles.map((t) => ({
    getAttribute: (n) => (n === "aria-label" ? t.label : null),
    click() { extra = "\n" + (revealText[t.subject] || ""); },
  }));
  return {
    body: { get innerText() { return base + extra; } },
    querySelectorAll: () => els,
    dispatchEvent: () => { extra = ""; return true; },
  };
}

async function runDetailReader({ tiles, revealText, wanted, max = 25, budget = 90000 }) {
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = fakeDetailPage({ tiles, revealText });
  sandbox.KeyboardEvent = function () {};
  // setTimeout is NOT a vm-context intrinsic, and the reader polls
  // with it. Without this the function throws, its outer catch
  // swallows the throw, and every counter reads zero — which looks
  // exactly like "nothing matched".
  sandbox.setTimeout = setTimeout;
  try {
    return await sandbox._readEventDetailsFunc(
      wanted, JOIN_PATTERNS_FROM_SOURCE, max, budget);
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
}

test("attendees are read as ADDRESSES, with no dependence on markup", async () => {
  const out = await runDetailReader({
    tiles: [{ subject: "Quarterly review", label: "Quarterly review, 12:30 PM to 1:00 PM" }],
    revealText: {
      "Quarterly review":
        "Required: Ana Doe a.doe@globex.example; Pat Roe p.roe@globex.example\n"
        + "Agenda: numbers, then the roadmap.",
    },
    wanted: [{ subject: "Quarterly review", startIso: "2026-08-20T12:30:00" }],
  });
  assert.equal(out.opened, 1);
  assert.equal(out.grew, 1);
  const d = out.details[0];
  assert.deepEqual([...d.attendees],
    ["a.doe@globex.example", "p.roe@globex.example"]);
  assert.match(d.body, /roadmap/);
  // Addresses are stripped from the agenda so it doesn't restate the
  // attendee list.
  assert.ok(!d.body.includes("@"), `addresses leaked into body: ${d.body}`);
});

test("a Teams join link is recognised, an incidental link is not", async () => {
  const out = await runDetailReader({
    tiles: [{ subject: "Teams sync", label: "Teams sync, 9:00 AM to 9:30 AM" }],
    revealText: {
      "Teams sync":
        "Join: https://teams.microsoft.com/l/meetup-join/EXAMPLE\n"
        + "Notes: https://learning.example.com/library/course-42",
    },
    wanted: [{ subject: "Teams sync", startIso: "2026-08-20T09:00:00" }],
  });
  // THE case five releases failed on: a Teams meeting whose URL is not
  // in the grid label at all.
  assert.match(out.details[0].joinUrl, /meetup-join/);
  assert.ok(!out.details[0].joinUrl.includes("learning.example.com"));
});

test("an event whose pane never renders yields nothing, not garbage", async () => {
  // Clicked, nothing appeared. Inventing an empty attendee list would
  // be indistinguishable from a meeting that genuinely has none.
  const out = await runDetailReader({
    tiles: [{ subject: "Silent one", label: "Silent one, 9:00 AM to 9:30 AM" }],
    revealText: { "Silent one": "" },
    wanted: [{ subject: "Silent one", startIso: "2026-08-20T09:00:00" }],
  });
  assert.equal(out.opened, 1);
  assert.equal(out.grew, 0);
  assert.equal(out.details.length, 0);
});

test("an event with no matching tile is skipped and counted", async () => {
  const out = await runDetailReader({
    tiles: [{ subject: "Something else", label: "Something else, 9:00 AM to 9:30 AM" }],
    revealText: {},
    wanted: [{ subject: "Not on screen", startIso: "2026-08-20T09:00:00" }],
  });
  assert.equal(out.matchedElement, 0);
  assert.equal(out.skipped, 1);
  assert.equal(out.details.length, 0);
});

test("the per-run cap is honoured and the remainder reported as skipped", async () => {
  // 47 events one at a time is not acceptable. What is not reached
  // must be COUNTED, never silently dropped — a truncated pass that
  // reads as a complete one is this project's recurring defect.
  const tiles = [], reveal = {}, wanted = [];
  for (let i = 0; i < 5; i++) {
    tiles.push({ subject: `M${i}`, label: `M${i}, 9:00 AM to 9:30 AM` });
    reveal[`M${i}`] = `Attendee a${i}@globex.example agenda text here`;
    wanted.push({ subject: `M${i}`, startIso: "2026-08-20T09:00:00" });
  }
  const out = await runDetailReader({ tiles, revealText: reveal, wanted, max: 2 });
  assert.equal(out.details.length, 2);
  assert.equal(out.skipped, 3);
});

test("detail read from the screen merges additively, like every other source", async () => {
  const out = await runDetailReader({
    tiles: [{ subject: "M", label: "M, 9:00 AM to 9:30 AM" }],
    revealText: { M: "Attendee a.doe@globex.example agenda" },
    wanted: [{ subject: "M", startIso: "2026-08-20T09:00:00" }],
  });
  const byKey = new Map([[
    detailKey("M", "2026-08-20T09:00"),
    { attendees: out.details[0].attendees, body: out.details[0].body, joinUrl: "" },
  ]]);
  const events = [{ subject: "M", start: "2026-08-20T09:00:00",
                    attendees: [], body: "", join_url: "https://kept.example/j/1" }];
  const { stats } = mergeDetailIntoEvents(events, byKey);
  assert.equal(stats.gainedAttendees, 1);
  // A join URL the label already supplied still wins.
  assert.equal(events[0].join_url, "https://kept.example/j/1");
  assert.equal(stats.gainedJoinUrl, 0);
});

// ── The screen pass must run while the week is rendered (v1.10) ──────
//
// THE BUG. v1.9 ran a single detail pass AFTER both weeks were
// scanned — i.e. after goToNextCalendarWeek had navigated away from
// the current week, with no navigation back. Mechanism 2 finds an
// event's tile by aria-label and clicks it, and only considers events
// starting inside DETAIL_WINDOW_HOURS (72h) — all of which are tiles in
// the CURRENT week. So it looked for tiles that were no longer
// rendered, matched nothing, counted everything skipped, and returned
// in under a second having opened nothing.
//
// Field evidence: a whole calendar-only capture finished in ~21s. A
// pass that actually opened ~20 events cannot finish in under a
// minute. The mechanism shipped in v2.45.0 never clicked once.

function fakeScripting(handler) {
  const prev = sandbox.chrome.scripting.executeScript;
  sandbox.chrome.scripting.executeScript = handler;
  return () => { sandbox.chrome.scripting.executeScript = prev; };
}

const NOW_ISO = () => {
  const d = new Date(Date.now() + 3600 * 1000);   // an hour from now
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
         `T${p(d.getHours())}:${p(d.getMinutes())}:00`;
};

function labelFor(subject, startIso) {
  // "Subject, 9:30 AM to 10:00 AM, Thursday, August 20, 2026, ..."
  const d = new Date(startIso);
  const end = new Date(d.getTime() + 30 * 60000);
  const t = (x) => {
    let h = x.getHours(); const m = String(x.getMinutes()).padStart(2, "0");
    const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
    return `${h}:${m} ${ap}`;
  };
  const MONTHS = ["January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];
  const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday"];
  return `${subject}, ${t(d)} to ${t(end)}, ${DAYS[d.getDay()]}, ` +
         `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}, ` +
         `Microsoft Teams Meeting, By Ana Poe, Busy`;
}

test("collectWeekDetail opens the events of the week it is given", async () => {
  const startIso = NOW_ISO();
  const candidates = [{ label: labelFor("Team Sync", startIso), layer: "aria-label" }];
  let handedToClicker = null;

  const restore = fakeScripting(async ({ args }) => {
    handedToClicker = args[0];
    return [{ result: {
      details: [{
        subject: "Team Sync", startIso,
        attendees: ["a.doe@globex.example"], body: "Agenda here", joinUrl: "",
      }],
      opened: 1, matchedElement: 1, grew: 1, skipped: 0, error: null,
    } }];
  });
  try {
    const diag = { domDetailAttempted: 0, domDetailOpened: 0, domDetailGrew: 0,
                   domDetailSkipped: 0, domDetailNoTile: 0 };
    const into = new Map();
    await sandbox.collectWeekDetail(1, candidates, [], diag, into);

    assert.equal(diag.domDetailAttempted, 1, "the event was never offered to the clicker");
    assert.equal(diag.domDetailOpened, 1);
    assert.equal(handedToClicker.length, 1);
    assert.equal(handedToClicker[0].subject, "Team Sync");

    const hit = into.get(sandbox.detailKey("Team Sync", startIso));
    assert.ok(hit, "detail was not recorded");
    assert.deepEqual([...hit.attendees], ["a.doe@globex.example"]);
    assert.equal(hit.body, "Agenda here");
  } finally { restore(); }
});

test("an event beyond the 72h window is not opened", async () => {
  const far = new Date(Date.now() + 10 * 24 * 3600 * 1000).toISOString().slice(0, 19);
  const candidates = [{ label: labelFor("Far Future", far), layer: "aria-label" }];
  let called = false;
  const restore = fakeScripting(async () => { called = true; return [{ result: null }]; });
  try {
    const diag = { domDetailAttempted: 0, domDetailOpened: 0, domDetailGrew: 0,
                   domDetailSkipped: 0, domDetailNoTile: 0 };
    await sandbox.collectWeekDetail(1, candidates, [], diag, new Map());
    assert.equal(diag.domDetailAttempted, 0);
    assert.equal(called, false, "clicked an event outside the window");
  } finally { restore(); }
});

test("an event the response recorder already answered is not opened", async () => {
  // Mechanism 1 is cheap and runs first; mechanism 2 must not pay to
  // re-open what it already has.
  const startIso = NOW_ISO();
  const candidates = [{ label: labelFor("Team Sync", startIso), layer: "aria-label" }];
  const body = { value: [{
    subject: "Team Sync", start: startIso,
    attendees: [{ emailAddress: { name: "Ana Doe" } }],
  }] };
  let called = false;
  const restore = fakeScripting(async () => { called = true; return [{ result: null }]; });
  try {
    const diag = { domDetailAttempted: 0, domDetailOpened: 0, domDetailGrew: 0,
                   domDetailSkipped: 0, domDetailNoTile: 0 };
    const into = new Map();
    await sandbox.collectWeekDetail(1, candidates, [body], diag, into);
    assert.equal(called, false, "re-opened an event mechanism 1 had already filled");
    assert.deepEqual([...into.get(sandbox.detailKey("Team Sync", startIso)).attendees],
                     ["Ana Doe"]);
  } finally { restore(); }
});

test("a clicker that throws is not fatal and is reported", async () => {
  const startIso = NOW_ISO();
  const candidates = [{ label: labelFor("Team Sync", startIso), layer: "aria-label" }];
  const restore = fakeScripting(async () => { throw new Error("tab closed"); });
  try {
    const diag = { domDetailAttempted: 0, domDetailOpened: 0, domDetailGrew: 0,
                   domDetailSkipped: 0, domDetailNoTile: 0 };
    await sandbox.collectWeekDetail(1, candidates, [], diag, new Map());
    assert.match(diag.domDetailError, /tab closed/);
  } finally { restore(); }
});

test("the screen pass is invoked BEFORE the week navigation", () => {
  // The ordering IS the bug. A functional test cannot easily drive the
  // whole two-week capture, but the order of these three calls in
  // captureCalendarTab is exactly what broke, so it is pinned directly.
  const src = fs.readFileSync(BG_PATH, "utf8");
  const body = src.slice(src.indexOf("async function captureCalendarTab()"));
  const firstDetail = body.indexOf("collectWeekDetail(tabId, week1.candidates");
  const nav = body.indexOf("goToNextCalendarWeek(tabId)");
  assert.ok(firstDetail > 0, "week 1 detail pass is gone");
  assert.ok(nav > 0, "week navigation is gone");
  assert.ok(
    firstDetail < nav,
    "the week-1 screen pass runs AFTER navigating to week 2 — its tiles " +
    "are no longer rendered, which is the v2.45.0 no-op");
});

// ── Teams: the URL is in a Join BUTTON, not in the text (v1.11) ──────
//
// FIELD RESULT 2026-08-20, v1.10: Webex and Zoom meetings gained a
// Join link, Teams meetings gained nothing. Not a Teams parsing
// problem — a question-shape problem, and the same one as v1.4's
// anchor-only probe, exactly inverted:
//
//   v1.4   looked at ELEMENTS, missed a URL that was text  → wrong
//   v1.10  looked at TEXT, missed a URL that is an href    → wrong
//
// Webex/Zoom add-ins paste the raw URL into the invite body, so it IS
// text. Teams renders a button and puts the URL only in the href.

test("a Teams join link is found in an anchor href, not the text", async () => {
  const out = await runRichDetailReader({
    tiles: [{ subject: "Team sync", label: "Team sync, 10:00 AM to 10:30 AM" }],
    reveal: {
      "Team sync": {
        // Exactly what a Teams pane shows: the words, never the URL.
        text: "Microsoft Teams Meeting\nJoin the meeting now",
        hrefs: ["https://teams.microsoft.com/l/meetup-join/19%3ameeting_EXAMPLE%40thread.v2/0"],
      },
    },
    wanted: [{ subject: "Team sync", startIso: "2026-08-21T10:00:00" }],
  });
  const d = out.details[0];
  assert.ok(d, "nothing extracted");
  assert.match(d.joinUrl, /teams\.microsoft\.com\/l\/meetup-join/);
  assert.equal(out.joinFromAnchor, 1);
});

test("a text URL still wins over an anchor when both are present", async () => {
  // A pasted URL is unambiguously part of THIS invite; an anchor is
  // located only by having newly appeared.
  const out = await runRichDetailReader({
    tiles: [{ subject: "Webex call", label: "Webex call, 9:30 AM to 9:45 AM" }],
    reveal: {
      "Webex call": {
        text: "Join here https://globex.webex.com/globex/j.php?MTID=EXAMPLE",
        hrefs: ["https://teams.microsoft.com/l/meetup-join/OTHER"],
      },
    },
    wanted: [{ subject: "Webex call", startIso: "2026-08-21T09:30:00" }],
  });
  assert.match(out.details[0].joinUrl, /webex\.com/);
  assert.equal(out.joinFromAnchor, 0);
});

test("a non-conferencing anchor never becomes a join link", async () => {
  // Every pane is full of navigation links. Only the shared provider
  // list may produce a Join button.
  const out = await runRichDetailReader({
    tiles: [{ subject: "Training", label: "Training, 2:00 PM to 3:30 PM" }],
    reveal: {
      "Training": {
        text: "Course block",
        hrefs: ["https://learning.example.com/library/", "https://outlook.office.com/mail/"],
      },
    },
    wanted: [{ subject: "Training", startIso: "2026-08-20T14:00:00" }],
  });
  const d = out.details[0];
  if (d) assert.equal(d.joinUrl, "");
  assert.equal(out.joinFromAnchor, 0);
});

test("an anchor already on screen before the click is not attributed", async () => {
  // The previous meeting's Join button is still in the DOM. Attaching
  // it to this meeting would send the user into the wrong call —
  // worse than an empty field.
  const page = fakeDetailPageRich({
    tiles: [{ subject: "Second", label: "Second, 11:00 AM to 11:30 AM" }],
    reveal: { "Second": { text: "Some agenda text", hrefs: [] } },
  });
  const stale = { getAttribute: (n) => (n === "href"
    ? "https://teams.microsoft.com/l/meetup-join/STALE" : null) };
  const origQSA = page.querySelectorAll;
  page.querySelectorAll = (sel) => (String(sel).includes("a[href]")
    ? [stale] : origQSA(sel));

  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = page;
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    const out = await sandbox._readEventDetailsFunc(
      [{ subject: "Second", startIso: "2026-08-21T11:00:00" }],
      JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
    const d = out.details[0];
    if (d) assert.equal(d.joinUrl, "", "a pre-existing anchor was attributed");
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
});

// ── Attendees are DATA, never text-shape guesses (v1.13) ─────────────
//
// FIELD RESULT 2026-08-20, extension 1.12: a meeting showed
// "Attendees (24)" — 22 of them Outlook's own controls ("Skip to main
// content", "App launcher", "Ribbon tabs", "Chat with Copilot"), plus
// the user's OWN account button, plus another meeting's organiser
// address pulled in by the whole-page email scan. The name-shape
// scanner is deleted; these tests pin what replaced it.

test("no label on the page is ever reported as an attendee", async () => {
  // The pane reveals text and the page is littered with name-shaped
  // labels. None of them may become people: attendees from the screen
  // pass are pane-diff EMAILS only. (Real display names come from
  // Outlook's own detail responses, which are data, not shapes.)
  const out = await runRichDetailReader({
    tiles: [{ subject: "Review", label: "Review, 3:00 PM to 3:30 PM" }],
    reveal: {
      "Review": {
        text: "Agenda for the review session.",
        names: ["App launcher", "Skip to main content", "Ana Doe",
                "Chat with Copilot", "Jordan Poe"],
      },
    },
    wanted: [{ subject: "Review", startIso: "2026-08-20T15:00:00" }],
  });
  const d = out.details[0];
  assert.ok(d, "nothing extracted");
  assert.deepEqual([...d.attendees], [],
    "a page label was reported as a person");
});

test("emails in the pane's own text are collected as attendees", async () => {
  const out = await runRichDetailReader({
    tiles: [{ subject: "Mixed", label: "Mixed, 1:00 PM to 2:00 PM" }],
    reveal: {
      "Mixed": { text: "Also invited: k.noh@zorg.example and a.doe@globex.example" },
    },
    wanted: [{ subject: "Mixed", startIso: "2026-08-20T13:00:00" }],
  });
  assert.deepEqual([...out.details[0].attendees].sort(),
                   ["a.doe@globex.example", "k.noh@zorg.example"]);
});

test("an address visible elsewhere on the page is not attributed", async () => {
  // The field case: a DIFFERENT meeting's organiser address is
  // rendered in that meeting's grid tile label, i.e. it is on screen
  // before the click. The whole-page scan attributed it to whichever
  // meeting was clicked; the pane-diff scan must not.
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  let extra = "";
  const base = "Grid baseline. UPS-AWS Flow, 12:30 PM, By k.noh@zorg.example, Tentative.";
  sandbox.document = {
    body: { get innerText() { return base + extra; } },
    querySelectorAll: (sel) => (String(sel).includes("a[href]") ? [] : [{
      getAttribute: (n) => (n === "aria-label"
        ? "Follow up session, 10:00 AM to 11:00 AM" : null),
      click() { extra = "\nAgenda: finalize the SOW."; },
    }]),
    dispatchEvent: () => { extra = ""; return true; },
  };
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    const out = await sandbox._readEventDetailsFunc(
      [{ subject: "Follow up session", startIso: "2026-08-21T10:00:00" }],
      JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
    const d = out.details[0];
    assert.ok(d, "nothing extracted");
    assert.deepEqual([...d.attendees], [],
      "another meeting's organiser address was attributed to this one");
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
});

// ── The 1.11 field regression: extraction fired before the pane loaded ─
//
// Outlook Web's DOM churns on its own — anchors and labelled elements
// appear without anyone clicking anything. 1.11's "has the pane
// rendered?" check treated ANY new anchor and ANY change in the
// page-wide label count as the pane arriving, so on a real calendar it
// fired on the first 150ms poll, extraction ran against a pane that
// had not loaded, and every meeting came back empty. The Webex links
// 1.10 had been finding disappeared — and the empty capture then
// overwrote the store's previously-enriched events.
//
// None of the existing tests caught it because every fake page was
// SILENT until clicked. These model the churn.

function churningPage({ paneDelayMs, paneText }) {
  // A page where an unrelated, NON-join anchor appears immediately
  // after the click (the churn), and the pane's real content arrives
  // only after `paneDelayMs`.
  let clickAt = null;
  const base = "Calendar grid baseline text that is already on screen.";
  const tile = {
    getAttribute: (n) => (n === "aria-label"
      ? "Standup, 9:30 AM to 9:45 AM" : null),
    click() { clickAt = Date.now(); },
  };
  const churnAnchor = {
    getAttribute: (n) => (n === "href"
      ? "https://outlook.office.com/mail/deeplink/1" : null),
  };
  const paneOpen = () => clickAt !== null && Date.now() - clickAt >= paneDelayMs;
  return {
    body: {
      get innerText() {
        return paneOpen() ? base + "\n" + paneText : base;
      },
    },
    querySelectorAll: (sel) => {
      if (String(sel).includes("a[href]")) {
        // The churn anchor exists the instant the click happens —
        // BEFORE the pane content — exactly the SPA behaviour that
        // fooled 1.11.
        return clickAt !== null ? [churnAnchor] : [];
      }
      return [tile];
    },
    dispatchEvent: () => true,
  };
}

test("page churn does not trigger extraction before the pane loads", async () => {
  // THE regression test. The churn anchor appears at t=0; the pane's
  // Webex URL arrives at t=500ms. 1.11 extracted at the first poll and
  // found nothing; the fix must wait for a signal that is actually
  // pane-shaped and then let the page settle.
  const page = churningPage({
    paneDelayMs: 500,
    paneText: "Join here https://globex.webex.com/globex/j.php?MTID=EXAMPLE",
  });
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = page;
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    const out = await sandbox._readEventDetailsFunc(
      [{ subject: "Standup", startIso: "2026-08-21T09:30:00" }],
      JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
    assert.equal(out.opened, 1);
    const d = out.details[0];
    assert.ok(d, "extraction fired early and captured nothing");
    assert.match(d.joinUrl, /webex\.com/,
      "the join link the pane carried was missed — extraction ran "
      + "before the pane rendered");
    // And the body must be the pane's text, never the whole page.
    assert.ok(!(d.body || "").includes("Calendar grid baseline"),
      "whole-page text was stored as the meeting body");
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
});

test("a previous pane's attendees do not bleed into the next meeting", async () => {
  // Escape does not always close a pane. Its text — including invitee
  // addresses — is then part of meeting B's BEFORE snapshot, and the
  // pane-diff scoping must keep it out of B.
  let phase = 0;
  const mk = (label, n) => ({
    getAttribute: (name) => (name === "aria-label" ? label : null),
    click() { phase = n; },
  });
  const page = {
    body: {
      get innerText() {
        return "Baseline calendar text on the screen already here."
          + (phase >= 1 ? "\nAlpha agenda. Invited: ana.doe@globex.example" : "")
          + (phase >= 2 ? "\nBeta agenda. Invited: pat.roe@globex.example" : "");
      },
    },
    querySelectorAll: (sel) => (String(sel).includes("a[href]") ? [] : [
      mk("Alpha, 9:00 AM to 9:30 AM", 1),
      mk("Beta, 10:00 AM to 10:30 AM", 2),
    ]),
    dispatchEvent: () => true,  // Escape does nothing — pane A stays
  };
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = page;
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    const out = await sandbox._readEventDetailsFunc(
      [{ subject: "Alpha", startIso: "2026-08-21T09:00:00" },
       { subject: "Beta", startIso: "2026-08-21T10:00:00" }],
      JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
    const alpha = out.details.find((d) => d.subject === "Alpha");
    const beta = out.details.find((d) => d.subject === "Beta");
    assert.ok(alpha && beta, "both meetings should yield detail");
    assert.deepEqual([...alpha.attendees], ["ana.doe@globex.example"]);
    assert.deepEqual([...beta.attendees], ["pat.roe@globex.example"],
      "pane A's invitee bled into meeting B");
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
});

// ── The diff must not care where the pane renders (v1.14) ────────────
//
// Every fake page in this suite APPENDED the pane's text to the body,
// so the old prefix-match diff always worked here — and nothing
// guaranteed it worked on real Outlook, where the event panel's
// position in the DOM relative to the grid is unknown. If the panel
// renders BEFORE the grid, the prefix diff yields "" for every event:
// no body, no addresses, no pasted URL, indistinguishable from "the
// pane had nothing".

async function runPositionalPage(position) {
  // position: where the pane's text lands in body.innerText —
  // "append", "prepend", or "middle".
  let open = false;
  const base1 = "Calendar grid first half with meetings and labels.";
  const base2 = "Calendar grid second half with more meetings.";
  const pane = "Invite body agenda text.\nJoin here https://globex.webex.com/globex/j.php?MTID=EXAMPLE\nAlso invited: a.doe@globex.example";
  const page = {
    body: {
      get innerText() {
        if (!open) return base1 + "\n" + base2;
        if (position === "append") return base1 + "\n" + base2 + "\n" + pane;
        if (position === "prepend") return pane + "\n" + base1 + "\n" + base2;
        return base1 + "\n" + pane + "\n" + base2;
      },
    },
    querySelectorAll: (sel) => (String(sel).includes("a[href]") ? [] : [{
      getAttribute: (n) => (n === "aria-label"
        ? "Standup, 9:30 AM to 9:45 AM" : null),
      click() { open = true; },
    }]),
    dispatchEvent: () => { open = false; return true; },
  };
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = page;
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    return await sandbox._readEventDetailsFunc(
      [{ subject: "Standup", startIso: "2026-08-21T09:30:00" }],
      JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
}

for (const position of ["append", "prepend", "middle"]) {
  test(`pane text is extracted when it renders at: ${position}`, async () => {
    const out = await runPositionalPage(position);
    const d = out.details[0];
    assert.ok(d, `nothing extracted for position=${position}`);
    assert.match(d.joinUrl, /webex\.com/,
      `the pasted URL was lost when the pane rendered at ${position}`);
    assert.deepEqual([...d.attendees], ["a.doe@globex.example"]);
    assert.match(d.body, /agenda text/);
    assert.ok(!d.body.includes("Calendar grid"),
      "grid text leaked into the body");
  });
}

test("both POST paths declare the capture diagnostics", () => {
  // Field incident, twice: capture_diag rode only the alarm path, so
  // every capture the user triggered THEMSELVES — the popup button
  // they press precisely when investigating — reported nothing, and
  // three diagnostic zips in a row carried a stale alarm-path diag
  // while the runs under investigation were invisible. Source guard:
  // both payload builders must attach it via the one shared builder.
  const src = fs.readFileSync(BG_PATH, "utf8");
  const count = (src.match(/payload\.capture_diag = buildCaptureDiag\(/g) || []).length;
  assert.equal(count, 2,
    "capture_diag must be attached on BOTH the calendar-only and the "
    + "full Capture & Send paths, through the shared builder");
});

// ── Serve the user's next meeting first (v1.15) ──────────────────────
//
// FIELD ARITHMETIC, 2026-08-20 22:07 capture: 23 events queued for
// clicking, 11 opened, then 12 consecutive tile misses — and the
// meeting the user expands every time (tomorrow morning) was among the
// missed. Two causes, both structural:
//
//   * the click list had an upper time bound only, so the PAST week's
//     finished meetings queued ahead of tomorrow's and burned the
//     opens on detail nobody would read;
//   * one stuck pane hid the grid, and every later tile then "did not
//     exist" — a cascade with exactly the 11-then-12 signature.

test("finished meetings do not compete for clicks; soonest comes first", async () => {
  // Drive collectWeekDetail's filter indirectly through the reader:
  // build the needing list the way captureCalendarTab does and assert
  // order and membership. The filter lives in background.js, so this
  // exercises the real code path via a scripted capture.
  const src = fs.readFileSync(BG_PATH, "utf8");
  // Source-level pins for the two properties (no DOM needed):
  assert.match(src, /tEnd < floor/,
    "the click list must exclude meetings that already ended");
  assert.match(src,
    /needing\.sort\(\(a, b\) => Date\.parse\(a\.start\) - Date\.parse\(b\.start\)\)/,
    "the click list must be ordered soonest-first");
});

test("a stuck pane does not cascade into misses for every later event", async () => {
  // Tile A opens a pane that hides ALL tiles until Escape. The old
  // code found no tile for B and gave up; recovery must Escape,
  // re-scan, and open B.
  let paneOpen = false;
  const mk = (label, revealText) => ({
    getAttribute: (n) => (n === "aria-label" ? label : null),
    click() { paneOpen = true; this._revealed = revealText; page._current = revealText; },
  });
  const page = {
    _current: "",
    body: {
      get innerText() {
        return "Baseline calendar text." + (paneOpen ? "\n" + page._current : "");
      },
    },
    querySelectorAll: (sel) => {
      if (String(sel).includes("a[href]")) return [];
      // THE cascade condition: while a pane is open, the grid (and its
      // tiles) is not in the accessibility tree at all.
      if (paneOpen) return [];
      return [
        mk("Alpha, 9:00 AM to 9:30 AM", "Alpha agenda body text here."),
        mk("Beta, 10:00 AM to 10:30 AM", "Beta agenda body text here."),
      ];
    },
    // A STICKY pane: the first Escape (the loop's routine close) is
    // swallowed — real OWA full-page event views do exactly this — and
    // only a second Escape closes it. The old code sent one Escape per
    // event and never retried a missing tile, so Beta was lost; the
    // recovery path's extra Escape is what must save it.
    _esc: 0,
    dispatchEvent: () => {
      page._esc += 1;
      if (page._esc >= 2) paneOpen = false;
      return true;
    },
  };
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = page;
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    const out = await sandbox._readEventDetailsFunc(
      [{ subject: "Alpha", startIso: "2026-08-21T09:00:00" },
       { subject: "Beta", startIso: "2026-08-21T10:00:00" }],
      JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
    assert.equal(out.opened, 2,
      "the second event was lost to the stuck-pane cascade");
    assert.ok(out.details.find((d) => d.subject === "Beta"),
      "Beta's detail missing — recovery did not re-find its tile");
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
});

test("every wanted event comes back with a named outcome", async () => {
  // The per-meeting status is what lets the app say WHY a meeting has
  // no detail. Every wanted event must resolve to exactly one status.
  const out = await runRichDetailReader({
    tiles: [{ subject: "Present", label: "Present, 9:00 AM to 9:30 AM" }],
    reveal: { "Present": { text: "An agenda that is long enough to count." } },
    wanted: [
      { subject: "Present", startIso: "2026-08-21T09:00:00" },
      { subject: "Ghost", startIso: "2026-08-21T10:00:00" },  // no tile
    ],
  });
  const byStatus = Object.fromEntries(
    (out.statuses || []).map((s) => [s.subject, s.status]));
  assert.equal(byStatus["Present"], "opened");
  assert.equal(byStatus["Ghost"], "no_tile");
});

// ── the invite body lives one frame deeper ───────────────────────────
//
// FIELD SCREENSHOT 2026-08-21 (v2.57.0). A real meeting's captured
// "agenda" read, in full:
//
//     Join / Chat / Fri 8/21/2026 10:00 AM - 11:00 AM /
//     No location added / GG / <organizer> invited you.
//
// The pane's own chrome. Not one word of the invite, and no join URL —
// both of which were on screen. Outlook renders the invite body in a
// same-origin IFRAME and parts of the card in SHADOW ROOTS;
// `document.body.innerText` and `document.querySelectorAll` stop at
// both boundaries. The grid scanner has always crossed them. The
// detail reader — whose entire job is reading that pane — did not.

function fakeDetailPageFramed({ tiles, reveal }) {
  const base = "Calendar grid baseline text that is already on screen.";
  let chrome = "";
  let frameText = "";
  let frameHrefs = [];
  let shadowHrefs = [];
  const mkAttr = (attrs) => ({
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
  });

  // The message-body iframe: its own document, unreachable from the
  // top document's queries.
  const frameDoc = {
    body: { get innerText() { return frameText; } },
    querySelectorAll: (sel) => (String(sel).includes("a[href]")
      ? frameHrefs.map((h) => mkAttr({ href: h }))
      : []),
  };
  const frameEl = { getAttribute: () => null, contentDocument: frameDoc };
  // A shadow host whose root holds the Join anchor (Teams' shape).
  const shadowRoot = {
    querySelectorAll: (sel) => (String(sel).includes("a[href]")
      ? shadowHrefs.map((h) => mkAttr({ href: h }))
      : []),
    textContent: "",
  };
  const shadowHost = { getAttribute: () => null, shadowRoot };

  const els = tiles.map((t) => ({
    getAttribute: (n) => (n === "aria-label" ? t.label : null),
    click() {
      const r = reveal[t.subject] || {};
      chrome = "\n" + (r.chrome || "");
      frameText = r.frameText || "";
      frameHrefs = r.frameHrefs || [];
      shadowHrefs = r.shadowHrefs || [];
    },
  }));

  return {
    body: { get innerText() { return base + chrome; } },
    querySelectorAll: (sel) => {
      const s = String(sel);
      if (s.includes("iframe")) return [frameEl];
      if (s === "*") return [shadowHost];
      if (s.includes("a[href]")) return [];
      return els;
    },
    dispatchEvent: () => {
      chrome = ""; frameText = ""; frameHrefs = []; shadowHrefs = [];
      return true;
    },
  };
}

async function runFramedDetailReader({ tiles, reveal, wanted }) {
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = fakeDetailPageFramed({ tiles, reveal });
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    return await sandbox._readEventDetailsFunc(
      wanted, JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
}

test("the invite body inside the message iframe is read, not the pane chrome",
     async () => {
  const out = await runFramedDetailReader({
    tiles: [{ subject: "Follow Up Session",
              label: "Follow Up Session, 10:00 AM to 11:00 AM" }],
    reveal: {
      "Follow Up Session": {
        // Exactly the shape the field screenshot showed.
        chrome: "Join\nChat\nFri 8/21/2026 10:00 AM - 11:00 AM\n"
                + "No location added\nJP\nJordan Poe invited you.",
        frameText: "Agenda: walk the final requirements list.\n"
                   + "jordan.poe@example.com\n"
                   + "Join the Webex meeting: https://acme.webex.com/meet/jordan.poe",
      },
    },
    wanted: [{ subject: "Follow Up Session", startIso: "2026-08-21T10:00:00" }],
  });
  const d = out.details[0];
  assert.ok(d, "the pane produced no detail at all");
  assert.match(d.body, /Agenda: walk the final requirements list/,
               "the invite body was never read out of the iframe");
  assert.equal(d.joinUrl, "https://acme.webex.com/meet/jordan.poe",
               "the pasted join URL was one frame deeper than the scan");
  assert.ok(d.attendees.includes("jordan.poe@example.com"),
            "addresses in the iframe body were not collected");
});

test("a Join anchor inside a shadow root is found", async () => {
  // Teams renders Join as a button whose anchor sits in a shadow root;
  // every field diagnostic to date reported joinFromAnchor: 0.
  const teamsUrl = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_X%40thread.v2/0";
  const out = await runFramedDetailReader({
    tiles: [{ subject: "Daily Pulse", label: "Daily Pulse, 9:30 AM to 9:45 AM" }],
    reveal: {
      "Daily Pulse": {
        chrome: "Join\nChat\nNo location added",
        frameText: "Daily sync on the rollout blockers.",
        shadowHrefs: [teamsUrl],
      },
    },
    wanted: [{ subject: "Daily Pulse", startIso: "2026-08-21T09:30:00" }],
  });
  const d = out.details[0];
  assert.ok(d, "the pane produced no detail at all");
  assert.equal(d.joinUrl, teamsUrl,
               "the Teams anchor in the shadow root was not found");
  assert.equal(out.joinFromAnchor, 1);
});

// ── the join URL, without assuming the button's shape ────────────────
//
// Three releases assumed a shape for the Join control and were wrong
// three times: 1.10 assumed text (Teams renders a button), 1.15
// assumed the button was an anchor, 1.16 assumed the anchor was
// reachable through open shadow roots and same-origin frames. The
// field answer never changed: no link.
//
// A join URL is a highly specific string, and if the meeting has one
// it is SOMEWHERE in the markup the click produced. The markup scan
// keeps the same safety rule the anchor scan used — it must have
// appeared WITH this meeting — on a surface that does not care how
// Outlook renders a button this quarter.

function fakeDetailPageMarkup({ tiles, reveal }) {
  const base = "Calendar grid baseline text that is already on screen.";
  let text = "";
  let markup = "";
  const els = tiles.map((t) => ({
    getAttribute: (n) => (n === "aria-label" ? t.label : null),
    click() {
      const r = reveal[t.subject] || {};
      text = "\n" + (r.text || "");
      markup = r.markup || "";
    },
  }));
  return {
    // No anchors anywhere, ever — the point of this fake.
    documentElement: { get innerHTML() { return markup; } },
    body: { get innerText() { return base + text; } },
    querySelectorAll: (sel) => {
      const s = String(sel);
      if (s.includes("a[href]") || s.includes("iframe") || s === "*") return [];
      return els;
    },
    dispatchEvent: () => { text = ""; markup = ""; return true; },
  };
}

async function runMarkupDetailReader({ tiles, reveal, wanted }) {
  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = fakeDetailPageMarkup({ tiles, reveal });
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    return await sandbox._readEventDetailsFunc(
      wanted, JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
}

test("a Join BUTTON with the URL in an attribute still yields the link",
     async () => {
  const url = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_ZZ%40thread.v2/0";
  const out = await runMarkupDetailReader({
    tiles: [{ subject: "Pulse", label: "Pulse, 9:30 AM to 9:45 AM" }],
    reveal: {
      "Pulse": {
        text: "Join\nChat\nNo location added\nDaily status sync.",
        // A button. No <a> in the document at all.
        markup: `<button data-join-url="${url}">Join</button>`,
      },
    },
    wanted: [{ subject: "Pulse", startIso: "2026-08-21T09:30:00" }],
  });
  const d = out.details[0];
  assert.ok(d, "the pane produced no detail at all");
  assert.equal(d.joinUrl, url,
               "the URL was in the markup and was not recovered");
  assert.equal(out.joinFromMarkup, 1);
});

test("a join URL escaped inside an inline JSON blob is recovered", async () => {
  // The card is hydrated from JSON in the markup; the URL is
  // HTML-escaped and percent-encoded, as it arrives in practice.
  const out = await runMarkupDetailReader({
    tiles: [{ subject: "Sync", label: "Sync, 1:00 PM to 1:30 PM" }],
    reveal: {
      "Sync": {
        text: "Join\nProject sync.",
        markup: '<script type="application/json">'
          + '{"joinUrl":"https%3A%2F%2Facme.webex.com%2Fmeet%2Fjordan.poe'
          + '?a=1&amp;b=2"}</script>',
      },
    },
    wanted: [{ subject: "Sync", startIso: "2026-08-21T13:00:00" }],
  });
  const d = out.details[0];
  assert.ok(d, "the pane produced no detail at all");
  assert.match(d.joinUrl, /acme\.webex\.com\/meet\/jordan\.poe/);
});

test("a join URL already in the markup before the click is not attributed",
     async () => {
  // The previous meeting's Join button is still rendered. Attaching it
  // here would send the user into the wrong call.
  const stale = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_STALE%40thread.v2/0";
  const page = fakeDetailPageMarkup({
    tiles: [{ subject: "Second", label: "Second, 11:00 AM to 11:30 AM" }],
    reveal: { "Second": { text: "Some agenda text", markup: `<button data-join-url="${stale}">Join</button>` } },
  });
  // Present BEFORE the click too.
  const origHtml = Object.getOwnPropertyDescriptor(
    page.documentElement, "innerHTML").get;
  Object.defineProperty(page.documentElement, "innerHTML", {
    get() { return `<button data-join-url="${stale}">Join</button>` + origHtml.call(this); },
  });

  const prevDoc = sandbox.document;
  const prevKE = sandbox.KeyboardEvent;
  const prevST = sandbox.setTimeout;
  sandbox.document = page;
  sandbox.KeyboardEvent = function () {};
  sandbox.setTimeout = setTimeout;
  try {
    const out = await sandbox._readEventDetailsFunc(
      [{ subject: "Second", startIso: "2026-08-21T11:00:00" }],
      JOIN_PATTERNS_FROM_SOURCE, 25, 90000);
    const d = out.details[0];
    if (d) assert.equal(d.joinUrl, "",
                        "a pre-existing join URL was attributed");
  } finally {
    sandbox.document = prevDoc;
    sandbox.KeyboardEvent = prevKE;
    sandbox.setTimeout = prevST;
  }
});

// ── the signed-out session stops being invisible ─────────────────────
//
// FIELD DIAGNOSTICS 2026-08-23. Friday evening: four structured
// captures in a row, 51 events each. Saturday morning onward: OWA tab
// 0 chars, Inbox 0 chars, recorder never installed, 0 candidates, 0
// responses — while the Teams tabs kept reading ~2,400 chars. The
// browser's Outlook session had expired: the background tab bounced
// to a sign-in origin the extension has no permission to script, so
// every read failed in a way indistinguishable from "the calendar was
// empty", and the app silently served Friday's stale detail for two
// days.
//
// The capture now classifies where the tab LANDED, and the flag rides
// the existing diag to the app.

test("sign-in origins are recognized, Outlook origins are not", () => {
  for (const url of [
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?x=1",
    "https://login.live.com/login.srf",
    "https://login.microsoft.com/anything",
    "https://account.microsoft.com/",
    "https://sts.login.windows.net/whatever",
  ]) {
    assert.equal(sandbox.isSignInUrl(url), true, url);
  }
  for (const url of [
    "https://outlook.office.com/calendar/view/week",
    "https://outlook.cloud.microsoft/mail/",
    "https://teams.microsoft.com/v2/",
    // Suffix matching, never substring: a hostile lookalike host must
    // not count as a sign-in page.
    "https://login.microsoftonline.com.evil.example/phish",
    "https://notlogin.live.com.evil.example/",
    "not a url at all",
    "",
  ]) {
    assert.equal(sandbox.isSignInUrl(url), false, url);
  }
});

test("the capture stamps authRedirect into the diag it POSTs", () => {
  // Source pin: the wiring from tab-landed-URL to the diag object the
  // backend stores. Without this line the classification exists and
  // never leaves the extension — the exact counters-kept-invisible
  // defect record_capture_diag documents.
  const src = fs.readFileSync(BG_PATH, "utf8");
  assert.match(src, /diag\.authRedirect\s*=\s*isSignInUrl\(/);
  assert.match(src, /diag\.calendarUnreadable\s*=/);
});

// ── the join URL inside the body's HTML, and the diag drop-list ──────
//
// FIELD CAPTURE 2026-08-23 12:19, the first healthy run after the
// signed-out weekend: 18 responses matched, 10 invite bodies gained,
// detailGainedJoinUrl 0. A Teams invite's join URL lives in the body
// HTML as the href of "Join the meeting now" — and _stripHtml removed
// every tag, href included, BEFORE the body was searched. The link
// was captured on all 10 meetings and deleted during cleanup.

test("a Teams join URL living only in the body's href is recovered", () => {
  const html = '<div>You have been invited.</div>'
    + '<a href="https://teams.microsoft.com/l/meetup-join/'
    + '19%3ameeting_QQ%40thread.v2/0?context=%7b%22Tid%22%3a%22t%22%7d">'
    + 'Join the meeting now</a><div>Meeting ID: 000 000 000</div>';
  const diag = {};
  const found = sandbox.detailsFromResponses([{
    Items: [{
      Subject: "Daily Pulse",
      Start: "2026-08-24T09:30:00",
      Body: { Content: html },
    }],
  }], diag);
  const d = found.get(sandbox.detailKey("Daily Pulse", "2026-08-24T09:30:00"));
  assert.ok(d, "the item never matched");
  assert.match(d.joinUrl, /^https:\/\/teams\.microsoft\.com\/l\/meetup-join\//,
               "the href inside the body HTML was stripped before the "
               + "body was searched");
  assert.equal(diag.joinFromResponseBody, 1);
  // The stripped body still reads as text, without the URL re-stated.
  assert.match(d.body, /You have been invited/);
});

test("an explicit join field still wins over the body scan", () => {
  const found = sandbox.detailsFromResponses([{
    Items: [{
      Subject: "Sync",
      Start: "2026-08-24T13:00:00",
      OnlineMeeting: { JoinUrl: "https://teams.microsoft.com/l/meetup-join/REAL/0" },
      Body: { Content: '<a href="https://zoom.us/j/999">stale forwarded link</a>' },
    }],
  }], {});
  const d = found.get(sandbox.detailKey("Sync", "2026-08-24T13:00:00"));
  assert.equal(d.joinUrl, "https://teams.microsoft.com/l/meetup-join/REAL/0");
});

test("the response key census reports key groups as booleans", () => {
  const diag = {};
  sandbox.detailsFromResponses([{
    Items: [{
      Subject: "S", Start: "2026-08-24T09:00:00",
      Attendees: [{ Name: "Jordan Poe" }],
      Body: { Content: "plain" },
    }],
  }], diag);
  assert.equal(diag.respHadAttendeesKey, true);
  assert.equal(diag.respHadBodyKey, true);
  assert.ok(!("respHadJoinKey" in diag) || diag.respHadJoinKey === false);
});

test("buildCaptureDiag passes through every scalar the capture recorded",
     () => {
  // The whitelist it used to be ate joinFromMarkup (1.17.0) and the
  // authRedirect/calendarUnreadable flags (1.18.0) — the drop-list
  // constructor, in the diagnostics channel itself.
  const out = sandbox.buildCaptureDiag({
    events: [1, 2, 3],
    diag: {
      joinFromMarkup: 4,
      joinFromResponseBody: 2,
      authRedirect: true,
      calendarUnreadable: false,
      someFutureCounter: 7,
      aString: "must not pass",
      aNested: { no: 1 },
    },
  });
  assert.equal(out.joinFromMarkup, 4);
  assert.equal(out.joinFromResponseBody, 2);
  assert.equal(out.authRedirect, true);
  assert.equal(out.calendarUnreadable, false);
  assert.equal(out.someFutureCounter, 7);
  assert.equal(out.eventsExtracted, 3);
  assert.ok(!("aString" in out));
  assert.ok(!("aNested" in out));
});

test("the click pass's markup counter reaches the capture diag", () => {
  const src = fs.readFileSync(BG_PATH, "utf8");
  assert.match(src, /diag\.joinFromMarkup\s*\+=\s*got\.joinFromMarkup/,
               "joinFromMarkup dies at the collectWeekDetail boundary");
});

// ── unmatched responses report their shape instead of hiding it ──────
//
// FIELD RUN 2026-08-23 13:52 (1.19.0): 179 responses seen, 19 matched
// by the recorder's gate — and detailsFromResponses recognized ZERO
// items, so the body-href scan never ran and every census flag stayed
// absent. The tenant's new Outlook stack uses key names this parser
// doesn't know, and finding them by guessing costs one full release
// cycle per guess. The payload we cannot read is in our hands: it can
// simply tell us its key names.

test("unmatched responses yield a key-name census and a join-URL boolean",
     () => {
  const diag = {};
  const found = sandbox.detailsFromResponses([{
    // A shape the parser does NOT recognize: no subject/start aliases.
    data: {
      calendarEvents: [{
        eventTitle: "Pulse",
        eventWindow: { begin: "2026-08-24T09:30:00" },
        htmlPayload: '<a href="https://teams.microsoft.com/l/meetup-join/19%3aX/0">Join</a>',
      }],
    },
  }], diag);
  assert.equal(found.size, 0);
  assert.ok(Array.isArray(diag.responseKeyNames), "no census emitted");
  for (const k of ["data", "calendarevents", "eventtitle", "eventwindow",
                   "begin", "htmlpayload"]) {
    assert.ok(diag.responseKeyNames.includes(k), `census missing ${k}`);
  }
  // Values never leak: the census is names only.
  assert.ok(!diag.responseKeyNames.some((n) => n.includes("pulse")));
  assert.equal(diag.responsesContainJoinShapedUrl, true,
               "the join URL is IN the captured payload and the diag "
               + "must say so");
});

test("matched responses emit no census (payload stays small)", () => {
  const diag = {};
  sandbox.detailsFromResponses([{
    Items: [{ Subject: "S", Start: "2026-08-24T09:00:00" }],
  }], diag);
  assert.ok(!("responseKeyNames" in diag));
});

test("buildCaptureDiag passes the bounded census list through", () => {
  const out = sandbox.buildCaptureDiag({
    events: [],
    diag: {
      responseKeyNames: ["subject", "start"],
      otherList: ["subject"],
      smuggled: ["a.doe@globex.example"],
    },
  });
  assert.deepEqual(out.responseKeyNames, ["subject", "start"]);
  // Lists under any OTHER key stay behind — an address or URL cannot
  // ride a list through this door.
  assert.ok(!("otherList" in out));
  assert.ok(!("smuggled" in out));
});

test("the post-click parser call carries the diag (mechanism 3)", () => {
  // Field runs 2026-08-23: the bodies arrive WITH the clicks
  // (postClickBodies 13), and this was the one call site without the
  // diag — census, key flags and joinFromResponseBody all silently
  // died on the richest payloads in the pipeline.
  const src = fs.readFileSync(BG_PATH, "utf8");
  assert.match(src,
    /detailsFromResponses\(capturedBodies\.slice\(before\),\s*diag\)/);
});

test("the census accumulates across calls instead of overwriting", () => {
  const diag = {};
  sandbox.detailsFromResponses([{ shapeOne: { alpha: 1 } }], diag);
  sandbox.detailsFromResponses([{ shapeTwo: { beta: 2 } }], diag);
  for (const k of ["shapeone", "alpha", "shapetwo", "beta"]) {
    assert.ok(diag.responseKeyNames.includes(k), `lost ${k}`);
  }
});
