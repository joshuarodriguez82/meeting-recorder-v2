"use client";

import { useEffect } from "react";
import { applyTheme, loadTheme, watchSystemTheme } from "@/lib/theme";

/** Re-applies the saved appearance once React is up (public/theme-init.js
 *  already did it before first paint) and keeps "System" following the
 *  OS when it switches light/dark while the app is open. Renders nothing. */
export function ThemeSync() {
  useEffect(() => {
    applyTheme(loadTheme());
    return watchSystemTheme(loadTheme);
  }, []);
  return null;
}
