/**
 * The Activity panel's Clear button must stay clickable.
 *
 * REPORTED TWICE (2026-09-02, 2026-09-10)
 * ---------------------------------------
 * With a dozen events queued, the list rendered at its full height,
 * painted over the Clear button below it, and swallowed the clicks —
 * so Clear "didn't work". It was never a handler problem; the button
 * was underneath the list.
 *
 * The first fix bounded the panel with flex (`min-h-0 flex-1` on a
 * ScrollArea). That held for one report and not the next, because our
 * ScrollArea wrapper gives its viewport `size-full` — height: 100% —
 * and no overflow of its own. Whether it scrolls therefore depends on
 * a percentage height resolving through a flex item, which is exactly
 * the kind of thing that works until a sibling's styles change.
 *
 * These assertions are on the markup rather than on rendered geometry:
 * jsdom does no layout, so a render test here would pass no matter
 * what. What they pin is the PROPERTY that makes the panel bounded
 * without depending on percentage resolution — an explicit max-height
 * and an explicit overflow on the same element.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SOURCE = readFileSync(
  join(process.cwd(), "src/components/activity-center.tsx"), "utf8");

/** The element wrapping the event list. */
function listContainerClasses(): string {
  // The list container is the div immediately before the `px-3 py-2`
  // inner wrapper that holds the events.
  const m = SOURCE.match(/<div className="([^"]*overflow-y-auto[^"]*)">\s*\n\s*<div className="px-3 py-2">/);
  return m ? m[1] : "";
}

describe("activity panel bounding", () => {
  it("scrolls the list with an explicit overflow", () => {
    expect(listContainerClasses()).toContain("overflow-y-auto");
  });

  it("caps the list height without relying on a percentage", () => {
    // A max-height on the SAME element as the overflow is what makes
    // it a scroll container regardless of how its parent is sized.
    expect(listContainerClasses()).toMatch(/max-h-\S+/);
  });

  it("does not size the list with flex-1, which needs the parent to resolve", () => {
    expect(listContainerClasses()).not.toContain("flex-1");
  });

  it("keeps the popover clipped so nothing can escape it", () => {
    expect(SOURCE).toMatch(/PopoverContent[\s\S]{0,200}overflow-hidden/);
  });

  it("puts Clear in a shrink-0 footer with its own background", () => {
    // shrink-0 stops the footer being compressed to nothing by a long
    // list; bg-popover stops anything showing through it.
    const footer = SOURCE.match(/<div className="([^"]*border-t[^"]*)">\s*\n\s*<Button/);
    expect(footer?.[1] ?? "").toContain("shrink-0");
    expect(footer?.[1] ?? "").toContain("bg-popover");
  });

  it("renders Clear after the list, so it is never underneath it", () => {
    const list = SOURCE.indexOf("overflow-y-auto");
    const clear = SOURCE.indexOf(">\n              Clear");
    expect(list).toBeGreaterThan(-1);
    expect(clear).toBeGreaterThan(list);
  });
});
