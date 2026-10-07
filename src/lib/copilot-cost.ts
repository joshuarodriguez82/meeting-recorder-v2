// Per-call cost estimates for the Live Co-Pilot's tick LLM calls.
//
// Goal: surface a sane "this is roughly what an hour of meeting costs
// you" line in Settings so users can tune their polling intervals
// against a real number. Estimates are deliberately rough — they
// assume average prompt + completion token sizes across many ticks,
// not exact-per-call accuracy. A 30% error here doesn't matter to the
// user's decision; an order-of-magnitude error would.
//
// Prices for the models known at release are in PROVIDER_RATES below.
// A model released after this build has no row: Settings then asks for
// its price per million tokens and remembers it on this computer
// (customRates), so a new model is priced without an app update.

/** Average tokens we send per wide tick — full window, mode prompt,
 *  meeting-type modifier, custom context, prior-tick memory, ~10 min
 *  transcript. Empirically 1500-3000 in; 100-300 out. */
// Raised in 2.83: the prompt now carries the meeting's details (name,
// client, organiser, attendees, the user's notes) and the Co-Pilot
// board (up to 30 items) as well as the ~4.5 min transcript window.
const AVG_WIDE_TOKENS_IN = 2600;
const AVG_WIDE_TOKENS_OUT = 200;

/** Hot tick — narrow window, same prompt prefix but ~90s of transcript
 *  instead of 10 min. Often returns empty arrays (small completion). */
const AVG_HOT_TOKENS_IN = 1800;
const AVG_HOT_TOKENS_OUT = 100;

// USD per 1k tokens, (input, output). Public Anthropic / OpenAI prices
// as of release. Free / local providers use 0 with notes.
type RatePair = { in: number; out: number; note?: string };

const PROVIDER_RATES: Record<string, RatePair> = {
  // Anthropic
  // Haiku 4.5 is $1 / $5 per million tokens. This row said $0.25 /
  // $1.25 — Haiku 3's price — so every estimate was 4x low.
  "anthropic:claude-haiku-4-5": { in: 0.001, out: 0.005 },
  // Haiku 5.5: $0.10 / $0.50 per million for prompts up to 100K tokens
  // (every Co-Pilot tick); $0.50 / $2.50 beyond.
  "anthropic:claude-haiku-5-5": { in: 0.0001, out: 0.0005 },
  "anthropic:claude-sonnet-4-6": { in: 0.003, out: 0.015 },
  "anthropic:claude-sonnet-5": { in: 0.002, out: 0.01 },
  "anthropic:claude-sonnet-5-5": { in: 0.002, out: 0.01 },
  // Opus 4.6-4.8 and 5 are $5 / $25. The 4.7 row said $15 / $75 —
  // Opus 4.1's price — so it read 3x high.
  "anthropic:claude-opus-4-6": { in: 0.005, out: 0.025 },
  "anthropic:claude-opus-4-7": { in: 0.005, out: 0.025 },
  "anthropic:claude-opus-4-8": { in: 0.005, out: 0.025 },
  "anthropic:claude-opus-5": { in: 0.005, out: 0.025 },
  "anthropic:claude-opus-5-5": { in: 0.004, out: 0.02 },
  "anthropic:claude-fable-5": { in: 0.01, out: 0.05 },
  "anthropic:claude-fable-5-1": { in: 0.01, out: 0.05 },
  // OpenAI
  "openai:gpt-4o-mini": { in: 0.00015, out: 0.0006 },
  "openai:gpt-4o": { in: 0.0025, out: 0.01 },
  // OpenRouter (representative — varies by model)
  "openai:openrouter-free": {
    in: 0, out: 0,
    note: "Free tier — ~50 req/day cap. Cap hits in ~12 min of recording at >0.07 calls/sec.",
  },
  // Local providers
  "openai:ollama": { in: 0, out: 0, note: "Local model — no LLM cost." },
  "openai:lmstudio": { in: 0, out: 0, note: "Local model — no LLM cost." },
};

/** A few common providers/models surfaced as comparison rows even when
 *  the user has a different one selected — gives them a sense of what
 *  switching would cost. */
const COMPARISON_KEYS: Array<{ key: string; label: string }> = [
  { key: "anthropic:claude-haiku-5-5", label: "Anthropic Haiku 5.5" },
  { key: "anthropic:claude-haiku-4-5", label: "Anthropic Haiku 4.5" },
  { key: "anthropic:claude-sonnet-5-5", label: "Anthropic Sonnet 5.5" },
  { key: "openai:gpt-4o-mini", label: "OpenAI GPT-4o-mini" },
  { key: "openai:ollama", label: "Ollama (local)" },
  { key: "openai:openrouter-free", label: "OpenRouter free tier" },
];

export interface CostEstimate {
  /** Wide ticks per hour at the current interval. */
  widePerHour: number;
  /** Hot ticks per hour at the current interval. 0 when disabled. */
  hotPerHour: number;
  /** Total LLM calls per minute. */
  callsPerMinute: number;
  /** USD per hour of recording at the user's currently configured
   *  provider + model. Null when we don't recognize the model. */
  currentHourlyUsd: number | null;
  /** Notes specific to the current configuration (e.g. free-tier cap). */
  currentNote?: string;
  /** Comparison rows for common alternatives. */
  comparisons: Array<{ label: string; hourlyUsd: number; note?: string }>;
}

/** Compose the rate key the same way the backend would — provider plus
 *  model id (lowercased + de-spaced for forgiving lookup). The "live"
 *  override takes precedence over the main provider when set. */
export function rateKey(provider: string, model: string): string {
  const p = (provider || "anthropic").trim().toLowerCase();
  // A dated snapshot ID ("claude-haiku-4-5-20251001", what Settings
  // stores) is priced as its model — without this the lookup missed
  // and the estimate showed nothing for the most common setting.
  const m = (model || "").trim().toLowerCase().replace(/-\d{8}$/, "");
  return `${p}:${m}`;
}

/** Best-effort guess for OpenAI-compatible providers based on the
 *  base URL when the rate table doesn't have an exact model match.
 *  Ollama / LM Studio = local = $0. OpenRouter free = $0 with caveat.
 *  Anything else falls through to "unknown" so we don't pretend to
 *  know the price. */
function guessByBaseUrl(baseUrl: string): RatePair | null {
  const u = (baseUrl || "").toLowerCase();
  if (!u) return null;
  if (u.includes("11434") || u.includes("localhost") || u.includes("127.0.0.1")) {
    return PROVIDER_RATES["openai:ollama"];
  }
  if (u.includes("lmstudio") || u.includes("1234")) {
    return PROVIDER_RATES["openai:lmstudio"];
  }
  if (u.includes("openrouter") && u.includes(":free")) {
    return PROVIDER_RATES["openai:openrouter-free"];
  }
  return null;
}

function hourlyForRate(rate: RatePair, widePerHour: number, hotPerHour: number): number {
  const wideCost = widePerHour * (
    (AVG_WIDE_TOKENS_IN / 1000) * rate.in
    + (AVG_WIDE_TOKENS_OUT / 1000) * rate.out
  );
  const hotCost = hotPerHour * (
    (AVG_HOT_TOKENS_IN / 1000) * rate.in
    + (AVG_HOT_TOKENS_OUT / 1000) * rate.out
  );
  return wideCost + hotCost;
}

/** Estimate cost from the user's current settings. */
export function estimateCopilotCost(args: {
  wideIntervalSec: number;
  hotIntervalSec: number;
  /** Active provider for tick calls. "anthropic" or "openai" — when
   *  the live override is set it's the live one; otherwise the main. */
  provider: string;
  /** Model id used for ticks (live override or main). */
  model: string;
  /** OpenAI-compatible base URL (live override or main). Used to
   *  detect Ollama / OpenRouter when the model id isn't in the rates
   *  table. */
  baseUrl: string;
  /** Prices the user entered for models this build doesn't know, keyed
   *  by rateKey(), in USD per MILLION tokens (how providers quote). */
  customRates?: Record<string, { in: number; out: number }>;
}): CostEstimate {
  const widePerHour = args.wideIntervalSec > 0
    ? 3600 / args.wideIntervalSec : 0;
  const hotPerHour = args.hotIntervalSec > 0
    ? 3600 / args.hotIntervalSec : 0;
  const callsPerMinute = (widePerHour + hotPerHour) / 60;

  const key = rateKey(args.provider, args.model);
  let rate: RatePair | null = PROVIDER_RATES[key] ?? null;
  const custom = args.customRates?.[key];
  if (!rate && custom && (custom.in > 0 || custom.out > 0)) {
    rate = { in: custom.in / 1000, out: custom.out / 1000 };
  }
  if (!rate) rate = guessByBaseUrl(args.baseUrl);

  const currentHourlyUsd = rate
    ? hourlyForRate(rate, widePerHour, hotPerHour)
    : null;

  const comparisons = COMPARISON_KEYS.map(({ key: k, label }) => {
    const r = PROVIDER_RATES[k];
    return {
      label,
      hourlyUsd: hourlyForRate(r, widePerHour, hotPerHour),
      note: r.note,
    };
  });

  return {
    widePerHour,
    hotPerHour,
    callsPerMinute,
    currentHourlyUsd,
    currentNote: rate?.note,
    comparisons,
  };
}

export function formatUsd(n: number): string {
  if (n === 0) return "$0";
  if (n < 0.01) return `<$0.01`;
  if (n < 1) return `$${n.toFixed(2)}`;
  if (n < 10) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(1)}`;
}

/** Whether this build has a price for the model (else Settings asks). */
export function hasKnownRate(provider: string, model: string): boolean {
  return rateKey(provider, model) in PROVIDER_RATES;
}

const CUSTOM_RATES_KEY = "mr-model-prices";

/** Prices entered in Settings for models this build doesn't know —
 *  per computer, since they only feed an estimate. USD per million. */
export function loadCustomRates(): Record<string, { in: number; out: number }> {
  try {
    const v = JSON.parse(localStorage.getItem(CUSTOM_RATES_KEY) || "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

export function saveCustomRate(key: string, rate: { in: number; out: number }): void {
  try {
    const all = loadCustomRates();
    all[key] = rate;
    localStorage.setItem(CUSTOM_RATES_KEY, JSON.stringify(all));
  } catch {
    // Storage blocked: the estimate just stays "unknown".
  }
}

