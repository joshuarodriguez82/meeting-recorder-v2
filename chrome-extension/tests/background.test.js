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
