// Appearance: light / dark / follow the system, and a colour theme.
//
// Light until chosen otherwise — the look the app has always had, so an
// upgrade never switches someone's theme behind their back.
//
// Stored per device in localStorage — it's how this screen looks, not
// data about meetings, so it doesn't belong in the backend's settings.
// Applied as two attributes on <html>: the `dark` class (the CSS's dark
// variant) and data-accent (globals.css re-tints the brand, action and
// sidebar colours per theme). public/theme-init.js applies the same
// thing before first paint so a dark theme never flashes white; keep
// the two in step (the test in theme.test.ts reads both).

export type ThemeMode = "system" | "light" | "dark";
export type Accent = "teal" | "ocean" | "forest" | "ember" | "graphite";

export interface ThemePrefs {
  mode: ThemeMode;
  accent: Accent;
}

export const THEME_STORAGE_KEY = "mr-theme";
export const DEFAULT_THEME: ThemePrefs = { mode: "light", accent: "teal" };

/** The colour themes, with the swatch shown in Settings. */
export const ACCENTS: { id: Accent; label: string; brand: string; action: string }[] = [
  { id: "teal", label: "Teal", brand: "#0b2f3a", action: "#0d9488" },
  { id: "ocean", label: "Ocean", brand: "#0f2747", action: "#2563eb" },
  { id: "forest", label: "Forest", brand: "#0f2e22", action: "#059669" },
  { id: "ember", label: "Ember", brand: "#3a1a0e", action: "#ea580c" },
  { id: "graphite", label: "Graphite", brand: "#16191f", action: "#475569" },
];

const MODES: ThemeMode[] = ["system", "light", "dark"];

/** Stored prefs, with anything unknown or missing falling back to the
 *  default — a value written by a newer version must not break this one. */
export function parseTheme(raw: string | null | undefined): ThemePrefs {
  try {
    const v = JSON.parse(raw ?? "") as Partial<ThemePrefs>;
    return {
      mode: MODES.includes(v.mode as ThemeMode) ? (v.mode as ThemeMode) : DEFAULT_THEME.mode,
      accent: ACCENTS.some((a) => a.id === v.accent) ? (v.accent as Accent) : DEFAULT_THEME.accent,
    };
  } catch {
    return { ...DEFAULT_THEME };
  }
}

export function loadTheme(): ThemePrefs {
  try {
    return parseTheme(localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return { ...DEFAULT_THEME };
  }
}

/** Whether the mode resolves to dark right now. */
export function isDark(mode: ThemeMode, systemDark: boolean): boolean {
  return mode === "dark" || (mode === "system" && systemDark);
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

export function applyTheme(prefs: ThemePrefs): void {
  const root = document.documentElement;
  root.classList.toggle("dark", isDark(prefs.mode, systemPrefersDark()));
  if (prefs.accent === "teal") root.removeAttribute("data-accent");
  else root.setAttribute("data-accent", prefs.accent);
}

export function saveTheme(prefs: ThemePrefs): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Storage blocked: the choice still applies for this session.
  }
  applyTheme(prefs);
  window.dispatchEvent(new CustomEvent("mr-theme-change", { detail: prefs }));
}

/** Keep "system" in step with the OS switching light/dark while the app
 *  is open. Returns an unsubscribe. */
export function watchSystemTheme(get: () => ThemePrefs): () => void {
  try {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (get().mode === "system") applyTheme(get());
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  } catch {
    return () => {};
  }
}
