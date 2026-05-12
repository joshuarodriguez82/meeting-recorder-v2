// Thin wrapper around @tauri-apps/plugin-updater so the rest of the app
// can ask "is an update available?" / "install it" without each call
// site needing to know the plugin's API surface or how to dynamic-import
// it for `npm run dev` browser sessions where Tauri isn't present.
//
// All Tauri imports are lazy because the React UI can also run under
// `next dev` against a manually-started backend (no Tauri shell). In
// that context the updater plugin doesn't exist; checks resolve to
// `notAvailable` rather than throwing.

export type UpdateCheckResult =
  | { kind: "available"; version: string; notes: string; date?: string }
  | { kind: "up-to-date"; current: string }
  | { kind: "not-available"; reason: string };

let _cachedUpdater: unknown | null = null;
let _cachedCheck: ReturnType<typeof loadUpdaterCheck> | null = null;

/**
 * Lazy-load the updater plugin. Returns null when not running under Tauri
 * (e.g. `next dev` browser session), or when the plugin failed to load
 * because the build didn't include it.
 */
async function loadUpdaterCheck(): Promise<typeof import("@tauri-apps/plugin-updater")["check"] | null> {
  try {
    const mod = await import("@tauri-apps/plugin-updater");
    _cachedUpdater = mod;
    return mod.check;
  } catch {
    return null;
  }
}

/**
 * Ask GitHub if there's a newer release than the one we're running.
 * Memoizes the loaded plugin handle so subsequent calls don't re-import.
 *
 * `silent` controls error visibility — startup auto-checks pass true so
 * a transient network failure doesn't toast; the user-initiated
 * "Check for Updates" button passes false.
 */
export async function checkForUpdate(): Promise<UpdateCheckResult> {
  if (!_cachedCheck) _cachedCheck = loadUpdaterCheck();
  const check = await _cachedCheck;
  if (!check) {
    return {
      kind: "not-available",
      reason: "Updater plugin not loaded (running outside Tauri shell?)",
    };
  }
  try {
    const update = await check();
    if (update) {
      return {
        kind: "available",
        version: update.version,
        notes: update.body ?? "",
        date: update.date ?? undefined,
      };
    }
    // No update returned = we're already current.
    return { kind: "up-to-date", current: "" };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return { kind: "not-available", reason: msg };
  }
}

/**
 * Download + install the available update, then relaunch the app.
 * Throws on failure (signature verification, network error, disk
 * write error, etc.) — callers should toast the error.
 *
 * The Tauri plugin handles the platform-specific replacement dance
 * (Windows file rename trick, macOS .app extraction). We just kick
 * it off and trust the result.
 */
export async function installUpdate(
  onProgress?: (downloaded: number, total: number | null) => void,
): Promise<void> {
  const mod = (_cachedUpdater ?? (await import("@tauri-apps/plugin-updater"))) as
    typeof import("@tauri-apps/plugin-updater");
  _cachedUpdater = mod;
  const update = await mod.check();
  if (!update) throw new Error("No update available");
  let downloaded = 0;
  let total: number | null = null;
  await update.downloadAndInstall((event) => {
    if (event.event === "Started") {
      total = event.data.contentLength ?? null;
      onProgress?.(0, total);
    } else if (event.event === "Progress") {
      downloaded += event.data.chunkLength;
      onProgress?.(downloaded, total);
    }
  });
  // downloadAndInstall on Windows replaces the exe and queues a relaunch
  // automatically; on macOS we have to ask for the relaunch ourselves.
  try {
    const { relaunch } = await import("@tauri-apps/plugin-process");
    await relaunch();
  } catch {
    // No process plugin available; user can quit + reopen manually.
  }
}
