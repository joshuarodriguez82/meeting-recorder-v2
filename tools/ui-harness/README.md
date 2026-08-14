# UI harness

Drives the real Next.js app in a real (headless) Chromium browser
against a stub backend, so a layout change can be checked with actual
rendered pixels and actual `getBoundingClientRect()` numbers instead of
guessing from the CSS diff.

## Why this exists

Two consecutive CSS fixes for a sticky-header overlap bug shipped
blind — reasoning from the stylesheet, no real browser in the loop —
and both were wrong. The real backend can't run in this container (it
needs torch/pyannote and platform audio/calendar APIs), so there was no
cheap way to get the actual app on screen to check before shipping a
third guess. This harness is that cheap way: a stdlib stub server that
satisfies the frontend's health gate and initial-load fetches, plus two
small Playwright scripts that navigate to a view and either screenshot
it or measure its layout geometry directly.

This is a tool for a human or an agent to run by hand when touching
layout — it is **not** wired into CI. A flaky screenshot job would be
worse than no job; see the note at the bottom.

## Prerequisites

- Chromium is preinstalled at `/opt/pw-browsers/chromium` (both scripts
  point `chromium.launch()` at it directly — no `npx playwright
  install` needed or wanted).
- `playwright-core` is this directory's only npm dependency. Install it
  **locally, inside `tools/ui-harness/`** — run `npm install` from
  *this* directory, not the repo root. It must never be added to the
  app's root `package.json`: this tool is not part of the shipped app.
- The app's own `node_modules` at the repo root (for `next dev`).
- Python 3 (stdlib only) for the stub backend.

## Running it

Three processes, each in its own terminal (or background job):

```sh
# 1. Stub backend — satisfies the frontend's health gate + initial fetches.
python3 tools/ui-harness/stub.py
# → "ui-harness stub backend listening on http://127.0.0.1:17645 (pid NNNN)"

# 2. The real app, pointed at nothing but the stub above.
npm run dev
# → Local: http://localhost:3000 (or the next free port if 3000 is busy —
#   check the printed URL and pass --base-url below if it isn't 3000)

# 3. From tools/ui-harness/, with its own node_modules installed:
cd tools/ui-harness
npm install    # first time only

node screenshot.mjs Settings settings.png --scroll=700
node measure.mjs Settings --scroll=700
```

The frontend finds the stub with **zero configuration**: outside the
Tauri shell, `src/lib/api.ts`'s `getBaseUrl()` falls back to
`http://127.0.0.1:17645` — the exact port the stub listens on — so a
plain `next dev` browser session talks to it automatically.

### screenshot.mjs

```
node screenshot.mjs <view> [outFile] [options]
```

Boots a page, clicks the nav button whose visible text matches
`<view>` (e.g. `Record`, `Sessions`, `Clients`, `Settings`), optionally
clicks a sub-tab, optionally scrolls the view's scroll container, and
writes a screenshot. Pass `""` for `<view>` to screenshot whatever the
app lands on with no nav click. Run `node screenshot.mjs` with no args
to see the full option list (`--base-url`, `--sub-tab`, `--scroll`,
`--wait`, `--settle`).

### measure.mjs

```
node measure.mjs <view> [options]
```

Same navigation as `screenshot.mjs`, but instead of a screenshot it
prints JSON with the exact `getBoundingClientRect()` of the view's
scroll container and every `position: sticky` element inside it —
top/bottom/left/right, computed padding, sticky offsets, background
color. This is what actually caught the sticky-overlap bug: the
screenshot alone didn't make the overlap obvious at a glance, but the
numbers did (a sticky footer's `top` less than the header's `bottom`
is an overlap, in black and white).

## Gotchas (each one cost real time)

- **Killing the stub**: `pkill -f stub.py` also matches the invoking
  shell's own command line (it contains the string `stub.py` too), so
  it can kill your own shell instead of the stub. Note the PID the
  stub prints on startup and `kill <pid>` instead.
- **curl to localhost**: this environment's proxy config intercepts
  plain `curl http://127.0.0.1:...` — pass `--noproxy '*'`, e.g.
  `curl -sS --noproxy '*' http://127.0.0.1:17645/health`.
- **The first-run tour overlay**: the app renders a `div.fixed
  inset-0` overlay on first load that sits on top of everything and
  intercepts clicks — including Playwright's own actionability-checked
  `page.locator(...).click()`, which waits for the target to become
  "not obscured" and times out because the overlay doesn't go away on
  its own in a fresh headless profile. Both scripts route navigation
  clicks through `page.evaluate()` and a plain DOM `.click()` instead,
  after removing any `div.fixed.inset-0` nodes first. Note that this
  only unblocks the *click* — React can still re-mount the overlay on
  a later render, so it may still be visible in the final screenshot;
  that's expected, not a harness bug.

## Not wired into CI

Deliberately. A real-browser layout check is exactly the kind of test
that flakes on timing (dev-server cold start, first paint, tour
overlay animation) in ways a CI runner's environment can make worse,
not better. Landing a flaky screenshot job would train everyone to
ignore red CI, which is worse than not having the check at all. Run it
by hand when touching layout; promote it to CI later if it proves
reliable enough over time.
