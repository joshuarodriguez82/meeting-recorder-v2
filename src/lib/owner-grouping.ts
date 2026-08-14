/**
 * Owner normalisation and grouping — frontend mirror of
 * backend/services/owner_service.py's split/normalise/resolve rules.
 *
 * The frontend can't literally import Python, so this file is a
 * hand-kept mirror of the pure (stateless) half of that module — same
 * pattern already used in this codebase for item-status hashing
 * (`computeItemHash` here mirrors `compute_item_hash` in
 * item_status_service.py) and for the follow-up/decision markdown
 * parsers mirrored between insights_service.py and the *-view.tsx
 * files. KEEP THESE TWO FILES IN LOCK-STEP — a change to the
 * split/normalise rules on one side without the other means Follow
 * Ups and Commitments disagree about who owns what.
 *
 * The STATEFUL half (the alias store — confirmed merges persisted to
 * owner_aliases.json) only exists on the backend; the frontend reaches
 * it through the `/owners/*` endpoints in src/lib/api.ts and passes
 * the resulting alias list into resolveOwners() below.
 */

export interface OwnerAlias {
  id: string;
  canonical: string;
  members: string[]; // tier-2 normalised keys
}

// ── Tier 1: splitting a multi-owner string ─────────────────────────────
//
// Split on '/', '&', and the standalone word "and". Deliberately do
// NOT split on comma: real names in this data contain commas (calendar
// organisers show up as "Roe, Bob Jr. [US-US]"), and a comma
// split would shred a single name into two fake people. If you're
// tempted to "improve" this by adding comma — don't.

const SPLIT_RE = /\s*(?:\/|&|\band\b)\s*/gi;

export function splitOwners(raw: string | null | undefined): string[] {
  const s = (raw || "").trim();
  if (!s) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  for (const piece of s.split(SPLIT_RE)) {
    const p = piece.trim();
    if (!p) continue;
    const key = p.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(p);
  }
  return out;
}

// ── Tier 2: safe normalisation of a single owner piece ─────────────────

const ORG_SUFFIX_RE = /\s*\([^()]*\)\s*$/;
const TRAILING_PUNCT_RE = /[\s.,;:!?]+$/;

export interface NormalizedOwner {
  key: string;
  display: string;
}

export function normalizeOwner(raw: string): NormalizedOwner {
  let s = (raw || "").trim();
  s = s.replace(TRAILING_PUNCT_RE, "").trim();
  const stripped = s.replace(ORG_SUFFIX_RE, "").trim();
  if (stripped) s = stripped;
  s = s.replace(TRAILING_PUNCT_RE, "").trim();
  s = s.replace(/\s+/g, " ");
  return { key: s.toLowerCase(), display: s };
}

// ── Aggregating raw owner strings across items ──────────────────────────

export interface OwnerAggregate {
  counts: Record<string, number>;
  display: Record<string, string>;
}

/**
 * Split + tier-2-normalise every raw owner string, and count how many
 * items (raw strings) touch each resulting key. An item with several
 * owners contributes one count to EACH person it names — this is a
 * per-person count, not a per-item count, so summing it across people
 * can legitimately exceed the item total; callers must report the item
 * total separately rather than imply the sum is it. Mirrors
 * owner_service.py's aggregate_raw_owners().
 */
export function aggregateRawOwners(rawStrings: string[]): OwnerAggregate {
  const counts: Record<string, number> = {};
  const variantFreq = new Map<string, Map<string, number>>();
  for (const raw of rawStrings) {
    for (const piece of splitOwners(raw)) {
      const { key, display } = normalizeOwner(piece);
      if (!key) continue;
      counts[key] = (counts[key] || 0) + 1;
      let freq = variantFreq.get(key);
      if (!freq) { freq = new Map(); variantFreq.set(key, freq); }
      freq.set(display, (freq.get(display) || 0) + 1);
    }
  }
  const display: Record<string, string> = {};
  for (const [key, freq] of variantFreq) {
    let best = "", bestCount = -1;
    for (const [d, c] of freq) {
      if (c > bestCount) { best = d; bestCount = c; }
    }
    display[key] = best;
  }
  return { counts, display };
}

// ── Resolving an item's raw owner field to canonical people ────────────

export function buildAliasIndex(
  aliases: OwnerAlias[] | undefined | null,
): Map<string, OwnerAlias> {
  const idx = new Map<string, OwnerAlias>();
  for (const a of aliases || []) {
    for (const m of a.members) idx.set(m, a);
  }
  return idx;
}

/**
 * Resolve a raw owner field (possibly multi-owner) to the list of
 * canonical display names it belongs to. This is what the Follow Ups
 * owner filter dropdown, its counts, and its item-membership checks
 * are all built on.
 *
 * "Mark/Josh" with no aliases       -> ["Mark", "Josh"]
 * "Josh (AWS)"                      -> ["Josh"]
 * "Joshua" with a Josh/Joshua alias -> ["Josh"]
 */
export function resolveOwners(
  raw: string | null | undefined,
  aliasIndex?: Map<string, OwnerAlias>,
): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const piece of splitOwners(raw)) {
    const { key, display } = normalizeOwner(piece);
    if (!key) continue;
    const alias = aliasIndex?.get(key);
    const canonical = alias ? alias.canonical : display;
    const dedupeKey = canonical.toLowerCase();
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    out.push(canonical);
  }
  return out;
}

/** Stable id for a canonical owner in the filter dropdown / management
 * dialog: the alias id when aliased, otherwise the tier-2 key. Distinct
 * from the display string, which can theoretically collide in ways an
 * id must not (kept simple since display strings are already unique
 * per key/alias in practice, but this keeps intent explicit at call
 * sites building a Map). */
export function canonicalId(
  raw: string,
  aliasIndex?: Map<string, OwnerAlias>,
): { id: string; display: string }[] {
  const out: { id: string; display: string }[] = [];
  const seen = new Set<string>();
  for (const piece of splitOwners(raw)) {
    const { key, display } = normalizeOwner(piece);
    if (!key) continue;
    const alias = aliasIndex?.get(key);
    const id = alias ? alias.id : `key:${key}`;
    const canonicalDisplay = alias ? alias.canonical : display;
    if (seen.has(id)) continue;
    seen.add(id);
    out.push({ id, display: canonicalDisplay });
  }
  return out;
}
