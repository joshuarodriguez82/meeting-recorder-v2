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
