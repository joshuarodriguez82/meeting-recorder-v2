#!/usr/bin/env node
// Boots a headless Chromium page against a running `next dev` server,
// navigates to a named view (and optionally a sub-tab within it), and
// writes a screenshot. See README.md for prerequisites and gotchas.
//
// Usage:
//   node screenshot.mjs <view> [outFile] [options]
//
//   <view>     Visible text of the top-level nav button to click
//              (e.g. "Record", "Sessions", "Clients", "Settings").
//              Pass "" (empty string) to screenshot whatever the app
//              lands on by default, with no nav click.
//   [outFile]  Screenshot path. Default: ./<view-or-"home">.png
//
// Options:
//   --base-url=<url>   App URL. Default http://localhost:3000
//   --sub-tab=<text>   After the main nav click, click a second button
//                       by its visible text (e.g. "Data & Diagnostics"
//                       inside Settings).
//   --scroll=<px>      scrollTop to set on the view's scroll container
//                       (see measure.mjs for how the container is
//                       found) before the screenshot.
//   --wait=<ms>         Initial wait for first paint + data fetch.
//                       Default 9000 — the app's first-run tour and
//                       initial /settings + /sessions fetches are slow
//                       enough that a shorter wait screenshots a
//                       loading state instead of the real layout.
//   --settle=<ms>       Wait after each nav click before the shot.
//                       Default 2000.
//
// Example:
//   node screenshot.mjs Settings settings.png --scroll=700
//   node screenshot.mjs Settings settings-diag.png \
//       --sub-tab="Data & Diagnostics"

import { chromium } from 'playwright-core';

function parseArgs(argv) {
  const positional = [];
  const opts = {};
  for (const a of argv) {
    if (a.startsWith('--')) {
      const [k, ...rest] = a.slice(2).split('=');
      opts[k] = rest.length ? rest.join('=') : true;
    } else {
      positional.push(a);
    }
  }
  return { positional, opts };
}

const { positional, opts } = parseArgs(process.argv.slice(2));
const view = positional[0] ?? '';
const outFile = positional[1] ?? `${view || 'home'}.png`;
const baseUrl = opts['base-url'] ?? 'http://localhost:3000';
const waitMs = Number(opts.wait ?? 9000);
const settleMs = Number(opts.settle ?? 2000);
const scrollPx = opts.scroll != null ? Number(opts.scroll) : null;
const subTab = opts['sub-tab'] ?? null;

// The app renders a first-run tour overlay (`div.fixed.inset-0`) that
// sits on top of everything and swallows clicks — including
// Playwright's own actionability-checked .click(), which waits for the
// target to be "not obscured" and then times out because the overlay
// never goes away on its own in a fresh headless profile. Removing it
// via a DOM query first, then clicking via a plain DOM .click() (not
// page.locator(...).click()), sidesteps that entirely.
async function clickButtonByText(page, text) {
  return page.evaluate((label) => {
    document.querySelectorAll('div.fixed.inset-0').forEach((d) => d.remove());
    const btn = [...document.querySelectorAll('button')]
      .find((b) => b.textContent.trim() === label);
    if (!btn) return false;
    btn.click();
    return true;
  }, text);
}

async function main() {
  const browser = await chromium.launch({
    executablePath: '/opt/pw-browsers/chromium',
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 880 } });
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(waitMs);
    // Clear the tour overlay even if we're not navigating anywhere —
    // otherwise a "home view" screenshot shows the overlay, not the app.
    await page.evaluate(() =>
      document.querySelectorAll('div.fixed.inset-0').forEach((d) => d.remove()));

    if (view) {
      const clicked = await clickButtonByText(page, view);
      if (!clicked) {
        throw new Error(`No button with text "${view}" found`);
      }
      await page.waitForTimeout(settleMs);
      await page.evaluate(() =>
        document.querySelectorAll('div.fixed.inset-0').forEach((d) => d.remove()));
    }

    if (subTab) {
      const clicked = await clickButtonByText(page, subTab);
      if (!clicked) {
        throw new Error(`No sub-tab button with text "${subTab}" found`);
      }
      await page.waitForTimeout(settleMs);
      await page.evaluate(() =>
        document.querySelectorAll('div.fixed.inset-0').forEach((d) => d.remove()));
    }

    if (scrollPx != null) {
      const result = await page.evaluate((px) => {
        const el = [...document.querySelectorAll('div')].find(
          (d) => d.scrollHeight > d.clientHeight + 50
            && getComputedStyle(d).overflowY === 'auto');
        if (!el) return 'no scroller found';
        el.scrollTop = px;
        return `scrolled to ${el.scrollTop}`;
      }, scrollPx);
      console.error(result);
      await page.waitForTimeout(600);
    }

    await page.screenshot({ path: outFile });
    console.log(`wrote ${outFile}`);
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
