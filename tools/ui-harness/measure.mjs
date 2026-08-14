#!/usr/bin/env node
// Measures real layout geometry (getBoundingClientRect) of a view's
// scroll container and any `position: sticky` elements inside it —
// the numbers a screenshot can't give you directly (exact overlap in
// px, computed padding, sticky offsets). Prints JSON to stdout.
//
// Built after shipping two blind CSS fixes for a sticky-header overlap
// bug that were both wrong — see README.md for why this exists.
//
// Usage:
//   node measure.mjs <view> [options]
//
//   <view>   Visible text of the top-level nav button to click.
//            Pass "" to measure whatever the app lands on by default.
//
// Options:
//   --base-url=<url>   App URL. Default http://localhost:3000
//   --sub-tab=<text>   Click a second button (by text) after <view>.
//   --scroll=<px>      scrollTop to set on the found scroll container
//                       before measuring. Default 700.
//   --wait=<ms>         Initial wait for first paint + data fetch.
//                       Default 9000.
//   --settle=<ms>       Wait after each nav click. Default 2000.
//
// Example:
//   node measure.mjs Settings --scroll=700

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
const baseUrl = opts['base-url'] ?? 'http://localhost:3000';
const waitMs = Number(opts.wait ?? 9000);
const settleMs = Number(opts.settle ?? 2000);
const scrollPx = Number(opts.scroll ?? 700);
const subTab = opts['sub-tab'] ?? null;

// See screenshot.mjs for why this is a DOM .click(), not
// page.locator(...).click(): the first-run tour overlay
// (div.fixed.inset-0) intercepts Playwright's actionability-checked
// click and times out.
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
    await page.evaluate(() =>
      document.querySelectorAll('div.fixed.inset-0').forEach((d) => d.remove()));

    if (view) {
      if (!(await clickButtonByText(page, view))) {
        throw new Error(`No button with text "${view}" found`);
      }
      await page.waitForTimeout(settleMs);
      await page.evaluate(() =>
        document.querySelectorAll('div.fixed.inset-0').forEach((d) => d.remove()));
    }

    if (subTab) {
      if (!(await clickButtonByText(page, subTab))) {
        throw new Error(`No sub-tab button with text "${subTab}" found`);
      }
      await page.waitForTimeout(settleMs);
      await page.evaluate(() =>
        document.querySelectorAll('div.fixed.inset-0').forEach((d) => d.remove()));
    }

    const out = await page.evaluate((px) => {
      const scroller = [...document.querySelectorAll('div')].find(
        (d) => d.scrollHeight > d.clientHeight + 50
          && getComputedStyle(d).overflowY === 'auto');
      if (!scroller) return { error: 'no scroll container found' };

      scroller.scrollTop = px;
      const sr = scroller.getBoundingClientRect();
      const cs = getComputedStyle(scroller);
      const sticky = [...scroller.querySelectorAll('div')]
        .filter((d) => getComputedStyle(d).position === 'sticky');

      return {
        scrollTop: scroller.scrollTop,
        scroller: {
          top: sr.top, bottom: sr.bottom, left: sr.left, right: sr.right,
          padTop: cs.paddingTop, padBottom: cs.paddingBottom,
          padLeft: cs.paddingLeft, padRight: cs.paddingRight,
        },
        sticky: sticky.map((d) => {
          const r = d.getBoundingClientRect();
          const c = getComputedStyle(d);
          return {
            top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            stickTop: c.top, stickBottom: c.bottom, bg: c.backgroundColor,
            text: d.innerText.slice(0, 40).replace(/\n/g, ' '),
          };
        }),
      };
    }, scrollPx);

    console.log(JSON.stringify(out, null, 1));
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
